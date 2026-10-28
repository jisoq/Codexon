import sqlite3
import pytest

from cachemonitor.quota_backlog import ObservationBacklog


def _commit_backlog_then_wait(path,control):
    backlog=ObservationBacklog(path,'home')
    for at in (1,2,3):assert backlog.append('quota',{'observed_at':at})
    backlog.acknowledge(backlog.first()[0])
    control.send('committed')
    control.recv()


@pytest.mark.integration
def test_committed_backlog_survives_forced_termination(tmp_path):
    import multiprocessing as mp
    path=tmp_path/'killed.sqlite'
    context=mp.get_context('spawn');parent,pipe=context.Pipe()
    child=context.Process(target=_commit_backlog_then_wait,args=(str(path),pipe))
    child.start();pipe.close()
    try:
        assert parent.poll(15),f'backlog did not commit; exit={child.exitcode}'
        assert parent.recv()=='committed'
        child.kill();child.join(10)
        assert not child.is_alive() and child.exitcode!=0
        restored=ObservationBacklog(path,'home')
        try:
            for at in (2,3):
                item=restored.first();assert item[1:]==('quota',{'observed_at':at})
                restored.acknowledge(item[0])
            assert restored.first() is None
            assert restored.db.execute('pragma integrity_check').fetchone()[0]=='ok'
        finally:restored.close()
    finally:
        if child.is_alive():child.kill()
        child.join(10);parent.close()


def test_pinned_reader_bounds_journal_and_recovery_preserves_ingress(tmp_path,monkeypatch):
    monkeypatch.setattr('cachemonitor.quota_backlog.local_wal',lambda path:True)
    backlog=ObservationBacklog(tmp_path/'pinned.sqlite','home')
    backlog.MAX_WAL_BYTES=64*1024
    db=backlog.connection();db.execute('pragma wal_checkpoint(truncate)')
    assert backlog.append('quota',{'observed_at':0})
    reader=sqlite3.connect(backlog.path)
    reader.execute('begin');reader.execute('select * from pending').fetchall()
    accepted=[0];sizes=[backlog.wal_path.stat().st_size]
    try:
        for at in range(1,100):
            if not backlog.available():break
            assert backlog.append('quota',{'observed_at':at});accepted.append(at)
            sizes.append(backlog.wal_path.stat().st_size)
        else:pytest.fail('pinned reader did not apply journal backpressure')
        size=backlog.wal_path.stat().st_size
        assert size<=backlog.MAX_WAL_BYTES+max(after-before for before,after in zip(sizes,sizes[1:]))
        # An observation already in flight still gets the existing one-item RAM
        # reserve; no later acquisition is allowed until recovery.
        assert backlog.append('quota',{'observed_at':100})
        assert backlog.unsaved is not None and not backlog.available()
        assert backlog.wal_path.stat().st_size==size
        reader.rollback();reader.close();reader=None
        assert backlog.available()
        for at in accepted+[100]:
            item=backlog.first();assert item[1:]==('quota',{'observed_at':at})
            backlog.acknowledge(item[0])
        assert backlog.first() is None and backlog.unsaved is None
    finally:
        if reader:reader.close()
        backlog.close()


def test_remote_storage_keeps_rollback_recovery(tmp_path,monkeypatch):
    monkeypatch.setattr('cachemonitor.quota_backlog.local_wal',lambda path:False)
    backlog=ObservationBacklog(tmp_path/'remote.sqlite','home')
    try:
        assert backlog.append('quota',{'observed_at':1})
        assert backlog.db.execute('pragma journal_mode').fetchone()[0]=='delete'
        assert backlog.close()
        assert backlog.first()[1:]==('quota',{'observed_at':1})
    finally:backlog.close()


def test_capacity_preserves_order_and_reopens_without_losing_distinct_observations(tmp_path):
    path=tmp_path/'quota.sqlite'
    backlog=ObservationBacklog(path,'home')
    backlog.MAX_ITEMS=2
    assert backlog.append('quota',{'observed_at':1})
    assert backlog.append('quota',{'observed_at':2})
    assert not backlog.available() and backlog.issue
    assert not backlog.append('quota',{'observed_at':3}) and backlog.unsaved is None
    assert backlog.close()
    restored=ObservationBacklog(path,'home')
    try:
        first=restored.first()
        assert first[1:] == ('quota',{'observed_at':1})
        # Leaving an entry unacknowledged keeps it replayable across interruption.
        restored.close()
        assert restored.first()==first
        restored.acknowledge(first[0])
        second=restored.first()
        assert second[1:]==('quota',{'observed_at':2})
        restored.acknowledge(second[0])
        assert restored.first() is None and restored.available()
        assert restored.db.execute('select items,bytes from budget').fetchone()==(0,0)
    finally:restored.close()


def test_failed_journal_write_keeps_one_record_and_blocks_further_acquisition(tmp_path,monkeypatch):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    original=backlog._store
    def fail(*args):raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(backlog,'_store',fail)
    assert backlog.append('quota',{'observed_at':1})
    assert not backlog.available()
    assert not backlog.append('quota',{'observed_at':2})
    assert not backlog.close()
    monkeypatch.setattr(backlog,'_store',original)
    assert backlog.close()
    restored=ObservationBacklog(tmp_path/'quota.sqlite','home')
    try:
        row=restored.first()
        assert row[1:]==('quota',{'observed_at':1})
        restored.acknowledge(row[0])
        assert restored.first() is None
    finally:restored.close()


