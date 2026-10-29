from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
import threading
import time


class Trace:
    lock = threading.Lock()

    def __init__(self, path):
        self.path = Path(path)

    def emit(self, stage, **values):
        row = dict(stage=stage, ns=time.perf_counter_ns(), pid=os.getpid(), **values)
        with self.lock, self.path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row['ns']


def fingerprint(data):
    if not data:
        return None
    return [data.get('home'), data.get('id'), data.get('calls'), data.get('latest_key')]


@dataclass
class QueryOverlap:
    kind: str
    request: str
    entered: threading.Event | ProcessEvent
    release: threading.Event | ProcessEvent
    cancelled: threading.Event | ProcessEvent
    finished: threading.Event | ProcessEvent
    deadline: float

    @property
    def boundary(self):
        return {'comparison': 'comparison_usage._finish_fold',
                'trend': 'prepared_performance._Input.ordered_rows'}[self.kind]

    def hold(self, trace, calls):
        if self.entered.is_set():
            return
        assert calls > 0, 'controlled overlap requires acquired query inputs'
        trace.emit('controlled_query_entered', request=self.request, boundary=self.boundary, calls=calls)
        self.entered.set()
        if not self.release.wait(self.deadline):
            raise TimeoutError(self.request + ' controlled query gate was not released')
        trace.emit('controlled_query_released', request=self.request,
                   cancelled=self.cancelled.is_set())


@contextmanager
def observe_queries(overlaps, trace):
    if not overlaps:
        yield
        return
    from cachemonitor import comparison_usage
    from cachemonitor.prepared_performance import _Input
    from cachemonitor.analysis_query_actor import QueryActor
    from cachemonitor.prepared_query import PreparedQuery
    requests = {overlap.request: overlap for overlap in overlaps}
    active = threading.local()
    call, fold = PreparedQuery._call, comparison_usage._finish_fold
    ordered_rows, actor_cancel = _Input.ordered_rows, QueryActor.cancel

    def query(self, message, token, cancel):
        overlap = requests.get(message.get('id')) if message['kind'] == 'query' else None
        if overlap is None:
            return call(self, message, token, cancel)
        active.overlap = overlap
        trace.emit('controlled_query_started', request=overlap.request,
                   prepared_token=[token.instance, token.generation])
        try:
            return call(self, message, token, cancel)
        finally:
            trace.emit('controlled_query_finished', request=overlap.request, cancelled=cancel())
            overlap.finished.set()
            active.overlap = None

    def finish_fold(groups, count, total, *args, **kwargs):
        overlap = getattr(active, 'overlap', None)
        if overlap is not None and overlap.kind == 'comparison':
            overlap.hold(trace, total.count)
        return fold(groups, count, total, *args, **kwargs)

    def rows(self, *args, **kwargs):
        iterator = ordered_rows(self, *args, **kwargs)
        try:
            for row in iterator:
                overlap = getattr(active, 'overlap', None)
                if overlap is not None and overlap.kind == 'trend':
                    overlap.hold(trace, 1)
                yield row
        finally:
            iterator.close()

    def cancelled(self, kind=None):
        actor_cancel(self, kind)
        with self.condition:
            if self.active is None or kind is not None and self.active[0] != kind:
                return
            overlap = requests.get(self.active[2].get('id'))
            if overlap is not None and overlap.entered.is_set():
                assert self.active[-1].is_set()
                trace.emit('controlled_query_cancelled', request=overlap.request)
                overlap.cancelled.set()

    PreparedQuery._call, comparison_usage._finish_fold = query, finish_fold
    _Input.ordered_rows, QueryActor.cancel = rows, cancelled
    try:
        yield
    finally:
        PreparedQuery._call, comparison_usage._finish_fold = call, fold
        _Input.ordered_rows, QueryActor.cancel = ordered_rows, actor_cancel


