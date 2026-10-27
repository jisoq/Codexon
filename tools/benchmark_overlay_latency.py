"""Trace isolated source-file changes through normal analysis and native painting.

Scenarios use UUID sessions and exact expected call rows. Timed runs measure
source/evidence writes or analysis restart to Qt afterRendering after an exact OverlayContent
paint. The default collector runs its normal source-watcher service loop.
--collector entry instead triggers CollectorService.poll explicitly. The target is an owned QWidget with an explicit local
Selection. Codex route discovery, physical monitor presentation, network calls,
and upstream model generation are outside this claim.

Run with the locked release interpreter through tools/run_ui_checks.py. Use a
separate --profile run for cProfile and production wall/thread CPU stage records.
The growth case requires a committed, incomplete conversion cohort. It
then appends and collects new source rows before releasing that local test gate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Trace:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def emit(self, stage, **values):
        row = dict(stage=stage, ns=time.perf_counter_ns(), pid=os.getpid(), **values)
        with self.lock, self.path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row['ns']


def fingerprint(data):
    if not data:
        return None
    return [data.get('home'), data.get('id'), data.get('calls'), data.get('latest_key')]


def analysis_process(connection, *args, trace_path, profile=False, entered=None, release=None):
    from cachemonitor.analysis_worker import process_main
    trace = Trace(trace_path)

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
        from cachemonitor.prepared_ingest import PreparedCohort
        convert = PreparedCohort.convert

        def gated(cohort, *values, **kwargs):
            for change in convert(cohort, *values, **kwargs):
                if not change['ready'] and not entered.is_set():
                    ready = cohort.store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]
                    assert ready == 0, 'growth gate requires an incomplete durable publication'
                    trace.emit('conversion_checkpoint', ready=ready,
                               changed=len(change['changed']), writes=change['writes'])
                    entered.set()
                    if not release.wait(60):
                        raise TimeoutError('source-growth gate was not released')
                    trace.emit('conversion_released')
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
    process_main(Connection(), *args)


def collector_process(connection, home, index, evidence, trace_path, profile, mode):
    if mode == 'watcher':
        return watcher_process(connection, home, index, evidence, trace_path, profile)
    from cachemonitor.usage_collection import CollectorService
    trace = Trace(trace_path)
    service = None
    try:
        service = CollectorService([home], index, evidence)
        connection.send(dict(kind='started', pid=os.getpid()))
        while True:
            command = connection.recv()
            if command['kind'] == 'stop':
                break
            trace.emit('collector_entry', scenario=command['scenario'])
            deadline = time.monotonic() + command['deadline']
            polls = 0
            cpu = time.process_time()
            profiler = None
            if profile:
                import cProfile
                profiler = cProfile.Profile()
                profiler.enable()
            while True:
                value = service.poll()
                polls += 1
                sessions = {s['id']: s for s in value['sessions']}
                selected = sessions.get(command['sid'], {})
                if collected_matches(value, selected, command):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError('collector did not expose exact requested call count')
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(Path(trace_path).with_name(command['scenario'] + '-collector.pstats')))
            stamp = trace.emit('collector_publication', scenario=command['scenario'],
                               owners=len(sessions), calls=sum(len(s['history']) for s in sessions.values()),
                               selected=fingerprint(dict(selected, calls=len(selected.get('history', ())),
                                   latest_key=selected['history'][-1]['key'] if selected.get('history') else None)), polls=polls,
                               cpu_seconds=time.process_time() - cpu,
                               bytes_read=value['index'].get('bytes_read'))
            connection.send(dict(kind='collected', ns=stamp, selected=selected,
                                 owners=len(sessions), errors=value['errors'], source_at=value['ts'],
                                 activity=value.get('request_activity', [])))
    except BaseException:
        connection.send(dict(kind='error', error=traceback.format_exc()))
    finally:
        if service is not None:
            service.close()
        connection.close()


def collected_matches(snapshot, selected, command):
    return bool(snapshot['usage_collection_complete']
                and len(snapshot['sessions']) >= command.get('owners', 0)
                and len(selected.get('history', ())) == command['calls']
                and (not command.get('activity') or any(
                    all(row.get(k) == v for k, v in command['activity'].items())
                    for row in snapshot.get('request_activity', ())))
                and all(selected['history'][-1].get(k) == v
                        for k, v in command.get('evidence', {}).items()))


def watcher_process(connection, home, index, evidence, trace_path, profile):
    from cachemonitor import usage_collection
    from cachemonitor.collection_replay import EncodedSession
    import zlib
    trace = Trace(trace_path)
    send_lock = threading.Lock()
    expectation_lock = threading.Lock()
    expectation = [None]
    stopping = threading.Event()
    original_poll = usage_collection.CollectorService.poll
    original_stopping = usage_collection.CollectorService.stopping

    def send(value):
        with send_lock:
            connection.send(value)

    def controls():
        while True:
            try:
                command = connection.recv()
            except EOFError:
                stopping.set()
                return
            if command['kind'] == 'stop':
                stopping.set()
                return
            with expectation_lock:
                expectation[0] = command
            send(dict(kind='armed', scenario=command['scenario']))

    def poll(service, *values, **kwargs):
        started = time.process_time()
        trace.emit('collector_entry')
        value = original_poll(service, *values, **kwargs)
        stamp = time.perf_counter_ns()
        with expectation_lock:
            command = expectation[0]
        if command is not None:
            selected = next((row for row in value['sessions'] if row['id'] == command['sid']), {})
            if isinstance(selected, EncodedSession):
                selected = json.loads(zlib.decompress(selected.payload))
            if collected_matches(value, selected, command):
                trace.emit('collector_publication', scenario=command['scenario'],
                           published_ns=stamp, owners=len(value['sessions']),
                           selected=fingerprint(dict(selected, calls=len(selected.get('history', ())),
                               latest_key=selected['history'][-1]['key'] if selected.get('history') else None)),
                           cpu_seconds=time.process_time() - started,
                           source_hint=bool(kwargs.get('source_hint')))
                with expectation_lock:
                    if expectation[0] is command:
                        expectation[0] = None
                send(dict(kind='collected', ns=stamp, selected=selected,
                          owners=len(value['sessions']), errors=value['errors'], source_at=value['ts'],
                          activity=value.get('request_activity', [])))
        return value

    usage_collection.CollectorService.poll = poll
    usage_collection.CollectorService.stopping = lambda service: stopping.is_set() or original_stopping(service)
    controls_thread = threading.Thread(target=controls, name='benchmark-control', daemon=True)
    controls_thread.start()
    send(dict(kind='started', pid=os.getpid()))
    sys.argv = ['usage-collector', '--codex-home', str(home), '--index-path', str(index),
                '--evidence-path', str(evidence)]
    try:
        result = usage_collection.main()
        if result:
            raise RuntimeError('normal collector loop failed with ' + str(result))
    except BaseException:
        send(dict(kind='error', error=traceback.format_exc()))
    finally:
        connection.close()


def event(kind, at, **payload):
    return dict(type=kind, timestamp=datetime.fromtimestamp(at, timezone.utc).isoformat(), payload=payload)


@dataclass
class Scenario:
    name: str
    sid: str
    rows: list[dict] = field(default_factory=list)
    source_ns: int = 0
    frame_ns: int | None = None
    paint_ns: int | None = None
    content_ns: int | None = None
    bridge_ns: int | None = None
    detail_ns: int | None = None
    collector_ns: int | None = None
    receiver_ns: int | None = None
    previous_revision: tuple | None = None
    evidence: dict = field(default_factory=dict)
    activity: dict = field(default_factory=dict)
    observed_activity: list = field(default_factory=list)
    source_at: float = 0.
    unattributed: bool = False

    def matches(self, data, home):
        return bool(self.rows and fingerprint(data) == self.identity(home)
                    and (self.previous_revision is None
                         or tuple(data.get('_overlay_revision') or ()) != self.previous_revision))

    def matches_snapshot(self, value, home):
        if self.unattributed:
            return bool(self.source_at and value['ts'] >= self.source_at
                        and not any(s['id'] == self.sid for s in value.get('overlay_sessions', ())))
        return any(self.matches(s, home) for s in value.get('overlay_sessions', ()))

    def identity(self, home):
        return [str(home), self.sid, len(self.rows), self.rows[-1]['key']]


def add_session(home, sid, title):
    path = home / 'sessions' / (sid + '.jsonl')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                   (sid, str(path), int(time.time()), title, title, 'gpt-6.1-sol', 'openai', str(home)))
    path.write_text(json.dumps(event('session_meta', time.time() - 100, id=sid)) + '\n', encoding='utf-8')
    return path


def append_rows(home, scenario, count, trace):
    records = []
    for _ in range(count):
        ordinal = len(scenario.rows)
        key = scenario.sid + '-response-' + str(ordinal)
        at = time.time() - 60 + ordinal / 10000
        turn = scenario.sid + '-turn-' + str(ordinal)
        tokens = dict(input_tokens=10000 + ordinal, cached_input_tokens=8000,
                      cache_write_input_tokens=0, output_tokens=100 + ordinal,
                      reasoning_output_tokens=10)
        records.append(event('turn_context', at - .00001, turn_id=turn,
                             model='gpt-6.1-sol', effort='high', service_tier='default'))
        records.append(event('token_usage_record', at, thread_id=scenario.sid,
                             turn_id=turn, response_id=key, model='gpt-6.1-sol',
                             service_tier='default', usage=tokens))
        scenario.rows.append(dict(key=key, input=tokens['input_tokens'], cached=8000,
                                  written=0, output=tokens['output_tokens'], reasoning=10))
    with (home / 'sessions' / (scenario.sid + '.jsonl')).open('a', encoding='utf-8') as output:
        output.write(''.join(json.dumps(value) + '\n' for value in records))
    scenario.source_ns = trace.emit('source_event', scenario=scenario.name, sid=scenario.sid,
                                    calls=len(scenario.rows), appended=count)


def check_output(data, scenario, home):
    if scenario.unattributed:
        return data is None and scenario.collector_ns is not None and scenario.bridge_ns is not None
    if not scenario.matches(data, home) or data.get('_recent_refs'):
        return False
    if scenario.evidence and any(data.get('latest', {}).get(k) != v for k, v in scenario.evidence.items()):
        return False
    assert data['priced'] == len(scenario.rows) and data['missing'] == 0, data
    costs = [((r['input'] - r['cached']) * 2 + r['cached'] * .1 + r['output'] * 10) / 1e6
             for r in scenario.rows]
    assert math.isclose(data['cost'], sum(costs), rel_tol=1e-12), (data['cost'], sum(costs))
    assert math.isclose(data['latest_cost'], costs[-1], rel_tol=1e-12)
    recent = scenario.rows[-20:]
    assert [row['key'] for row in data['recent']] == [row['key'] for row in recent]
    for actual, expected in zip(data['recent'], recent):
        for key, value in expected.items():
            assert actual[key] == value, (key, actual, expected)
    return True


def host_snapshot():
    if os.name != 'nt':
        return dict(logical_processors=os.cpu_count(), platform=platform.platform())
    command = "& { $cpu=Get-CimInstance Win32_Processor; $load=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\"; $queue=Get-CimInstance Win32_PerfFormattedData_PerfOS_System; [pscustomobject]@{cores=($cpu|Measure-Object NumberOfCores -Sum).Sum;logical_processors=($cpu|Measure-Object NumberOfLogicalProcessors -Sum).Sum;cpu_percent=$load.PercentProcessorTime;queue=$queue.ProcessorQueueLength}|ConvertTo-Json -Compress }"
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else dict(error=result.stderr)


def run(args, directory, report):
    from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
    configure_font_rendering()
    configure_high_dpi()
    from PySide6.QtCore import QEventLoop, QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QWidget
    from cachemonitor import analysis_worker
    from cachemonitor.overlay import OverlayController
    from cachemonitor.overlay_tracking import Selection
    from cachemonitor.overlay_windows import WindowsOverlay
    from cachemonitor.overlay_view import OverlayContent
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    load_bundled_fonts()
    home = directory / 'home'
    home.mkdir()
    (home / 'sessions').mkdir()
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,updated_at INTEGER,name TEXT,title TEXT,model TEXT,model_provider TEXT,cwd TEXT)')
    with sqlite3.connect(home / 'logs_2.sqlite') as db:
        db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,thread_id TEXT,process_uuid TEXT,feedback_log_body TEXT)')
    index, evidence, quota = (directory / name for name in ('index.sqlite', 'evidence.sqlite', 'quota.sqlite'))
    trace = Trace(directory / 'gui.jsonl')
    context = mp.get_context('spawn')
    parent, child = context.Pipe()
    collector = context.Process(target=collector_process,
        args=(child, home, index, evidence, directory / 'collector.jsonl', args.profile, args.collector))
    host = QWidget()
    host.setWindowTitle('Codexon isolated overlay latency target')
    host.resize(1100, 900)
    host.show()
    native = WindowsOverlay()
    native.visible_target = lambda hwnd: bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    controller = OverlayController(QSettings(str(directory / 'overlay.ini'), QSettings.IniFormat),
                                   native_enabled=False, appearance_path=directory / 'appearance.toml')
    controller.native = native
    native.configure(int(controller.widget.winId()))
    bridge = None
    current = None
    connected = False
    paint = OverlayContent.paint
    receiver = analysis_worker.SnapshotReceiver

    class Receiver(receiver):
        def receive(self, value):
            result = super().receive(value)
            if current is not None and result is not None:
                if current.matches_snapshot(result, home):
                    if current.receiver_ns is None:
                        current.receiver_ns = trace.emit('snapshot_receiver', scenario=current.name)
            return result
    analysis_worker.SnapshotReceiver = Receiver

    def painted(content, painter):
        try:
            paint(content, painter)
            if content is controller.widget.content_model and current is not None:
                if current.paint_ns is None and check_output(content.data, current, home):
                    current.paint_ns = trace.emit('native_paint', scenario=current.name,
                                                 summary=fingerprint(content.data))
        except BaseException:
            report['errors'].append(traceback.format_exc())
    OverlayContent.paint = painted

    def rendered():
        if current is not None and current.paint_ns and current.frame_ns is None:
            current.frame_ns = trace.emit('native_after_rendering', scenario=current.name)

    def changed():
        try:
            if current is not None and current.content_ns is None:
                if check_output(controller.widget.content_model.data, current, home):
                    current.content_ns = trace.emit('content_model', scenario=current.name)
        except BaseException:
            report['errors'].append(traceback.format_exc())
    controller.widget.content_model.changed.connect(changed)

    def wait_for(predicate, label, *, check_errors=True):
        nonlocal connected
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(5)
        deadline = time.monotonic() + args.deadline
        errors = []

        def poll():
            nonlocal connected
            try:
                if check_errors and report['errors']:
                    raise RuntimeError(report['errors'][-1])
                controller.observed_at = time.monotonic()
                if controller.widget._quick is not None and not connected:
                    controller.widget.quick.quickWindow().afterRendering.connect(rendered, Qt.DirectConnection)
                    connected = True
                if predicate():
                    loop.quit()
                elif time.monotonic() >= deadline:
                    raise TimeoutError(label)
            except BaseException as error:
                errors.append(error)
                loop.quit()
        timer.timeout.connect(poll)
        timer.start()
        poll()
        if not errors and not predicate():
            loop.exec()
        timer.stop()
        if errors:
            raise errors[0]

    armed = [None]

    def arm(scenario, count=0, owners=0):
        command = dict(kind='poll', scenario=scenario.name, sid=scenario.sid,
                       calls=len(scenario.rows) + count, owners=owners, deadline=args.deadline, activity=scenario.activity,
                       evidence={k:v for k,v in scenario.evidence.items()
                                 if k in ('response_status', 'response_model', 'timing_valid')})
        if args.collector == 'watcher':
            parent.send(command)
            wait_for(parent.poll, 'collector observation armed')
            value = parent.recv()
            assert value['kind'] == 'armed', value
            armed[0] = (scenario, owners)

    def append(scenario, count, owners=0):
        arm(scenario, count, owners)
        append_rows(home, scenario, count, trace)

    def collect(scenario, owners=0):
        if args.collector == 'watcher' and armed[0] != (scenario, owners):
            arm(scenario, owners=owners)
        if args.collector == 'entry':
            parent.send(dict(kind='poll', scenario=scenario.name, sid=scenario.sid,
                             calls=len(scenario.rows), owners=owners, deadline=args.deadline, activity=scenario.activity,
                             evidence={k:v for k,v in scenario.evidence.items()
                                       if k in ('response_status', 'response_model', 'timing_valid')}))
        wait_for(parent.poll, 'collector publication')
        value = parent.recv()
        assert value['kind'] == 'collected', value
        assert not value['errors'], value
        assert [r['key'] for r in value['selected'].get('history', ())] == [r['key'] for r in scenario.rows]
        scenario.collector_ns = value['ns']
        scenario.source_at = value['source_at']
        scenario.observed_activity = [row for row in value.get('activity', ())
            if scenario.activity and all(row.get(k) == v for k, v in scenario.activity.items())]
        if scenario.activity:
            assert len(scenario.observed_activity) == 1, value
        armed[0] = None
        return value

    def start_bridge(label, entered=None, release=None):
        nonlocal bridge
        analysis_worker.process_main = partial(analysis_process,
            trace_path=directory / (label + '-analysis.jsonl'), profile=args.profile,
            entered=entered, release=release)
        bridge = analysis_worker.AnalysisBridge([str(home)], index,
            model_evidence_path=evidence, quota_path=quota, cache_floor=0)

        def snapshot(value):
            if current is not None and current.matches_snapshot(value, home):
                if current.bridge_ns is None:
                    current.bridge_ns = trace.emit('bridge_snapshot', scenario=current.name)
            controller.receive_snapshot(value)
            if current is not None and current.unattributed and current.bridge_ns is not None:
                controller.widget.content_model.update()

        def detail(value):
            if current is not None and current.matches(value.get('summary'), home):
                current.detail_ns = trace.emit('selected_detail', scenario=current.name)
            controller.receive_overlay_detail(value)
        bridge.snapshot.connect(snapshot)
        bridge.overlay_detail.connect(detail)
        bridge.failure.connect(report['errors'].append)
        bridge.fault.connect(controller.receive_overlay_fault)
        controller.overlay_loader = bridge.request_overlay_detail
        bridge.start()

    def stop_bridge():
        nonlocal bridge
        if bridge is not None:
            bridge.requestInterruption()
            wait_for(lambda: not bridge.isRunning(), 'analysis shutdown', check_errors=False)
            assert bridge.process is None or not bridge.process.is_alive()
            bridge.wait()
            bridge = None

    def select(scenario):
        controller.receive_target(dict(target=dict(hwnd=int(host.winId())),
            selection=Selection(scenario.sid), confirmed=True))

    def finish(scenario):
        wait_for(lambda: scenario.frame_ns is not None, scenario.name + ' native frame')
        data = controller.widget.content_model.data
        assert scenario.source_ns <= scenario.content_ns <= scenario.paint_ns <= scenario.frame_ns
        assert check_output(data, scenario, home)
        assert controller.widget.isVisible() and native.u.IsWindowVisible(int(controller.widget.winId()))
        assert not any(w.qml_errors for w in (controller.widget, *controller.chrome))
        image = controller.widget.quick.grabFramebuffer()
        assert not image.isNull() and image.width() > 100 and image.height() > 100
        colors = {image.pixelColor(x, y).rgba() for x in range(0, image.width(), 7)
                  for y in range(0, image.height(), 7)}
        assert len(colors) > 10, 'native frame contains no rendered overlay content'
        assert image.save(str(directory / (scenario.name + '.png')))
        if scenario.evidence:
            speed = next(item for item in controller.widget.content_model.headline_metrics() if item['id'] == 'speed')
            expected = scenario.evidence['output_speed']
            assert speed['number'] == ('—' if expected is None else f'{expected:,.1f}')
        if scenario.unattributed:
            metrics = {item['id']:item['number'] for item in controller.widget.content_model.headline_metrics()}
            assert controller.widget.content_model.latest_call() == {}
            assert metrics['cost'] == metrics['speed'] == '—'
        frame_hash = hashlib.sha256(bytes(image.constBits())).hexdigest()
        output = (dict(calls=0, cost=None, latest={}, revision=None, recent_keys=[])
                  if scenario.unattributed else dict(calls=data['calls'], cost=data['cost'], latest=data['latest'],
                    revision=data.get('_overlay_revision'), recent_keys=[r['key'] for r in data['recent']]))
        result = dict(name=scenario.name, calls=len(scenario.rows), source_ns=scenario.source_ns,
            source_at=scenario.source_at, unattributed=scenario.unattributed, activity=scenario.observed_activity,
            stage_ms={name: (value - scenario.source_ns) / 1e6 for name, value in
                [('collector', scenario.collector_ns), ('receiver', scenario.receiver_ns), ('bridge', scenario.bridge_ns),
                 ('detail', scenario.detail_ns), ('content', scenario.content_ns),
                 ('paint', scenario.paint_ns), ('frame', scenario.frame_ns)] if value is not None},
            frame_sha256=frame_hash, native_hwnd=int(controller.widget.winId()),
            output=output)
        report['samples'].append(result)
        trace.emit('assertions_passed', scenario=scenario.name, calls=len(scenario.rows), frame_sha256=frame_hash)
        print(json.dumps(dict(scenario=scenario.name, stage_ms=result['stage_ms']), ensure_ascii=False), flush=True)

    def observe(scenario, status, response_id):
        arm(scenario)
        rollout = home / 'sessions' / (scenario.sid + '.jsonl')
        source_hash = hashlib.sha256(rollout.read_bytes()).hexdigest()
        from cachemonitor.model_evidence import EvidenceStore
        store = EvidenceStore(evidence)
        at = time.time()
        scenario.source_at = at
        try:
            store.write(home, scenario.activity['attempt'], at, 'WebSocket',
                response_id=response_id, requested_model='gpt-6.1-sol',
                response_model='gpt-6.1-sol' if response_id else '', status=status,
                requested_service_tier='default', response_service_tier='default' if response_id else '',
                request_observed_at=at - 1,
                generation_observed_at=at - .75 if status != 'pending' else None,
                completed_observed_at=at if status == 'completed' else None,
                generation_latency_ms=250. if status != 'pending' else None,
                completion_latency_ms=1000. if status == 'completed' else None)
        finally:
            store.close()
        scenario.source_ns = trace.emit('evidence_event', scenario=scenario.name,
            sid=scenario.sid, simulated_local_timing=True, status=status, response_id=response_id)
        collect(scenario)
        if scenario.unattributed:
            controller.widget.content_model.update()
        finish(scenario)
        assert hashlib.sha256(rollout.read_bytes()).hexdigest() == source_hash

    try:
        collector.start()
        child.close()
        wait_for(parent.poll, 'collector startup')
        assert parent.recv()['kind'] == 'started'
        unknown = str(uuid.uuid4())
        add_session(home, unknown, 'Unattributed request benchmark')
        for status in ('pending', 'created', 'completed'):
            current = Scenario('unattributed_' + status, unknown, unattributed=True,
                activity=dict(attempt='local-unattributed-request', status=status,
                              response_id='' if status == 'pending' else 'unbound-response'))
            select(current)
            if bridge is None:
                start_bridge('unattributed')
            observe(current, status, current.activity['response_id'])
        stop_bridge()
        fresh = Scenario('fresh', str(uuid.uuid4()))
        add_session(home, fresh.sid, 'Fresh benchmark session')
        current = fresh
        select(current)
        append(current, 1)
        collect(current)
        start_bridge('fresh')
        finish(current)
        current = Scenario('warm_append', fresh.sid, list(fresh.rows))
        append(current, 1)
        collect(current)
        finish(current)
        for status in ('created', 'completed'):
            current = Scenario('response_progress' if status == 'created' else 'response_evidence',
                current.sid, list(current.rows),
                previous_revision=tuple(controller.widget.data.get('_overlay_revision') or ()),
                activity=dict(attempt='local-simulated-completion', status=status,
                              response_id=current.rows[-1]['key']))
            current.evidence = dict(response_status=status, requested_model='gpt-6.1-sol',
                response_model='gpt-6.1-sol', timing_valid=status == 'completed',
                output_speed=float(current.rows[-1]['output']) if status == 'completed' else None)
            if status == 'completed':
                current.evidence.update(model_match='일치', generation_latency_ms=250., completion_latency_ms=1000.)
            observe(current, status, current.rows[-1]['key'])
        stop_bridge()
        retained = current
        for number in range(args.owners - 1):
            owner = Scenario('history', str(uuid.uuid4()))
            add_session(home, owner.sid, 'Retained benchmark session ' + str(number))
            append_rows(home, owner, args.rows, trace)
        current = Scenario('retained_seed', retained.sid, list(retained.rows))
        append(current, max(0, args.rows - len(current.rows)), owners=args.owners)
        collect(current, owners=args.owners)
        current = Scenario('restart_retained', current.sid, list(current.rows))
        current.source_ns = trace.emit('analysis_restart', scenario=current.name)
        select(current)
        start_bridge('restart')
        finish(current)
        current = Scenario('warm_retained', current.sid, list(current.rows))
        append(current, 1)
        collect(current)
        finish(current)
        current = Scenario('new_session_retained', str(uuid.uuid4()))
        add_session(home, current.sid, 'New session beside retained history')
        append(current, 1)
        select(current)
        collect(current)
        finish(current)
        stop_bridge()
        if not args.skip_growth:
            # New owners exceed the production cohort size without changing it.
            for number in range(17):
                owner = Scenario('growth_seed', str(uuid.uuid4()))
                add_session(home, owner.sid, 'Conversion benchmark session ' + str(number))
                append_rows(home, owner, args.rows, trace)
            current = Scenario('growth_seed', current.sid, list(current.rows))
            collect(current, owners=args.owners + 18)
            entered, release = context.Event(), context.Event()
            quota = directory / 'growth-quota.sqlite'
            start_bridge('growth', entered, release)
            try:
                wait_for(entered.is_set, 'incomplete committed conversion cohort')
                current = Scenario('growth_during_conversion', current.sid, list(current.rows))
                append(current, 1)
                collect(current)
            finally:
                release.set()
            finish(current)
        report['work'] = dict(scenarios=len(report['samples']),
                              checked_calls=sum(s['calls'] for s in report['samples']),
                              model_requests=0, failures=len(report['errors']))
        report['renderer'] = str(controller.widget.quick.quickWindow().rendererInterface().graphicsApi())
    finally:
        stop_bridge()
        OverlayContent.paint = paint
        analysis_worker.SnapshotReceiver = receiver
        controller.stop()
        host.close()
        app.processEvents()
        if collector.pid and collector.is_alive():
            parent.send(dict(kind='stop'))
            collector.join(min(10, args.deadline))
            if collector.is_alive():
                collector.terminate()
                collector.join(5)
                report['errors'].append('collector required termination')
        parent.close()
        child.close()
        assert not collector.is_alive()


def code_identity(directory, name='sources.json'):
    def git(*values):
        return subprocess.check_output(['git', '-C', str(ROOT), *values])
    paths = git('ls-files', '-z', 'cachemonitor', 'native', 'requirements-release.lock').decode().split('\0')
    paths.extend(str(path.relative_to(ROOT)).replace('\\', '/')
                 for path in (ROOT / 'cachemonitor').rglob('*')
                 if path.suffix in ('.py', '.qml'))
    paths.append('tools/benchmark_overlay_latency.py')
    hashes = {path:hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
              for path in sorted(set(paths)) if path and (ROOT / path).is_file()}
    (directory / name).write_text(json.dumps(hashes, sort_keys=True, indent=2), encoding='utf-8')
    native = ROOT / 'cachemonitor/_cpu_native.pyd'
    receipt = ROOT / 'cachemonitor/_cpu_native.build.json'
    return dict(head=git('rev-parse', 'HEAD').decode().strip(),
                tree=git('rev-parse', 'HEAD^{tree}').decode().strip(),
                diff_sha256=hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest(),
                source_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                native_sha256=hashlib.sha256(native.read_bytes()).hexdigest(),
                native_receipt=json.loads(receipt.read_text(encoding='utf-8')))


def summarize_trace(directory, report):
    events = []
    for path in directory.glob('*.jsonl'):
        events.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines())
    for sample in report['samples']:
        fingerprint = [None, None, sample['calls'], sample['output']['latest'].get('key')]
        for value in sorted(events, key=lambda row:row['ns']):
            if value['stage'] == 'analysis_publication' and value['ns'] >= sample['source_ns']:
                if sample['unattributed'] and value.get('source_at', 0) >= sample['source_at']:
                    sample['stage_ms']['analysis'] = (value['ns'] - sample['source_ns']) / 1e6
                    break
                if (not sample['unattributed'] and any(row[2:] == fingerprint[2:] for row in value.get('summaries', []))
                        and value.get('revisions', {}).get(sample['output']['latest']['sid']) == json.loads(json.dumps(sample['output']['revision']))):
                    sample['stage_ms']['analysis'] = (value['ns'] - sample['source_ns']) / 1e6
                    break
    report['work'] = dict(scenarios=len(report['samples']),
        checked_calls=sum(s['calls'] for s in report['samples']),
        generated_calls=sum(e.get('appended', 0) for e in events if e['stage'] == 'source_event'),
        collector_publications=sum(e['stage'] == 'collector_publication' for e in events),
        model_requests=0, failures=len(report['errors']))
    report['cpu_stages'] = [e for e in events if e['stage'] == 'cpu_stages']
    report['interrupted_profile_cycles'] = sum(e['outcome'] != 'complete' for e in report['cpu_stages'])
    report['profiles'] = [str(path.name) for path in directory.glob('*.pstats')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/overlay-latency/measurement')
    parser.add_argument('--owners', type=int, default=3)
    parser.add_argument('--rows', type=int, default=8)
    parser.add_argument('--deadline', type=float, default=90)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--collector', choices=('entry', 'watcher'), default='watcher')
    parser.add_argument('--skip-growth', action='store_true')
    args = parser.parse_args()
    if args.owners < 1 or args.rows < 2 or args.deadline <= 0:
        parser.error('owners >= 1, rows >= 2, and deadline > 0 are required')
    if os.name != 'nt':
        parser.error('this benchmark requires native Windows Qt rendering')
    output = args.output.resolve()
    allowed = (ROOT / 'artifacts/overlay-latency/measurement').resolve()
    if not output.is_relative_to(allowed):
        parser.error('--output must remain inside artifacts/overlay-latency/measurement')
    directory = output / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    isolation = directory / 'profile'
    isolation.mkdir()
    for key in ('HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'CODEX_HOME', 'TEMP', 'TMP'):
        target = isolation / key.lower()
        target.mkdir()
        os.environ[key] = str(target)
    report = dict(version=1, mode='profile' if args.profile else 'latency',
        claim='Source file close to matching native Qt paint and afterRendering; triggered collector poll; explicit owned-window selection.',
        excludes=['source-watcher scheduling', 'Codex route discovery', 'physical presentation', 'upstream model timing'],
        collector=args.collector, simulated_model_timings=True,
        identity_limit='Global attempts without source-bound response identities do not identify a selected owner. Their native cost and speed stay unknown.',
        checkout=str(ROOT), python=sys.executable, owners=args.owners, rows=args.rows,
        lock_sha256=hashlib.sha256((ROOT / 'requirements-release.lock').read_bytes()).hexdigest(),
        samples=[], errors=[], host_before=host_snapshot())
    try:
        report['head'] = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        report['code'] = code_identity(directory)
        if args.collector == 'watcher':
            report['claim'] = 'Source file or evidence commit through normal collector watcher loop to matching native Qt paint and afterRendering; explicit owned-window selection.'
            report['excludes'].remove('source-watcher scheduling')
        run(args, directory, report)
    except BaseException:
        report['errors'].append(traceback.format_exc())
    finally:
        report['host_after'] = host_snapshot()
        report['failure_count'] = len(report['errors'])
        report['code_after'] = code_identity(directory, name='sources-after.json')
        if report.get('code') != report['code_after']:
            report['errors'].append('source identity changed during measurement')
        summarize_trace(directory, report)
        report['failure_count'] = len(report['errors'])
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(str(directory / 'report.json'), flush=True)
    return int(bool(report['errors']))


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