def test_byte_pressure_retains_accepted_observations_until_acknowledged(tmp_path):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    backlog.OBSERVATION_RESERVE_BYTES=64
    backlog.MAX_BYTES=100
    try:
        assert backlog.append('quota',{'value':'x'*40})
        assert not backlog.available()
        before=backlog.first()
        assert not backlog.append('quota',{'value':'x'*100})
        assert backlog.first()==before and backlog.unsaved is None
        assert not backlog.available() and backlog.issue
        backlog.acknowledge(before[0])
        assert backlog.available()
    finally:backlog.close()


def test_control_reserve_retains_stop_boundary_when_observation_capacity_is_full(tmp_path):
    backlog=ObservationBacklog(tmp_path/'quota.sqlite','home')
    backlog.MAX_ITEMS=1
    backlog.CONTROL_RESERVE=1
    try:
        assert backlog.append('quota',{'observed_at':1})
        assert not backlog.available()
        assert backlog.append('control',(False,2))
        assert not backlog.append('control',(True,3))
        assert backlog.unsaved is None
        first=backlog.first();backlog.acknowledge(first[0])
        assert backlog.first()[1:]==('control',[False,2])
    finally:backlog.close()



@pytest.mark.parametrize('model_count,minimum_bytes', [(1400, 64*1024), (6000, 256*1024)])
def test_large_normalized_observation_replays_through_existing_ledger(tmp_path, model_count, minimum_bytes):
    from contextlib import closing
    import json

    from cachemonitor import quota_payloads, quota_tracking_store as tracking
    from cachemonitor.quota_cycles import QuotaLedger
    from cachemonitor.quota_live import normalize_limits

    models = [f'fixture-model-{number:05d}-'+'x'*48 for number in range(model_count)]
    buckets = {'codex': dict(limitId='codex', planType='plus',
        primary=dict(usedPercent=20, windowDurationMins=300, resetsAt=1000),
        secondary=dict(usedPercent=10, windowDurationMins=10080, resetsAt=2000))}
    buckets.update((model, dict(normalModelSlug=model)) for model in models)
    quota = normalize_limits(dict(rateLimitsByLimitId=buckets), 100, 'fixture-account')
    encoded = json.dumps(quota, ensure_ascii=False, separators=(',', ':')).encode()
    assert minimum_bytes < len(encoded) < 8*1024*1024
    path = tmp_path/'quota.sqlite'
    backlog = ObservationBacklog(path, 'home')
    try:
        with closing(QuotaLedger(path)) as ledger:
            tracking.enable(ledger.db, 'control', 90)
            ledger.observe('control', quota, commit=False)
            tracking.observe(ledger.db, 'control', quota)
            control = ledger.db.execute("SELECT * FROM tracking_observations WHERE home='control'").fetchone()
            assert quota_payloads.PayloadReader(ledger.db).decode(control) == quota
            assert backlog.append('quota', quota)
            assert backlog.close()
            row = backlog.first()
            assert row[1:] == ('quota', quota)
            tracking.enable(ledger.db, 'home', 90)
            ledger.observe('home', row[2], commit=False)
            tracking.observe(ledger.db, 'home', row[2])
            tracking.observe(ledger.db, 'home', row[2])
            saved = ledger.db.execute("SELECT * FROM tracking_observations WHERE home='home'").fetchall()
            assert len(saved) == 1
            assert quota_payloads.PayloadReader(ledger.db).decode(saved[0]) == quota
            assert len(quota['separate_models']) == model_count
            backlog.acknowledge(row[0])
            assert backlog.first() is None and backlog.available()
    finally:
        backlog.close()



def test_inflight_response_over_remaining_bytes_uses_one_record_reserve_and_recovers(tmp_path):
    backlog = ObservationBacklog(tmp_path/'quota.sqlite', 'home')
    previous = dict(observed_at=1, payload='x'*(31*256*1024))
    response = dict(observed_at=2, payload='y'*(512*1024))
    try:
        assert backlog.append('quota', previous)
        assert backlog.available() and backlog.reserve_fetch()
        assert backlog.append('quota', response)
        assert backlog.unsaved is not None
        assert backlog.db.execute('SELECT bytes FROM budget').fetchone()[0] <= backlog.MAX_BYTES
        assert not backlog.available()
        assert not backlog.append('quota', dict(observed_at=3))
        row = backlog.first()
        assert row[1:] == ('quota', previous)
        backlog.acknowledge(row[0])
        assert backlog.available()
        row = backlog.first()
        assert row[1:] == ('quota', response)
        backlog.acknowledge(row[0])
        assert backlog.first() is None and backlog.unsaved is None
        assert backlog.available() and not backlog.issue
    finally:
        backlog.close()



def test_cancelled_fetch_reservation_cannot_admit_unrequested_overflow(tmp_path):
    backlog = ObservationBacklog(tmp_path/'quota.sqlite', 'home')
    backlog.MAX_BYTES = 100
    backlog.OBSERVATION_RESERVE_BYTES = 8
    previous = dict(value='x'*40)
    response = dict(value='y'*60)
    try:
        assert backlog.append('quota', previous)
        assert backlog.reserve_fetch()
        assert not backlog.reserve_fetch()
        backlog.cancel_fetch()
        assert not backlog.append('quota', response)
        assert backlog.unsaved is None
        assert backlog.reserve_fetch()
        assert backlog.append('quota', response)
        assert backlog.unsaved is not None
        row = backlog.first()
        assert row[1:] == ('quota', previous)
        backlog.acknowledge(row[0])
        assert backlog.available()
        row = backlog.first()
        assert row[1:] == ('quota', response)
        backlog.acknowledge(row[0])
        assert backlog.first() is None
    finally:
        backlog.close()
