"""Trace isolated source-file changes through normal analysis and native painting.

Scenarios use UUID sessions and exact expected call rows. Timed runs measure
source/evidence writes or analysis restart to Qt afterRendering after an exact OverlayContent
paint. The default collector runs its normal source-watcher service loop.
--collector entry instead triggers CollectorService.poll explicitly. The target is an owned QWidget with an explicit local
Selection. Codex route discovery, physical monitor presentation, network calls,
and upstream model generation are outside this claim.

Run with the locked release interpreter through tools/run_ui_checks.py. Use a
separate --profile run for cProfile and production wall/thread CPU stage records.
Controlled overlap cases pause aggregate conversion or an executing comparison
or trend query. Selected source changes must reach native painting before each
local gate opens. These cases prove overlap and cancellation, not timing speedups.
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

HARNESS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS_ROOT))
from tools.benchmark_cumulative import query_shapes
from tools.benchmark_overlay_worker import QueryOverlap, Trace, analysis_process, fingerprint

CHECKOUT_PARSER = argparse.ArgumentParser(add_help=False)
CHECKOUT_PARSER.add_argument('--checkout', type=Path)
CHECKOUT_ARGS, _ = CHECKOUT_PARSER.parse_known_args()
ROOT = Path(CHECKOUT_ARGS.checkout or os.environ.get('CODEXON_OVERLAY_BENCHMARK_CHECKOUT')
            or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(ROOT))


def collector_process(connection, home, index, evidence, trace_path, profile, mode):
    from cachemonitor.model_evidence import EvidenceStore
    EvidenceStore(evidence).close()
    if mode == 'watcher':
        return watcher_process(connection, home, index, evidence, trace_path, profile)
    from cachemonitor import usage_collection
    from cachemonitor.collection_replay import EncodedSession
    import zlib
    CollectorService = usage_collection.CollectorService
    trace = Trace(trace_path)
    module_path = Path(usage_collection.__file__).resolve()
    assert module_path.parents[1] == ROOT, (module_path, ROOT)
    trace.emit('product_import', role='collector', checkout=str(ROOT), module_path=str(module_path))
    service = None
    try:
        service = CollectorService([home], index, evidence)
        ready = service.poll(detailed=False)
        trace.emit('collector_ready', demand=ready.get('demand'),
                   evidence_highwater=ready.get('activity_source_limit'),
                   evidence_offset=ready.get('activity_source_offset'))
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
                value = service.poll(detailed=False)
                polls += 1
                sessions = {s['id']: (dict(json.loads(zlib.decompress(s.payload)), **s)
                    if isinstance(s, EncodedSession) else s) for s in value['sessions']}
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
                               demand=value.get('demand'),
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


def collected_matches(snapshot, selected, command, *, observed_activity=None):
    return bool(selected.get('collection_complete')
                and snapshot.get('demand', {}).get('aggregate') == command.get('aggregate', False)
                and (not command.get('owners') or snapshot['usage_collection_complete']
                     and not snapshot['index'].get('loading'))
                and len(snapshot['sessions']) >= command.get('owners', 0)
                and len(selected.get('history', ())) == command['calls']
                and (not command.get('activity') or any(
                    all(row.get(k) == v for k, v in command['activity'].items())
                    for row in (snapshot.get('request_activity', ()) if observed_activity is None else observed_activity)))
                and all(selected['history'][-1].get(k) == v
                        for k, v in command.get('evidence', {}).items()))


def watcher_process(connection, home, index, evidence, trace_path, profile, *, homes=None):
    from cachemonitor import usage_collection
    from cachemonitor.collection_replay import EncodedSession
    import zlib
    trace = Trace(trace_path)
    module_path = Path(usage_collection.__file__).resolve()
    assert module_path.parents[1] == ROOT, (module_path, ROOT)
    trace.emit('product_import', role='collector', checkout=str(ROOT), module_path=str(module_path))
    send_lock = threading.Lock()
    expectation_lock = threading.Lock()
    expectation = [None]
    stopping = threading.Event()
    started = False
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
                command['observed_activity'] = []
                command['activity_observed_ns'] = None
                expectation[0] = command
            send(dict(kind='armed', scenario=command['scenario']))

    def poll(service, *values, **kwargs):
        nonlocal started
        cpu = time.process_time()
        trace.emit('collector_entry')
        value = original_poll(service, *values, **kwargs)
        if not started:
            trace.emit('collector_ready', demand=value.get('demand'),
                       evidence_highwater=value.get('activity_source_limit'),
                       evidence_offset=value.get('activity_source_offset'))
            send(dict(kind='started', pid=os.getpid()))
            started = True
        stamp = time.perf_counter_ns()
        with expectation_lock:
            command = expectation[0]
        if command is not None:
            selected = next((row for row in value['sessions'] if row['id'] == command['sid']), {})
            if isinstance(selected, EncodedSession):
                selected = dict(json.loads(zlib.decompress(selected.payload)), **selected)
            if command.get('activity'):
                matches = [row for row in value.get('request_activity', ())
                    if all(row.get(key)==expected for key,expected in command['activity'].items())]
                if matches:
                    command['observed_activity'] = matches
                    if command['activity_observed_ns'] is None:
                        command['activity_observed_ns'] = stamp
                        trace.emit('collector_activity',scenario=command['scenario'],observed_ns=stamp,
                            complete=value['usage_collection_complete'],calls=len(selected.get('history',())))
            if collected_matches(value, selected, command, observed_activity=command['observed_activity']):
                trace.emit('collector_publication', scenario=command['scenario'],
                           published_ns=stamp, owners=len(value['sessions']), source_epoch=service.epoch,
                           demand=value.get('demand'),
                           selected=fingerprint(dict(selected, calls=len(selected.get('history', ())),
                               latest_key=selected['history'][-1]['key'] if selected.get('history') else None)),
                           cpu_seconds=time.process_time() - cpu,
                           source_hint=bool(kwargs.get('source_hint')),
                           activity_observed_ns=command['activity_observed_ns'])
                with expectation_lock:
                    if expectation[0] is command:
                        expectation[0] = None
                send(dict(kind='collected', ns=stamp, selected=selected,
                          owners=len(value['sessions']), errors=value['errors'], source_at=value['ts'],
                          activity=(command['observed_activity'] if command.get('activity')
                              else value.get('request_activity', [])),
                          activity_observed_ns=command['activity_observed_ns']))
        return value

    usage_collection.CollectorService.poll = poll
    usage_collection.CollectorService.stopping = lambda service: stopping.is_set() or original_stopping(service)
    controls_thread = threading.Thread(target=controls, name='benchmark-control', daemon=True)
    controls_thread.start()
    sys.argv = ['usage-collector', *[part for item in (homes or [home])
        for part in ('--codex-home', str(item))], '--index-path', str(index),
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
    aggregate: bool = False

    def matches(self, data, home):
        return bool(self.source_ns and self.rows and fingerprint(data) == self.identity(home)
                    and (self.previous_revision is None
                         or tuple(data.get('_overlay_revision') or ()) != self.previous_revision))

    def matches_snapshot(self, value, home):
        if self.unattributed:
            return bool(self.source_at and value['ts'] >= self.source_at
                        and all(self.empty_selected(s, home) for s in value.get('overlay_sessions', ())
                                if s['id'] == self.sid))
        return any(self.matches(s, home) for s in value.get('overlay_sessions', ()))

    def empty_selected(self, data, home):
        return (data is None or fingerprint(data) == [str(home), self.sid, 0, None]
                and not any(data.get(key) for key in ('latest', 'recent')))

    def identity(self, home):
        return [str(home), self.sid, len(self.rows), self.rows[-1]['key']]


def add_session(home, sid, title, *, started_at=None):
    path = home / 'sessions' / (sid + '.jsonl')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                   (sid, str(path), int(time.time()), title, title, 'gpt-6.1-sol', 'openai', str(home)))
    path.write_text(json.dumps(event('session_meta', time.time() - 100 if started_at is None else started_at, id=sid)) + '\n', encoding='utf-8')
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
        return (scenario.empty_selected(data, home) and (data is None or not data.get('_recent_refs'))
                and scenario.collector_ns is not None and scenario.bridge_ns is not None)
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


def native_application():
    from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
    configure_font_rendering()
    configure_high_dpi()
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    load_bundled_fonts()
    return app


class NativeOverlayTarget:
    def __init__(self, directory, app):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QWidget
        from cachemonitor.overlay import OverlayController
        from cachemonitor.overlay_windows import WindowsOverlay
        self.app = app
        self.host = QWidget()
        self.host.setWindowTitle('Codexon isolated overlay latency target')
        self.host.resize(1100, 900)
        self.host.show()
        self.native = WindowsOverlay()
        self.native.visible_target = lambda hwnd:bool(self.native.u.IsWindowVisible(hwnd) and not self.native.u.IsIconic(hwnd))
        self.controller = OverlayController(QSettings(str(directory/'overlay.ini'), QSettings.IniFormat),
                                           native_enabled=False, appearance_path=directory/'appearance.toml')
        self.controller.native = self.native
        self.native.configure(int(self.controller.widget.winId()))

    def select(self, sid):
        from cachemonitor.overlay_tracking import Selection
        self.controller.receive_target(dict(target=dict(hwnd=int(self.host.winId())),
                                            selection=Selection(sid), confirmed=True))

    def capture(self, path):
        controller = self.controller
        assert controller.widget.isVisible() and self.native.u.IsWindowVisible(int(controller.widget.winId()))
        assert not any(w.qml_errors for w in (controller.widget, *controller.chrome))
        image = controller.widget.quick.grabFramebuffer()
        assert not image.isNull() and image.width() > 0 and image.height() > 0, (
            image.isNull(), image.width(), image.height(),
            controller.widget.quick.width(), controller.widget.quick.height())
        colors = {image.pixelColor(x, y).rgba() for x in range(0, image.width(), 7)
                  for y in range(0, image.height(), 7)}
        assert len(colors) > 10, 'native frame contains no rendered overlay content'
        assert image.save(str(path))
        return hashlib.sha256(bytes(image.constBits())).hexdigest()

    def stop(self):
        self.controller.stop()
        self.host.close()
        self.app.processEvents()


def run(args, directory, report):
    app = native_application()
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from cachemonitor import analysis_worker
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.usage_collection import CollectionClient
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
    surface = NativeOverlayTarget(directory, app)
    controller, native = surface.controller, surface.native
    bridge = None
    query_overlaps = ()
    demand = CollectionClient([str(home)], index, evidence)
    current = None
    connected = False
    paint = OverlayContent.paint
    receiver = analysis_worker.SnapshotReceiver

    class Receiver(receiver):
        def receive(self, value):
            result = super().receive(value)
            if current is not None and result is not None:
                if not current.matches_snapshot(result, home):
                    trace.emit('snapshot_rejected', scenario=current.name, source_at=result['ts'], expected_at=current.source_at,
                        summaries=[dict(identity=fingerprint(s), recent_refs=s.get('_recent_refs'), empty=current.empty_selected(s, home))
                                   for s in result.get('overlay_sessions', ())])
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
                    trace.emit('wait_timeout', label=label, data=controller.widget.content_model.data,
                        state=controller.widget.content_model.state, collection=controller.collection_evidence,
                        visible=controller.widget.isVisible(), qml_errors=controller.widget.qml_errors)
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
        demand.set_demand([(None, scenario.sid)], aggregate=bool(owners))
        command = dict(kind='poll', scenario=scenario.name, sid=scenario.sid,
                       calls=len(scenario.rows) + count, owners=owners, aggregate=bool(owners or scenario.aggregate),
                       deadline=args.deadline, activity=scenario.activity,
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
        if args.collector == 'entry':
            demand.set_demand([(None, scenario.sid)], aggregate=bool(owners))
        if args.collector == 'watcher' and armed[0] != (scenario, owners):
            arm(scenario, owners=owners)
        if args.collector == 'entry':
            parent.send(dict(kind='poll', scenario=scenario.name, sid=scenario.sid,
                             calls=len(scenario.rows), owners=owners, aggregate=bool(owners or scenario.aggregate),
                             deadline=args.deadline, activity=scenario.activity,
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
        if owners:
            demand.set_demand([(None, scenario.sid)])
        return value

    def start_bridge(label, entered=None, release=None, refresh=None, query_overlaps=()):
        nonlocal bridge
        analysis_worker.process_main = partial(analysis_process,
            trace_path=directory / (label + '-analysis.jsonl'), root=ROOT, profile=args.profile,
            entered=entered, release=release, refresh=refresh, query_overlaps=query_overlaps)
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
        controller.selection_loader = bridge.select_session
        selection = controller.target_state.get('selection')
        controller.selection_loader(selection.thread_id if selection and selection.host == 'local' else None)
        bridge.start()

    def stop_bridge():
        nonlocal bridge
        if bridge is not None:
            controller.selection_loader = controller.overlay_loader = None
            bridge.requestInterruption()
            wait_for(lambda: not bridge.isRunning(), 'analysis shutdown', check_errors=False)
            assert bridge.process is None or not bridge.process.is_alive()
            bridge.wait()
            bridge = None

    def finish(scenario):
        wait_for(lambda: scenario.frame_ns is not None, scenario.name + ' native frame')
        data = controller.widget.content_model.data
        assert scenario.source_ns <= scenario.content_ns <= scenario.paint_ns <= scenario.frame_ns
        assert check_output(data, scenario, home)
        frame_hash = surface.capture(directory/(scenario.name + '.png'))
        if scenario.evidence:
            speed = next(item for item in controller.widget.content_model.headline_metrics() if item['id'] == 'speed')
            expected = scenario.evidence['output_speed']
            assert speed['number'] == ('—' if expected is None else f'{expected:,.1f}')
        if scenario.unattributed:
            metrics = {item['id']:item['number'] for item in controller.widget.content_model.headline_metrics()}
            assert controller.widget.content_model.latest_call() == {}
            assert metrics['cost'] == metrics['speed'] == '—'
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
            surface.select(current.sid)
            if bridge is None:
                start_bridge('unattributed')
            observe(current, status, current.activity['response_id'])
        stop_bridge()
        fresh = Scenario('fresh', str(uuid.uuid4()))
        add_session(home, fresh.sid, 'Fresh benchmark session')
        current = fresh
        surface.select(current.sid)
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
        surface.select(current.sid)
        start_bridge('restart')
        finish(current)
        current = Scenario('warm_retained', current.sid, list(current.rows))
        append(current, 1)
        collect(current)
        finish(current)
        current = Scenario('new_session_retained', str(uuid.uuid4()))
        add_session(home, current.sid, 'New session beside retained history')
        append(current, 1)
        surface.select(current.sid)
        collect(current)
        finish(current)
        stop_bridge()
        expected_calls = args.owners * args.rows + 2
        if not args.skip_growth:
            from cachemonitor.prepared_store import Limits
            growth_owners=Limits().owners_per_batch+1
            for number in range(growth_owners):
                owner = Scenario('growth_seed', str(uuid.uuid4()))
                add_session(home, owner.sid, 'Conversion benchmark session ' + str(number))
                append_rows(home, owner, args.rows, trace)
            current = Scenario('growth_seed', current.sid, list(current.rows))
            collect(current, owners=args.owners + growth_owners + 1)
            entered, release, refresh = (context.Event() for _ in range(3))
            quota = directory / 'growth-quota.sqlite'
            current.source_ns = trace.emit('analysis_restart', scenario=current.name)
            start_bridge('growth', entered, release, refresh)
            wait_for(lambda: current.bridge_ns is not None and current.frame_ns is not None,
                     'selected native frame before aggregate preparation')
            aggregate_results = []
            bridge.result.connect(aggregate_results.append)
            now = time.time()
            bridge.request('growth-aggregate', dict(page=1, start=0, end=now + 60, now=now + 60,
                period='custom', category_usage=True, usage=dict(dimension='model', metric='calls')), ())
            trace.emit('aggregate_requested', scenario='growth_during_conversion')
            try:
                wait_for(entered.is_set, 'incomplete committed aggregate conversion cohort')
                current = Scenario('growth_during_conversion', current.sid, list(current.rows), aggregate=True)
                append(current, 1)
                collect(current)
                refresh.set()
                finish(current)
                assert not release.is_set() and not aggregate_results
                trace.emit('selected_live_during_aggregate', scenario=current.name)
            finally:
                release.set()
                refresh.set()
            wait_for(lambda: bool(aggregate_results), 'aggregate preparation after gate release')
            result = aggregate_results[-1]
            assert result['id'] == 'growth-aggregate', result
            expected_calls = (args.owners + growth_owners) * args.rows + 3
            assert result['result']['analysis']['response_count'] == expected_calls, result
            trace.emit('aggregate_completed', scenario=current.name, request=result['id'], calls=expected_calls)
            report['samples'][-1]['controlled_overlap'] = dict(kind='aggregate_conversion', terminal='completed')
            bridge.cancel_queries()
        stop_bridge()
        query_overlaps = tuple(QueryOverlap(kind, kind + '-overlap',
            entered=context.Event(), release=context.Event(), cancelled=context.Event(),
            finished=context.Event(), deadline=args.deadline) for kind in ('comparison', 'trend'))
        start_bridge('query-overlap', query_overlaps=query_overlaps)
        query_results, stale_results = [], []
        bridge.result.connect(query_results.append)
        bridge.stale.connect(stale_results.append)
        now = time.time()
        _, comparison, trend, _ = query_shapes(320)
        comparison = dict(comparison, start=0, end=now + 60, now=now + 60)
        trend = dict(trend, start=now - 7 * 86400, end=now + 60, now=now + 60,
                     time_range=[now - 7 * 86400, now + 60])
        for overlap, query in zip(query_overlaps, (comparison, trend)):
            bridge.request(overlap.request, query, ())
            wait_for(overlap.entered.is_set, overlap.kind + ' query acquired its inputs')
            current = Scenario('selected_during_' + overlap.kind, current.sid, list(current.rows), aggregate=True)
            append(current, 1)
            collect(current)
            finish(current)
            expected_calls += 1
            assert not overlap.release.is_set() and not overlap.finished.is_set()
            assert not any(value['id'] == overlap.request for value in query_results + stale_results)
            trace.emit('selected_live_during_query', scenario=current.name, request=overlap.request,
                       boundary=overlap.boundary, controlled_overlap=True)
            bridge.cancel_queries()
            wait_for(overlap.cancelled.is_set, overlap.kind + ' query cancellation observed')
            overlap.release.set()
            wait_for(overlap.finished.is_set, overlap.kind + ' query unwound after cancellation')
            replacement = overlap.request + '-current'
            bridge.request(replacement, query, ())
            wait_for(lambda: any(value['id'] == replacement for value in query_results),
                     overlap.kind + ' current-generation query completion')
            result = next(value for value in query_results if value['id'] == replacement)
            value = result['result']['performance' if overlap.kind == 'trend' else 'usage']
            calls = value['calls'] if overlap.kind == 'trend' else value['metrics']['calls']['value']
            assert calls == expected_calls, result
            assert not any(value['id'] == overlap.request for value in query_results + stale_results)
            assert check_output(controller.widget.content_model.data, current, home)
            report['samples'][-1]['controlled_overlap'] = dict(kind=overlap.kind, boundary=overlap.boundary,
                terminal='cancelled', replacement=replacement, replacement_calls=calls)
            trace.emit('controlled_query_current_completed', scenario=current.name, request=replacement, calls=calls)
            bridge.cancel_queries()
        report['work'] = dict(scenarios=len(report['samples']),
                              checked_calls=sum(s['calls'] for s in report['samples']),
                              model_requests=0, failures=len(report['errors']))
        report['renderer'] = str(controller.widget.quick.quickWindow().rendererInterface().graphicsApi())
    finally:
        for overlap in query_overlaps:
            overlap.release.set()
        stop_bridge()
        demand.set_demand()
        demand.close()
        OverlayContent.paint = paint
        analysis_worker.SnapshotReceiver = receiver
        surface.stop()
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
    hashes = {path:hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
              for path in sorted(set(paths)) if path and (ROOT / path).is_file()}
    (directory / name).write_text(json.dumps(hashes, sort_keys=True, indent=2), encoding='utf-8')
    native = ROOT / 'cachemonitor/_cpu_native.pyd'
    receipt = ROOT / 'cachemonitor/_cpu_native.build.json'
    return dict(head=git('rev-parse', 'HEAD').decode().strip(),
                tree=git('rev-parse', 'HEAD^{tree}').decode().strip(),
                diff_sha256=hashlib.sha256(git('diff', '--binary', 'HEAD', '--',
                    'cachemonitor', 'native', 'requirements-release.lock')).hexdigest(),
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
    report['controlled_query_events'] = sorted((e for e in events if e['stage'].startswith('controlled_query_')
        or e['stage'] == 'selected_live_during_query'), key=lambda row: row['ns'])
    report['product_imports'] = [e for e in events if e['stage'] == 'product_import']
    report['cpu_stages'] = [e for e in events if e['stage'] == 'cpu_stages']
    report['interrupted_profile_cycles'] = sum(e['outcome'] != 'complete' for e in report['cpu_stages'])
    report['profiles'] = [str(path.name) for path in directory.glob('*.pstats')]


def harness_identity():
    paths = [HARNESS_ROOT / 'tools' / name for name in
             ('benchmark_overlay_latency.py', 'benchmark_overlay_worker.py', 'benchmark_cumulative.py')]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, default=ROOT)
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
    os.environ['CODEXON_OVERLAY_BENCHMARK_CHECKOUT'] = str(ROOT)
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
        harness=harness_identity(),
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
        report['harness_after'] = harness_identity()
        if report['harness'] != report['harness_after']:
            report['errors'].append('benchmark script changed during measurement')
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
