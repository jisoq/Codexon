"""Measure public prepared-analysis workflows on an explicit SQLite copy.

Each invocation starts a fresh QueryActor worker. Request elapsed time includes
admission, delivery, and multiprocessing pipe serialization. GUI, collection,
and network work are excluded. OS file-cache state is uncontrolled.
"""
import argparse
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import multiprocessing as mp
from multiprocessing.connection import wait
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def file_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(path=str(path), bytes=stat.st_size, modified_ns=stat.st_mtime_ns,
                device=stat.st_dev, inode=stat.st_ino, sha256=digest)


def memory(pid=None):
    class Counters(ctypes.Structure):
        _fields_ = [('cb', W.DWORD), ('faults', W.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                'peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged',
                'nonpaged', 'commit', 'peak_commit', 'private')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [W.HANDLE, ctypes.POINTER(Counters), W.DWORD]
    handle = kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1010, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = Counters();value.cb = ctypes.sizeof(value)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return dict(peak_working_set=value.peak_ws, working_set=value.ws,
                    peak_commit=value.peak_commit, private_bytes=value.private)
    finally:
        if pid is not None:
            kernel.CloseHandle(handle)


def worker(connection, checkout, database):
    sys.path.insert(0, checkout)
    try:
        from cachemonitor.prepared_query import PreparedQuery
        from cachemonitor.analysis_query_actor import QueryActor
        from cachemonitor.prepared_store import PreparedReader
        from cachemonitor.native_backend import get_native
        import cachemonitor
        package = Path(cachemonitor.__file__).resolve()
        if not package.is_relative_to(Path(checkout)):
            raise RuntimeError('worker imported a different checkout')
        native = get_native()
        with PreparedReader(database) as reader, reader.pin() as view:
            token = view.token

        class MeasuredQuery(PreparedQuery):
            def __call__(self, message, token, cancel):
                if self.engine is not None:
                    self.engine.read_metrics.clear()
                result = super().__call__(message, token, cancel)
                result['benchmark_work'] = dict(self.engine.read_metrics) if self.engine else {}
                return result

        actor = QueryActor(lambda:MeasuredQuery(database))
        try:
            connection.send(dict(kind='ready', token=(token.instance, token.generation),
                pid=os.getpid(), package=str(package), memory=memory(), cpu_seconds=time.process_time(),
                native=file_identity(native.__file__) if native is not None else None,
                backend=os.environ.get('CODEXON_CPU_BACKEND', 'auto')))
            while True:
                ready = wait([connection, actor.notification], timeout=60)
                if not ready:
                    raise TimeoutError('query worker readiness deadline exceeded')
                if connection in ready:
                    message = connection.recv()
                    if message['kind'] == 'close':
                        break
                    actor.submit(message, token)
                if actor.notification in ready:
                    for result in actor.drain():
                        connection.send(result)
        finally:
            actor.close()
        if actor.failure:
            raise RuntimeError(actor.failure)
        connection.send(dict(kind='closed', cpu_seconds=time.process_time(), memory=memory()))
    except BaseException:
        connection.send(dict(kind='worker_error', trace=traceback.format_exc()))
        raise
    finally:
        connection.close()


def exchange(connection, request):
    connection.send(request)
    deadline = time.monotonic() + 65
    while connection.poll(max(0, deadline-time.monotonic())):
        message = connection.recv()
        if message['kind'] != 'progress':
            return message
    raise TimeoutError('completed public query reply deadline exceeded')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path, required=True, help='Explicit prepared SQLite copy')
    parser.add_argument('--output', type=Path, required=True, help='New JSON report path')
    parser.add_argument('--now', type=float, required=True, help='Fixed query timestamp')
    args = parser.parse_args()
    checkout, database, output = (path.resolve() for path in (args.checkout, args.database, args.output))
    if not database.is_file() or not (checkout/'cachemonitor').is_dir():
        parser.error('database and checkout must exist')
    if output.exists():
        parser.error('output must be a new report path')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(checkout=str(checkout), database=str(database), now=args.now,
        scope='PreparedQuery, QueryActor, admission, delivery and pipe serialization; GUI, collection and network excluded',
        cache='fresh worker and engine; OS file cache uncontrolled', runs=[], errors=[], completed=False, cleanup=False)
    parent, child = mp.Pipe()
    process = mp.Process(target=worker, args=(child, str(checkout), str(database)))
    started, cpu_started = time.perf_counter(), time.process_time()
    peak_family = 0
    try:
        def git(*command):
            return subprocess.check_output(['git', *command], cwd=checkout, text=True).strip()
        report['identity'] = dict(head=git('rev-parse', 'HEAD'), root=git('rev-parse', '--show-toplevel'),
            status=git('status', '--short'), database=file_identity(database),
            runtime=file_identity(sys.executable), python=sys.version,
            lock=file_identity(checkout/'requirements-release.lock'))
        process.start();child.close()
        report['worker_pid'] = process.pid
        if not parent.poll(20):
            raise TimeoutError('worker initialization exceeded deadline')
        ready = parent.recv()
        if ready['kind'] != 'ready':
            raise RuntimeError(ready)
        report['ready'] = ready
        now, day = args.now, 86400
        base = dict(page=5, now=now, start=now-7*day, end=now, period='custom',
            plot_width=768, granularity='auto', trend_period='7d', presentation=True,
            trend_selection=dict(models=None, modes=['Standard', 'Fast']), time_range=[now-7*day, now])
        cases = [('timeline-cold', dict(trend_mode='timeline')),
            ('clock-cold', dict(trend_mode='hour_of_day')),
            ('timeline-revisit', dict(trend_mode='timeline')),
            ('clock-revisit', dict(trend_mode='hour_of_day')),
            ('timeline-3d', dict(trend_mode='timeline', time_range=[now-3*day, now])),
            ('clock-3d', dict(trend_mode='hour_of_day', time_range=[now-3*day, now])),
            ('timeline-return', dict(trend_mode='timeline')),
            ('clock-historical-end', dict(trend_mode='hour_of_day', time_range=[now-7*day, now-day])),
            ('empty', dict(trend_mode='timeline', trend_selection=dict(models=[], modes=['Standard'])))]
        hashes = {}
        for index, (label, change) in enumerate(cases):
            request = dict(kind='query', id=index, logical=label, query=dict(base, **change))
            wall, cpu = time.perf_counter(), time.process_time()
            message = exchange(parent, request)
            elapsed = (time.perf_counter()-wall)*1000
            parent_cpu = (time.process_time()-cpu)*1000
            if message['kind'] != 'result':
                raise RuntimeError(message)
            value = message['result']['performance']
            if message['prepared_token'] != ready['token'] or value['calls'] < 0:
                raise AssertionError('query returned an invalid token or call count')
            if not any(panel['key'] == 'count' for panel in value['panels']):
                raise AssertionError('query omitted its count panel')
            if label == 'empty' and value['calls'] != 0:
                raise AssertionError('empty model selection returned calls')
            digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            origin = {'timeline-revisit':'timeline-cold', 'clock-revisit':'clock-cold',
                      'timeline-return':'timeline-cold'}.get(label)
            if origin and hashes[origin] != digest:
                raise AssertionError('revisited query changed its completed result')
            hashes[label] = digest
            own, remote = memory(), memory(process.pid)
            peak_family = max(peak_family, own['working_set']+remote['working_set'])
            report['runs'].append(dict(label=label, elapsed_ms=elapsed, parent_cpu_ms=parent_cpu,
                calls=value['calls'], panels=len(value['panels']), sha256=digest,
                work=message['benchmark_work'], metrics=message['metrics'],
                current_revision=message['result']['revision'], prepared_token=message['prepared_token'],
                parent_memory=own, worker_memory=remote))
        report['closed'] = exchange(parent, dict(kind='close'))
        if report['closed']['kind'] != 'closed':
            raise RuntimeError(report['closed'])
        process.join(10)
        if process.is_alive():
            raise TimeoutError('worker did not exit after closing')
        if process.exitcode:
            raise RuntimeError('worker exited unsuccessfully')
        report['worker_cpu_ms'] = (report['closed']['cpu_seconds']-ready['cpu_seconds'])*1000
        report['completed'] = True
    except BaseException as error:
        report['errors'].append(dict(type=type(error).__name__, message=str(error), trace=traceback.format_exc()))
    finally:
        child.close()
        if process.is_alive():
            process.terminate();process.join(10)
        report.update(cleanup=not process.is_alive(), exit_code=process.exitcode,
            whole_elapsed_ms=(time.perf_counter()-started)*1000,
            parent_cpu_ms=(time.process_time()-cpu_started)*1000, parent_memory=memory(),
            sampled_family_peak_working_set=peak_family,
            memory_method='parent+worker working sets sampled after replies; shared pages can be counted twice; lifetime process peaks are separate, not a continuous family peak')
        if not report['cleanup']:
            report['errors'].append(dict(type='CleanupFailure', message='owned worker is still alive'))
        parent.close()
        if report['cleanup']:
            process.close()
        report['error_count'] = len(report['errors'])
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(dict(output=str(output), completed=report['completed'],
                              errors=report['error_count'], cleanup=report['cleanup'])), flush=True)
    return 0 if report['completed'] and report['cleanup'] and not report['errors'] else 1


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