def analysis_process(connection, *args, trace_path, root, profile=False, entered=None, release=None, refresh=None, query_overlaps=()):
    from cachemonitor import analysis_worker
    process_main = analysis_worker.process_main
    trace = Trace(trace_path)
    module_path = Path(analysis_worker.__file__).resolve()
    assert module_path.parents[1] == root, (module_path, root)
    trace.emit('product_import', role='analysis', checkout=str(root), module_path=str(module_path))

    class Connection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def send(self, value):
            if value['kind'] == 'snapshot':
                snapshot = value['value']
                trace.emit('analysis_publication', token=snapshot.get('prepared_token'), source_at=snapshot.get('ts'),
                           summaries=[fingerprint(s) for s in snapshot.get('overlay_sessions', [])],
                           revisions={s['id']:s.get('_overlay_revision') for s in snapshot.get('overlay_sessions', [])})
            elif value['kind'] in ('fatal', 'error', 'overlay_detail', 'stale'):
                trace.emit('analysis_' + value['kind'], request=value.get('id'),
                           summary=fingerprint(value.get('summary')), error=value.get('error'))
            connection.send(value)

    if entered is not None:
        from cachemonitor.prepared_ingest import PreparedCohort, is_scoped_catalog
        convert = PreparedCohort.convert
        checkpoint = None

        def paused_aggregate_checkpoints():
            while not release.is_set():
                if not refresh.wait(60):
                    raise TimeoutError('source-growth gate was not released')
                refresh.clear()
                if not release.is_set():
                    yield dict(checkpoint, changed=(), removed=(), writes=0)
            trace.emit('conversion_released')

        def gated(cohort, catalog, *values, **kwargs):
            nonlocal checkpoint
            aggregate = not is_scoped_catalog(catalog)
            if aggregate and checkpoint is not None:
                yield from paused_aggregate_checkpoints()
            for change in convert(cohort, catalog, *values, **kwargs):
                if aggregate and not change['ready'] and checkpoint is None:
                    ready = cohort.store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]
                    assert ready == 0, 'growth gate requires an incomplete durable publication'
                    trace.emit('conversion_checkpoint', ready=ready,
                               changed=len(change['changed']), writes=change['writes'])
                    checkpoint = change
                    entered.set()
                    yield change
                    yield from paused_aggregate_checkpoints()
                else:
                    yield change
        PreparedCohort.convert = gated
    if profile:
        import cProfile
        from cachemonitor.quota_diagnostics import StageTimings
        snapshot = StageTimings.snapshot

        def stage_snapshot(timing, **kwargs):
            value = snapshot(timing, **kwargs)
            if kwargs.get('complete'):
                trace.emit('cpu_stages', **value)
            return value
        StageTimings.snapshot = stage_snapshot
        from cachemonitor.prepared_ingest import PreparedCohort
        from cachemonitor.prepared_delivery import PreparedPublisher
        sequence = [0]

        def measure_method(owner, name):
            original = getattr(owner, name)

            def measured(instance, *values, **kwargs):
                sequence[0] += 1
                number = sequence[0]
                profiler = cProfile.Profile()
                try:
                    return profiler.runcall(original, instance, *values, **kwargs)
                finally:
                    target = Path(trace_path).with_name(f'{Path(trace_path).stem}-{number:03d}-{name}.pstats')
                    profiler.dump_stats(str(target))
                    trace.emit('profile_saved', method=name, path=target.name)
            setattr(owner, name, measured)
        original_convert = PreparedCohort.convert

        def profiled_convert(cohort, *values, **kwargs):
            sequence[0] += 1
            number = sequence[0]
            profiler = cProfile.Profile()
            iterator = iter(original_convert(cohort, *values, **kwargs))
            try:
                while True:
                    try:
                        value = profiler.runcall(next, iterator)
                    except StopIteration:
                        break
                    yield value
            finally:
                iterator.close()
                target = Path(trace_path).with_name(f'{Path(trace_path).stem}-{number:03d}-convert.pstats')
                profiler.dump_stats(str(target))
                trace.emit('profile_saved', method='convert', path=target.name,
                           metrics=dict(cohort.metrics))
        PreparedCohort.convert = profiled_convert
        measure_method(PreparedPublisher, 'adopt')
        measure_method(PreparedPublisher, 'publish')
    with observe_queries(query_overlaps, trace):
        process_main(Connection(), *args)
