from contextlib import closing
import multiprocessing
import time
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor import analysis_worker
from cachemonitor.mismatch_history import MismatchArchive, MismatchReader, archive_path
from cachemonitor.mismatch_page import MismatchPage
from tests.test_mismatch_history import Source, call


class ArchiveEndpoint:
    def __init__(self, connection, kind, waiting, release, waiting_latest, finish):
        self.connection = connection
        self.kind = kind
        self.waiting = waiting
        self.release = release
        self.waiting_latest = waiting_latest
        self.finish = finish
        self.requests = 0

    def send(self, value):
        self.connection.send(value)

    def poll(self, *args):
        return self.connection.poll(*args)

    def recv(self):
        message = self.connection.recv()
        if message['kind'] == self.kind:
            self.requests += 1
            if self.requests == 1:
                self.waiting.set()
                assert self.release.wait(30), 'the first archive request was not released'
            elif self.requests == 2:
                self.connection.send(dict(kind='error', id=message['id'], error='unrelated query failure'))
                self.waiting_latest.set()
                assert self.finish.wait(30), 'the blocked archive endpoint was not released'
        return message

    def close(self):
        self.connection.close()


def gated_static_process(arguments, kind, waiting, release, waiting_latest, finish):
    endpoint = ArchiveEndpoint(arguments[0], kind, waiting, release, waiting_latest, finish)
    analysis_worker.process_main(endpoint, *arguments[1:])


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'the observable bridge state did not arrive'
        QTest.qWait(10)


@pytest.mark.integration
@pytest.mark.parametrize('kind', ['mismatch_page', 'mismatch_record'])
def test_same_revision_restart_recovers_latest_outstanding_archive_read(tmp_path, monkeypatch, kind):
    app = QApplication.instance() or QApplication([])
    quota = tmp_path/'usage.sqlite'
    archive = archive_path(quota)
    with closing(MismatchArchive(archive)) as writer:
        health = writer.capture(Source(dict(call('older'), ts=99.), dict(call('newer'), ts=101.)))
        assert health['status'] == 'ready' and health['revision'] > 0
    with closing(MismatchReader(archive)) as reader:
        expected = reader.window()['window']['rows']
    context = multiprocessing.get_context('spawn')
    waiting, release = context.Event(), context.Event()
    waiting_latest, finish = context.Event(), context.Event()
    processes = []

    def launch(**kwargs):
        if not processes:
            process = context.Process(target=gated_static_process,
                args=(kwargs['args'], kind, waiting, release, waiting_latest, finish), daemon=True)
        else:
            process = context.Process(**kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(analysis_worker.mp, 'get_context',
                        lambda *_: SimpleNamespace(Pipe=context.Pipe, Process=launch))
    snapshot = dict(ts=200., sessions=[], homes=[], errors=[], unassigned=[], mismatch_history=health)
    bridge = analysis_worker.AnalysisBridge([], index_path=tmp_path/'index.sqlite',
        static_snapshot=snapshot, quota_path=quota, model_evidence_path=tmp_path/'evidence.sqlite')
    page = MismatchPage()
    page.set_worker(bridge)
    page.set_active(True)
    page.receive_health(health)
    pages, records, snapshots, faults = [], [], [], []
    bridge.mismatch_page.connect(pages.append)
    bridge.mismatch_record.connect(records.append)
    bridge.fault.connect(faults.append)

    def receive_snapshot(value):
        snapshots.append(value)
        page.receive_health(value['mismatch_history'])
        page.refresh(automatic=True)

    bridge.snapshot.connect(receive_snapshot)
    page.refresh(automatic=True)
    bridge.start()
    try:
        if kind == 'mismatch_record':
            wait_until(lambda: page.table.rowCount() == 2)
            page.open_record(0)
        wait_until(waiting.is_set)
        if kind == 'mismatch_page':
            page.request_page()
            latest = page.page_request
            deliveries = pages
        else:
            page.open_record(1)
            latest = page.record_request
            deliveries = records
        release.set()
        wait_until(lambda: waiting_latest.is_set() and bool(deliveries)
                   and any(value.get('request_id') == latest and value.get('query_failed') for value in faults))
        assert deliveries[0]['id'] != latest
        assert page.table.rowCount() == (0 if kind == 'mismatch_page' else 2)
        assert page.saved_record is None
        request_ids = page.page_request, page.record_request
        processes[0].terminate()
        wait_until(lambda: len(snapshots) >= 2 and any(value['id'] == latest for value in deliveries))
        assert all(value['mismatch_history']['revision'] == health['revision'] for value in snapshots)
        assert (page.page_request, page.record_request) == request_ids
        assert [value['id'] for value in deliveries] == [latest-1, latest]
        assert list(page.table.model().rows.values) == expected
        if kind == 'mismatch_record':
            assert page.saved_record == expected[1]
            assert page.detail.isVisible()
        completed = len(pages), len(records)
        processes[1].terminate()
        wait_until(lambda: len(snapshots) >= 3)
        before_sync = len(snapshots)
        bridge.request_snapshot()
        wait_until(lambda: len(snapshots) > before_sync)
        assert (len(pages), len(records)) == completed
        assert (page.page_request, page.record_request) == request_ids
    finally:
        if processes and processes[0].is_alive():
            release.set()
            finish.set()
        bridge.requestInterruption()
        if not bridge.wait(3000):
            for process in processes:
                if process.pid and process.is_alive():
                    process.terminate()
            assert bridge.wait(5000), 'the isolated analysis processes did not stop'
        for process in processes:
            process.join(5)
            assert not process.is_alive()
        page.deleteLater()
        app.processEvents()
