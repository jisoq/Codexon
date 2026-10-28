"""Observe synthetic historical warning recovery through normal analysis and native frames."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.benchmark_overlay_latency import (
    NativeOverlayTarget, Scenario, Trace, add_session, analysis_process,
    check_output, code_identity, event, fingerprint, host_snapshot, native_application,
    watcher_process,
)


def create_home(home):
    home.mkdir(parents=True)
    (home/'sessions').mkdir()
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,updated_at INTEGER,name TEXT,title TEXT,model TEXT,model_provider TEXT,cwd TEXT)')
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,target TEXT,thread_id TEXT,process_uuid TEXT,feedback_log_body TEXT)')


def write_raw(home, sid, plans):
    with (home/'sessions'/(sid+'.jsonl')).open('a', encoding='utf-8') as output:
        for plan in plans:
            tier = 'default' if plan['condition'][2] == 'Standard' else 'priority'
            output.write(json.dumps(event('turn_context', plan['at']-.00001, turn_id=plan['turn'],
                model=plan['condition'][0], effort=plan['condition'][1], service_tier=tier))+'\n')
            output.write(json.dumps(event('token_usage_record', plan['at'], thread_id=sid,
                turn_id=plan['turn'], response_id=plan['key'], model=plan['condition'][0], service_tier=tier,
                usage=dict(input_tokens=plan['input'], cached_input_tokens=plan['cached'],
                    cache_write_input_tokens=0, output_tokens=plan['output'], reasoning_output_tokens=10)))+'\n')


def numeric_rows(plans):
    return [dict(key=p['key'], input=p['input'], cached=p['cached'], written=0,
                 output=p['output'], reasoning=10) for p in plans]


def expected_owner(home, sid, plans):
    buckets = {}
    for plan in plans:
        if not plan['eligible']:
            continue
        condition = tuple(plan['condition'])
        count, size = buckets.get(condition, (0,0))
        buckets[condition] = count+1, max(size,sys.getsizeof(plan['key']))
    return dict(owner=[str(home),sid], rows=len(plans), buckets=[
        dict(condition=list(condition), rows=count, max_key_getsizeof=size)
        for condition,(count,size) in sorted(buckets.items())])


def create_fixture(directory, shape):
    from cachemonitor.model_evidence import EvidenceStore
    history, control = directory/'h', directory/'w'
    create_home(history)
    create_home(control)
    expected = []
    store = EvidenceStore(directory/'evidence.sqlite')
    count = valid = 0
    warm_condition = None
    base = time.time()-86400
    try:
        for number in range(shape['public_owners']):
            sid = str(uuid.uuid4())
            add_session(history,sid,'Synthetic retained owner '+str(number),started_at=base-1)
            profile = (shape['owner_profiles'][number] if shape.get('owner_profiles') else
                dict(rows=shape['canonical_rows']//shape['public_owners']
                     +int(number < shape['canonical_rows']%shape['public_owners'])))
            buckets = ([(b['condition'],b.get('key_chars',shape['key_chars']))
                        for b in profile.get('buckets',()) for _ in range(b['rows'])]
                       if shape.get('owner_profiles') else None)
            plans = []
            for ordinal in range(profile['rows']):
                eligible = (ordinal < len(buckets) if buckets is not None else
                    (count+1)*shape['completed_rows']//shape['canonical_rows']
                    > count*shape['completed_rows']//shape['canonical_rows'])
                condition, chars = (buckets[ordinal] if buckets is not None and eligible else
                    (['gpt-6.1-sol','high','Standard','WebSocket',1,0,True],shape['key_chars']))
                inp = (8000,20000,75000,150000,230000,300000)[condition[4]]
                plan = dict(key=f'{count:x}'.zfill(chars), turn=uuid.uuid5(uuid.UUID(sid),str(ordinal)).hex,
                    at=base+count/100, condition=condition, input=inp,
                    cached=int(inp*(.8 if condition[6] else .5)),
                    output=(256,1024,4096,10000)[condition[5]], elapsed=1000.,eligible=eligible)
                plans.append(plan)
                if eligible:
                    if warm_condition is None and condition[0] == 'gpt-6.1-sol' and condition[2] == 'Standard':
                        warm_condition = condition
                    completed_observation(store,history,'history-'+f'{count:x}',plan['key'],plan['at'],
                        model=condition[0],effort=condition[1],tier=condition[2],transport=condition[3])
                    valid += 1
                count += 1
            write_raw(history,sid,plans)
            expected.append(expected_owner(history,sid,plans))
        if warm_condition is None:
            raise ValueError('descriptor has no populated bulk scope supported by the literal warm price oracle')
        target, peer = str(uuid.uuid4()), str(uuid.uuid4())
        now = time.time()
        add_session(control,target,'Numeric and active-warning control',started_at=now-130)
        add_session(control,peer,'Eight healthy baseline calls',started_at=now-130)
        condition = ['gpt-6.1-sol','high','Standard','WebSocket',1,0,True]
        peers = [dict(key=peer+'-response-'+str(i),turn=peer+'-turn-'+str(i),at=now-110+i,
            condition=condition,input=10000,cached=8000,output=256,elapsed=1000.,eligible=True) for i in range(8)]
        initial = [dict(key=target+'-response-'+str(i),turn=target+'-turn-'+str(i),
            at=now-120+i if i < 2 else now-102+i,condition=condition,
            input=10000+i if i < 2 else 10000,cached=8000,output=100+i if i < 2 else 256,
            elapsed=1000. if i < 2 else 4000.,eligible=i >= 2) for i in range(5)]
        later = dict(key=target+'-response-5',turn=target+'-turn-5',at=now-90,
            condition=condition,input=10000,cached=8000,output=256,elapsed=4000.,eligible=True)
        for sid,plans in ((peer,peers),(target,initial)):
            write_raw(control,sid,plans)
            for plan in plans:
                completed_observation(store,control,'control-'+plan['key'],plan['key'],plan['at'],
                                      completion_ms=plan['elapsed'])
            expected.append(expected_owner(control,sid,plans))
    finally:
        store.close()
    assert count == shape['canonical_rows'] and valid == shape['completed_rows']
    after = [expected_owner(control,target,[*initial,later]) if row['owner'][1] == target else row for row in expected]
    warm_sid = str(uuid.uuid4())
    warm_input = (8000,20000,75000,150000,230000,300000)[warm_condition[4]]
    warm_plan = dict(key=warm_sid+'-response-0',turn=warm_sid+'-turn-0',at=now+1,
        condition=warm_condition,input=warm_input,cached=int(warm_input*(.8 if warm_condition[6] else .5)),
        output=(256,1024,4096,10000)[warm_condition[5]],elapsed=1000.,eligible=True)
    manifest = dict(shape=shape,homes=[str(history),str(control)],selected_home=str(control),sid=target,
        initial=initial,after_during=[*initial,later],during_append=later,peer_keys=[p['key'] for p in peers],
        expected=dict(initial=expected,after_during=after),
        warm=dict(home=str(history),sid=warm_sid,plan=warm_plan,
                  expected=expected_owner(history,warm_sid,[warm_plan])),
        extras=dict(owners=2,initial_rows=13,initial_eligible=11,initial_ineligible=2,
                    after_during_rows=14,after_during_eligible=12),
        identities=dict(home_chars=[len(str(home)) for home in (history,control)],
            home_getsizeof=[sys.getsizeof(str(home)) for home in (history,control)],sid_chars=36),
        model_requests=0,simulated_model_timings=True)
    (directory/'fixture.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def prepare_fixture(connection, fixture):
    from dataclasses import replace
    from cachemonitor.prepared_ingest import PreparedCohort, pages, read_catalog, source_identity
    from cachemonitor.prepared_store import Limits, PreparedStore
    from cachemonitor.quota_cycles import QuotaLedger
    from cachemonitor.usage_collection import CollectionClient, CollectorService
    manifest = json.loads((fixture/'fixture.json').read_text(encoding='utf-8'))
    index,evidence,quota = (fixture/name for name in ('index.sqlite','evidence.sqlite','quota.sqlite'))
    deadline = time.monotonic()+360
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('pre-clock canonical preparation deadline')
    try:
        with ExitStack() as cleanup:
            service = CollectorService(manifest['homes'],index,evidence)
            cleanup.callback(service.close)
            while True:
                check()
                value = service.poll()
                if value.get('usage_collection_complete') and not value['index'].get('loading'):
                    break
            assert not value['errors'],value['errors']
            client = CollectionClient(manifest['homes'],index,evidence)
            cleanup.callback(client.close)
            ledger = QuotaLedger(quota)
            cleanup.callback(ledger.close)
            ledger.scan_modes(manifest['homes'])
            store = cleanup.enter_context(PreparedStore(quota.with_suffix('.prepared.sqlite'),
                replace(Limits(),read_seconds=60,read_vm_steps=20_000_000)))
            catalog = read_catalog(client,cancel=check)
            cohort = PreparedCohort(store,client,ledger,cancel=check,speed_writer=None)
            def progress(event):
                with store.read_current(seconds=60) as view:
                    entries=list(pages(view.inventory))
                    prepared=[row for row in entries if row['dependencies'].get('prepared')]
                    value=dict(event=event,at=time.time(),token=[view.token.instance,view.token.generation],
                        ready=view.ready,prepared_owners=len(prepared),
                        prepared_calls=sum(row['dependencies'].get('call_count',0) for row in prepared),
                        metrics=dict(cohort.metrics))
                with (fixture/'preparation-progress.jsonl').open('a',encoding='utf-8') as output:
                    output.write(json.dumps(value)+'\n')
            progress('window_start')
            try:
                for number,change in enumerate(cohort.convert(catalog),1):
                    check()
                    if number%8 == 0 or change['ready']:
                        progress('cohort_commit')
                    while store.maintain():
                        check()
            finally:
                progress('window_end')
            with store.read_current(seconds=60) as view:
                owners = [r for r in pages(view.catalog) if r['dependencies'].get('prepared')
                          and not r['metadata'].get('_internal_review')]
                assert len(owners) == len(manifest['expected']['initial'])
                assert sum(r['dependencies']['call_count'] for r in owners) == sum(r['rows'] for r in manifest['expected']['initial'])
                assert view.source_identity == source_identity(catalog)
                assert view.checkpoint_state('speed-health-v2') is None
                assert store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0] == 1
                result = dict(token=[store.token.instance,store.token.generation],metrics=cohort.metrics,
                              source=source_identity(catalog),checkpoint_absent=True)
        (fixture/'preparation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        connection.send(dict(kind='prepared',result=result))
    except BaseException:
        connection.send(dict(kind='error',error=traceback.format_exc()))
    finally:
        connection.close()


def proof_audit(proofs, expected):
    actual = {owner:dict(rows=proof.length,buckets={condition:(count,size)
        for condition,(count,size) in proof.buckets.items()}) for owner,proof in proofs.items()}
    wanted = {tuple(r['owner']):dict(rows=r['rows'],buckets={tuple(b['condition']):(b['rows'],b['max_key_getsizeof'])
        for b in r['buckets']}) for r in expected}
    assert actual.keys() == wanted.keys(),'recovery proof owner membership differs from raw-input descriptor'
    mismatches = [dict(owner=owner,actual=actual[owner],expected=wanted[owner])
                  for owner in wanted if actual[owner] != wanted[owner]]
    assert not mismatches,repr(mismatches[:3])
    rows = sum(row['rows'] for row in actual.values())
    eligible = sum(count for row in actual.values() for count,size in row['buckets'].values())
    return dict(owners=len(actual),rows=rows,eligible=eligible,ineligible=rows-eligible,
        memberships=sum(len(r['buckets']) for r in actual.values()),
        normalized_scopes=len({condition for r in actual.values() for condition in r['buckets']}),
        home_scopes=len({(owner[0],condition) for owner,r in actual.items() for condition in r['buckets']}))


def observe_health(trace, expected, warm, first_row, exited, warm_done):
    from threading import local
    from cachemonitor import prepared_speed_writer as warning_module
    from cachemonitor.prepared_speed import PreparedSpeedInput
    from cachemonitor.prepared_speed_writer import PreparedWarningWorker, SPEED_CHECKPOINT_NAMESPACE
    from cachemonitor.prepared_store import PreparedReader, Snapshot, Stage, pack, unpack
    from cachemonitor.speed_health import SpeedHealth
    state = local()
    verified, warm_tokens = [False], set()
    create, close = PreparedSpeedInput.from_snapshot, PreparedSpeedInput.close
    stream, checkpoint_state, snapshot_exit = Snapshot.iter_speed_rows, Snapshot.checkpoint_state, Snapshot.__exit__
    admit, collect = SpeedHealth._admit, SpeedHealth.collect_prepared
    restore, commit = warning_module.restore_checkpoint, PreparedWarningWorker.commit_completed
    publish, install = Stage.publish, Stage.checkpoint

    def token(value):
        return [value.instance,value.generation]

    def frontiers(owners):
        return hashlib.sha256(json.dumps(sorted((owner,stamp.revision,stamp.length,stamp.last_ordinal)
            for owner,stamp in owners.items())).encode()).hexdigest()

    def created(snapshot):
        started,cpu = time.perf_counter_ns(),time.thread_time()
        source = create(snapshot)
        state.current = dict(source=source,snapshot=snapshot,started=started,cpu=cpu,
            missing=False,row=False,restored=False,recovery=None,collected=False,error=None,tracker=None,observed_rows=0)
        trace.emit('health_operation_entry',operation='warning_worker',token=token(source.token),
            source=unpack(source.source_identity,source.limits),public=snapshot.public,ready=snapshot.ready,
            owners=len(source.owners),owner_frontiers_sha256=frontiers(source.owners))
        return source

    def head(snapshot, namespace):
        value = checkpoint_state(snapshot,namespace)
        current = getattr(state,'current',None)
        if current and current['snapshot'] is snapshot and namespace == SPEED_CHECKPOINT_NAMESPACE:
            if value is None:
                current['missing'] = True
                current['recovery'] = 'missing checkpoint'
                trace.emit('health_recovery_entry',token=token(current['source'].token))
            elif tuple(value['token']) != tuple(token(current['source'].token)):
                current['recovery'] = 'stale checkpoint'
        return value

    def restoring(health,source,*values,**kwargs):
        current = getattr(state,'current',None)
        try:
            result = restore(health,source,*values,**kwargs)
        except warning_module.CheckpointRejected:
            if current and current['source'] is source:current['recovery'] = 'rejected checkpoint'
            raise
        if current and current['source'] is source:
            current['restored'] = True
            current['tracker'] = health
            if not verified[0]:
                trace.emit('health_workload_verified',**proof_audit(health.proofs,expected),
                    restored=True,token=token(source.token))
                verified[0] = True
        return result

    def rows(snapshot,*values,**kwargs):
        for value in stream(snapshot,*values,**kwargs):
            current = getattr(state,'current',None)
            if current and current['snapshot'] is snapshot:current['observed_rows'] += 1
            if current and current['snapshot'] is snapshot and current['missing'] and not current['row']:
                current['row'] = True
                trace.emit('health_first_historical_row',token=token(current['source'].token))
                first_row.set()
            yield value

    def admitted(health,proofs):
        current = state.current
        if not verified[0]:
            start = time.perf_counter_ns()
            trace.emit('health_workload_verified',**proof_audit(proofs,expected),
                verification_ns=time.perf_counter_ns()-start,token=token(current['source'].token))
            verified[0] = True
        verify_warm(proofs,current['source'])
        return admit(health,proofs)

    def verify_warm(proofs,source):
        warm_owner = (warm['home'],warm['sid'])
        source_token = tuple(token(source.token))
        if source_token not in warm_tokens and warm_owner in proofs and proofs[warm_owner].buckets:
            proof_audit({warm_owner:proofs[warm_owner]},[warm['expected']])
            condition = tuple(warm['plan']['condition'])
            peer_rows = sum(p.buckets.get(condition,(0,0))[0] for owner,p in proofs.items()
                            if owner[0] == warm_owner[0] and owner != warm_owner)
            expected_peers = sum(b['rows'] for owner in expected if owner['owner'][0] == warm_owner[0]
                                 for b in owner['buckets'] if tuple(b['condition']) == condition)
            assert peer_rows == expected_peers and peer_rows > 0,(peer_rows,expected_peers)
            warm_tokens.add(source_token)
            trace.emit('warm_eligible_scope_verified',owner=warm_owner,condition=condition,
                peer_rows=peer_rows,rows=proofs[warm_owner].length,token=list(source_token))

    def collecting(health,source):
        current = getattr(state,'current',None)
        if current and current['source'] is source:current['tracker'] = health
        result = collect(health,source)
        if current and current['source'] is source:
            current['read_rows'] = source.read_rows
            current['collected'] = True
            verify_warm(health.proofs,source)
            if health.metrics.get('capacity_fallback'):
                trace.emit('fixture_limiter',metrics=health.metrics)
                raise RuntimeError('production allowance rejected the declared synthetic shape')
        return result

    def leaving(snapshot,*values):
        current = getattr(state,'current',None)
        if current and current['snapshot'] is snapshot and values[0] is not None:
            current['error'] = [values[0].__name__,str(values[1])]
        return snapshot_exit(snapshot,*values)

    def closing(source):
        current = getattr(state,'current',None)
        try:
            return close(source)
        finally:
            if current and current['source'] is source:
                health = current['tracker']
                success = current['collected'] and current['error'] is None
                trace.emit('health_operation_exit',operation='warning_worker',success=success,
                    elapsed_ns=time.perf_counter_ns()-current['started'],cpu_seconds=time.thread_time()-current['cpu'],
                    read_rows=current['read_rows'] if current['collected'] else source.read_rows,
                    observed_rows=current['observed_rows'],checkpoint=dict(restored=current['restored'],recovery=current['recovery']),
                    metrics={} if health is None else dict(health.metrics),token=token(source.token),error=current['error'])
                if current['missing']:
                    trace.emit('health_recovery_exit',success=success,token=token(source.token))
                    exited.set()
                state.current = None

    def published(stage,*values,**kwargs):
        result = publish(stage,*values,**kwargs)
        record = dict(token=token(result),writes=dict(stage.writes),source=unpack(stage.source,stage.limits))
        trace.emit('prepared_stage_committed',**record)
        current = getattr(state,'committing',None)
        if current is not None:current.update(record)
        return result

    def checkpoint_install(stage,*values,**kwargs):
        started,cpu = time.perf_counter_ns(),time.thread_time()
        success = False
        try:
            result = install(stage,*values,**kwargs)
            success = True
            return result
        finally:
            trace.emit('checkpoint_install_exit',namespace=values[0],success=success,
                elapsed_ns=time.perf_counter_ns()-started,cpu_seconds=time.thread_time()-cpu,
                complete=kwargs.get('complete'),sections=stage.writes['checkpoint_sections'],
                chunks=stage.writes['checkpoint_chunks'])

    def committed(worker):
        before = worker.store.token
        basis = worker._target
        record = {}
        state.committing = record
        started,cpu = time.perf_counter_ns(),time.thread_time()
        try:
            result = commit(worker)
        finally:
            state.committing = None
        if result is None:return None
        assert result.error is None,result.error
        assert basis.source == result.target.source
        assert frontiers(basis.owners) == frontiers(result.target.owners)
        assert result.target.token.instance == before.instance and result.target.token.generation == before.generation+1
        assert record['token'] == token(result.target.token)
        assert all(value == 0 or name in ('checkpoint_chunks','checkpoint_sections')
            for name,value in record['writes'].items()),record
        with PreparedReader(worker.path,worker.limits) as reader:
            with reader.pin(result.target.token) as view:
                stored = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                assert stored is not None and tuple(stored['token']) == tuple(token(result.target.token)),stored
                assert pack(view.source_identity,view.limits) == result.target.source
                checkpoint_head = dict(token=list(stored['token']),sections=stored['sections'],encoded_bytes=stored['encoded_bytes'])
        trace.emit('health_checkpoint_committed',**record,basis_token=token(before),checkpoint=result.checkpoint,
            read_rows=result.read_rows,metrics=result.metrics,checkpoint_head=checkpoint_head,
            owner_frontiers_sha256=frontiers(result.target.owners),elapsed_ns=time.perf_counter_ns()-started,
            cpu_seconds=time.thread_time()-cpu)
        if tuple(token(before)) in warm_tokens:warm_done.set()
        return result

    PreparedSpeedInput.from_snapshot = staticmethod(created)
    PreparedSpeedInput.close = closing
    Snapshot.checkpoint_state = head
    Snapshot.iter_speed_rows = rows
    Snapshot.__exit__ = leaving
    SpeedHealth._admit = admitted
    SpeedHealth.collect_prepared = collecting
    warning_module.restore_checkpoint = restoring
    Stage.publish = published
    Stage.checkpoint = checkpoint_install
    PreparedWarningWorker.commit_completed = committed



def recovery_analysis(connection,*args,trace_path,fixture,phase,first_row,exited,warm_done):
    manifest = json.loads((fixture/'fixture.json').read_text(encoding='utf-8'))
    observe_health(Trace(trace_path),manifest['expected'][phase],manifest['warm'],first_row,exited,warm_done)
    analysis_process(connection,*args,trace_path=trace_path)


def warning_oracle(manifest,phase):
    slow = [p for p in manifest[phase] if p['eligible']]
    return dict(active=True,baseline_speed=256.,threshold=128.,baseline_count=8,
        baseline_keys=manifest['peer_keys'],baseline_source='peers',recent_speed=64.,recent_count=3,
        drop_percent=75.,recent_keys=[p['key'] for p in slow[-3:]],incident_id=slow[0]['key'],
        call_id=slow[-1]['key'],model='gpt-6.1-sol',effort='high',tier='Standard')


@dataclass
class NativeFrame:
    name: str
    scenario: Scenario
    warning: dict | None = None
    content_ns: int | None = None
    paint_ns: int | None = None
    frame_ns: int | None = None
    painted_data: dict | None = None

    def matches(self,content,home):
        if not check_output(content.data,self.scenario,home):
            return False
        if self.warning is not None:
            visible = content.speed_alert()
            return all(visible.get(key) == value for key,value in self.warning.items())
        return True


class RecoveryCase:
    def __init__(self,args,directory,report,manifest):
        self.args,self.directory,self.report,self.manifest = args,directory,report,manifest
        self.startup_ns = time.perf_counter_ns()
        self.app = native_application()
        self.surface = NativeOverlayTarget(directory,self.app)
        self.controller = self.surface.controller
        self.home = Path(manifest['selected_home'])
        self.index,self.evidence,self.quota = (args.fixture/name for name in ('index.sqlite','evidence.sqlite','quota.sqlite'))
        self.trace = Trace(directory/'gui.jsonl')
        self.context = mp.get_context('spawn')
        self.parent,self.child = self.context.Pipe()
        self.collector = self.context.Process(target=watcher_process,
            args=(self.child,Path(manifest['homes'][0]),self.index,self.evidence,directory/'collector.jsonl',False),
            kwargs=dict(homes=manifest['homes']))
        self.first_row,self.exited,self.warm_done = self.context.Event(),self.context.Event(),self.context.Event()
        self.bridge = None
        self.current = None
        self.frames = []
        self.connected = False
        from cachemonitor.overlay_view import OverlayContent
        self.original_paint = OverlayContent.paint
        case = self
        def painted(content,painter):
            case.original_paint(content,painter)
            if content is case.controller.widget.content_model:
                case.painted(content)
        OverlayContent.paint = painted
        self.controller.widget.content_model.changed.connect(self.changed)

    def wait(self,predicate,label,*,check_errors=True):
        from PySide6.QtCore import QEventLoop,QTimer,Qt
        loop,timer = QEventLoop(),QTimer()
        timer.setInterval(5)
        deadline = time.monotonic()+(120 if self.args.case in ('cold','during') else 30)
        failure = []
        def poll():
            try:
                if check_errors and self.report['errors']:
                    raise RuntimeError(self.report['errors'][-1])
                self.controller.observed_at = time.monotonic()
                if self.controller.widget._quick is not None and not self.connected:
                    self.controller.widget.quick.quickWindow().afterRendering.connect(self.rendered,Qt.DirectConnection)
                    self.connected = True
                if predicate():
                    loop.quit()
                elif time.monotonic() >= deadline:
                    raise TimeoutError(label)
            except BaseException as error:
                failure.append(error)
                loop.quit()
        timer.timeout.connect(poll)
        timer.start()
        poll()
        if not failure and not predicate():
            loop.exec()
        timer.stop()
        if failure:
            raise failure[0]

    def changed(self):
        try:
            for frame in self.frames:
                if frame.content_ns is None and frame.matches(self.controller.widget.content_model,self.home):
                    frame.content_ns = self.trace.emit('content_model',frame=frame.name)
        except BaseException:
            self.report['errors'].append(traceback.format_exc())

    def painted(self,content):
        try:
            for frame in self.frames:
                if frame.paint_ns is None and frame.matches(content,self.home):
                    frame.painted_data = json.loads(json.dumps(content.data))
                    frame.paint_ns = self.trace.emit('native_paint',frame=frame.name,
                        kind='warning' if frame.warning is not None else 'numeric')
        except BaseException:
            self.report['errors'].append(traceback.format_exc())

    def rendered(self):
        for frame in self.frames:
            if frame.paint_ns and frame.frame_ns is None:
                frame.frame_ns = self.trace.emit('native_after_rendering',frame=frame.name)

    def expect(self,scenario,warning=None):
        frame = NativeFrame(scenario.name+('-warning' if warning is not None else '-numeric'),scenario,warning)
        self.frames.append(frame)
        self.changed()
        return frame

    def finish(self,frame):
        self.wait(lambda:frame.frame_ns is not None,frame.name+' frame')
        assert 0 < frame.scenario.source_ns <= frame.content_ns <= frame.paint_ns <= frame.frame_ns
        assert frame.matches(self.controller.widget.content_model,self.home)
        image_hash = self.surface.capture(self.directory/(frame.name+'.png'))
        if frame.scenario.unattributed:
            metrics = {r['id']:r['number'] for r in self.controller.widget.content_model.headline_metrics()}
            assert metrics['cost'] == metrics['speed'] == chr(0x2014)
        elif frame.scenario.evidence:
            speed = next(r for r in self.controller.widget.content_model.headline_metrics() if r['id'] == 'speed')
            expected = frame.scenario.evidence['output_speed']
            assert speed['number'] == (chr(0x2014) if expected is None else f'{expected:,.1f}')
        result = dict(name=frame.name,kind='warning' if frame.warning is not None else 'numeric',
            source_ns=frame.scenario.source_ns,content_ns=frame.content_ns,paint_ns=frame.paint_ns,
            frame_ns=frame.frame_ns,capture_ns=time.perf_counter_ns(),frame_sha256=image_hash,
            source_to_frame_ms=(frame.frame_ns-frame.scenario.source_ns)/1e6,
            rendered_data=frame.painted_data,expected_warning=frame.warning)
        self.report['samples'].append(result)
        return result

    def snapshot(self,value):
        if self.current and self.current.matches_snapshot(value,self.home):
            self.current.bridge_ns = self.current.bridge_ns or self.trace.emit('bridge_snapshot',scenario=self.current.name)
        self.controller.receive_snapshot(value)
        if self.current and self.current.unattributed and self.current.bridge_ns:
            self.controller.widget.content_model.update()

    def detail(self,value):
        self.trace.emit('selected_detail',summary=fingerprint(value.get('summary')))
        self.controller.receive_overlay_detail(value)

    def start(self,phase):
        from cachemonitor import analysis_worker
        self.collector.start()
        self.report['owned_pids'].append(self.collector.pid)
        self.child.close()
        self.wait(self.parent.poll,'collector startup')
        assert self.parent.recv()['kind'] == 'started'
        analysis_worker.process_main = partial(recovery_analysis,trace_path=self.directory/'analysis.jsonl',
            fixture=self.args.fixture,phase=phase,first_row=self.first_row,exited=self.exited,warm_done=self.warm_done)
        self.bridge = analysis_worker.AnalysisBridge(self.manifest['homes'],self.index,
            model_evidence_path=self.evidence,quota_path=self.quota,cache_floor=0)
        self.bridge.snapshot.connect(self.snapshot)
        self.bridge.overlay_detail.connect(self.detail)
        self.bridge.failure.connect(self.report['errors'].append)
        self.bridge.fault.connect(self.controller.receive_overlay_fault)
        self.controller.overlay_loader = self.bridge.request_overlay_detail
        self.bridge.start()

    def arm(self,scenario,calls=None):
        self.current = scenario
        self.parent.send(dict(kind='poll',scenario=scenario.name,sid=scenario.sid,
            calls=len(scenario.rows) if calls is None else calls,owners=0,deadline=120,
            activity=scenario.activity,evidence={k:v for k,v in scenario.evidence.items()
                if k in ('response_status','requested_model','response_model','model_match')}))
        self.wait(self.parent.poll,'watcher expectation')
        assert self.parent.recv()['kind'] == 'armed'

    def collect(self,scenario):
        self.wait(self.parent.poll,'exact collector publication')
        value = self.parent.recv()
        assert value['kind'] == 'collected' and not value['errors'],value
        assert [r['key'] for r in value['selected'].get('history',())] == [r['key'] for r in scenario.rows]
        scenario.collector_ns,scenario.source_at = value['ns'],value['source_at']
        if scenario.activity:
            matches = [r for r in value.get('activity',()) if all(r.get(k) == v for k,v in scenario.activity.items())]
            assert len(matches) == 1,value
            scenario.observed_activity = matches

    def evidence_update(self,scenario,status,elapsed=1000.,condition=None):
        from cachemonitor.model_evidence import EvidenceStore
        self.arm(scenario)
        rollout = self.home/'sessions'/(scenario.sid+'.jsonl')
        digest = hashlib.sha256(rollout.read_bytes()).hexdigest()
        store = EvidenceStore(self.evidence)
        at = time.time()
        condition = condition or ['gpt-6.1-sol','high','Standard','WebSocket',1,0,True]
        try:
            if status == 'pending':
                store.write(self.home,scenario.activity['attempt'],at,condition[3],status=status,
                            requested_model=condition[0],requested_effort=condition[1],request_observed_at=at)
            else:
                completed_observation(store,self.home,scenario.activity['attempt'],scenario.rows[-1]['key'],at,
                    status=status,completion_ms=elapsed,model=condition[0],effort=condition[1],
                    tier=condition[2],transport=condition[3])
        finally:
            store.close()
        scenario.source_at = at
        scenario.source_ns = self.trace.emit('evidence_commit',scenario=scenario.name,status=status)
        self.collect(scenario)
        if scenario.unattributed and scenario.bridge_ns:
            self.controller.widget.content_model.update()
        assert hashlib.sha256(rollout.read_bytes()).hexdigest() == digest

    def stop(self):
        from cachemonitor.overlay_view import OverlayContent
        if self.bridge is not None:
            if self.bridge.process is not None:
                self.report['owned_pids'].append(self.bridge.process.pid)
            self.bridge.requestInterruption()
            self.wait(lambda:not self.bridge.isRunning(),'analysis shutdown',check_errors=False)
            assert self.bridge.process is None or not self.bridge.process.is_alive()
            self.bridge.wait()
        OverlayContent.paint = self.original_paint
        self.surface.stop()
        if self.collector.pid and self.collector.is_alive():
            self.parent.send(dict(kind='stop'))
            self.collector.join(10)
            if self.collector.is_alive():
                self.collector.terminate()
                self.collector.join(5)
                self.report['errors'].append('collector required termination')
        self.parent.close()
        self.child.close()
        assert not self.collector.is_alive()


def recovery_shape(path):
    value = (json.loads(path.read_text(encoding='utf-8-sig')) if path else
             dict(version=1, label='counts-only scale', public_owners=1417,
                  canonical_rows=96593, completed_rows=42505, key_chars=47))
    if value.get('version') != 1:
        raise ValueError('unsupported recovery descriptor version')
    for name in ('public_owners', 'canonical_rows', 'completed_rows', 'key_chars'):
        if type(value.get(name)) is not int:
            raise ValueError('recovery descriptor requires integer ' + name)
    owners, rows, completed = (value[name] for name in
                               ('public_owners', 'canonical_rows', 'completed_rows'))
    if not 1 <= owners <= rows or not 1 <= completed <= rows:
        raise ValueError('invalid recovery owner, row, or completion counts')
    if not len(f'{rows:x}') <= value['key_chars'] <= 256:
        raise ValueError('response identities cannot preserve uniqueness and ingress limits')
    profiles = value.get('owner_profiles')
    if profiles is not None:
        if len(profiles) != owners or sum(p['rows'] for p in profiles) != rows:
            raise ValueError('owner profiles differ from descriptor totals')
        valid = 0
        for profile in profiles:
            if type(profile['rows']) is not int or profile['rows'] < 1:
                raise ValueError('each public owner requires canonical rows')
            used = 0
            for bucket in profile['buckets']:
                condition = bucket['condition']
                if (len(condition) != 7 or not all(type(v) is str and v for v in condition[:4])
                        or condition[2] not in ('Standard', 'Fast')
                        or condition[3] not in ('WebSocket', 'HTTP/SSE')
                        or type(condition[4]) is not int or not 0 <= condition[4] < 6
                        or type(condition[5]) is not int or not 0 <= condition[5] < 4
                        or type(condition[6]) is not bool
                        or type(bucket['rows']) is not int or bucket['rows'] < 1):
                    raise ValueError('unsupported observed condition profile')
                if 'proof_max_key_getsizeof_bytes' in bucket and 'key_chars' not in bucket:
                    raise ValueError('proof byte maxima require an explicit synthetic key_chars choice')
                chars = bucket.get('key_chars', value['key_chars'])
                if type(chars) is not int or not len(f'{rows:x}') <= chars <= 256:
                    raise ValueError('key_chars must be an explicit character width, not getsizeof bytes')
                used += bucket['rows']
            if used > profile['rows']:
                raise ValueError('valid buckets exceed owner rows')
            valid += used
        if valid != completed:
            raise ValueError('owner bucket counts differ from completion total')
    value['identity_width_claim'] = ('Declared synthetic character widths. Any supplied per-owner maximum '
        'is applied as a maximum-width workload, not a full row histogram. getsizeof maxima require explicit conversion.')
    value['shape_claim'] = ('Provided owner and condition counts; synthetic ordering and identity widths.' if profiles
                            else 'Counts only; actual owner/scope distribution and identity histogram are not matched.')
    return value


def completed_observation(store, home, attempt, key, at, *, model='gpt-6.1-sol',
                          effort='high', tier='Standard', transport='WebSocket', status='completed', completion_ms=1000.):
    tier_value = 'default' if tier == 'Standard' else 'priority'
    store.write(home, attempt, at, transport, response_id=key, requested_model=model,
        response_model=model, status=status, requested_service_tier=tier_value,
        response_service_tier=tier_value, requested_effort=effort, response_effort=effort,
        request_observed_at=at - completion_ms/1000, generation_observed_at=at - completion_ms/1000 + .25,
        completed_observed_at=at if status == 'completed' else None,
        generation_latency_ms=250., completion_latency_ms=completion_ms if status == 'completed' else None)


def recovery_cim_audit(directory, report):
    pids = {report['preparation_pid']} if report.get('preparation_pid') else set()
    for path in directory.glob('*/trace/*.jsonl'):
        pids.update(json.loads(line)['pid'] for line in path.read_text(encoding='utf-8').splitlines())
    pids.update(case['result']['gui_pid'] for case in report.get('cases', ()) if case.get('result'))
    pids.update(pid for case in report.get('cases',()) if case.get('result')
                for pid in case['result'].get('owned_pids',()))
    pids.update(case['ui_result']['pid'] for case in report.get('cases', ()) if case.get('ui_result'))
    if not pids:
        return dict(owned_pids=[], surviving=[])
    condition = ' OR '.join('ProcessId=' + str(pid) for pid in sorted(pids))
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "$p=@(Get-CimInstance Win32_Process -Filter '" + condition + "' | "
              "Select-Object ProcessId,Name,CreationDate,CommandLine); ConvertTo-Json -InputObject $p -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, encoding='utf-8-sig', timeout=20, check=True)
    surviving = json.loads(result.stdout)
    return dict(owned_pids=sorted(pids), surviving=surviving)


def recovery_input_identity(fixture):
    result = {}
    for path in sorted(fixture.rglob('*')):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda:source.read(1024*1024), b''):
                digest.update(chunk)
        result[str(path.relative_to(fixture))] = dict(bytes=path.stat().st_size, sha256=digest.hexdigest())
    return result


def events(directory):
    return sorted((json.loads(line) for path in directory.glob('*.jsonl')
        for line in path.read_text(encoding='utf-8').splitlines()),key=lambda row:row['ns'])


def selected_scenario(manifest,phase,name,clock):
    scenario = Scenario(name,manifest['sid'],numeric_rows(manifest[phase]))
    scenario.source_ns = clock
    scenario.evidence = dict(response_status='completed',requested_model='gpt-6.1-sol',
        response_model='gpt-6.1-sol',timing_valid=True,output_speed=64.,
        generation_latency_ms=250.,completion_latency_ms=4000.)
    return scenario


def response_scenario(name,sid,rows,status,elapsed,condition,attempt):
    scenario = Scenario(name,sid,list(rows),activity=dict(attempt=attempt,status=status,response_id=rows[-1]['key']))
    scenario.evidence = dict(response_status=status,requested_model=condition[0],response_model=condition[0],
        timing_valid=status == 'completed',output_speed=rows[-1]['output']/(elapsed/1000) if status == 'completed' else None)
    if status == 'completed':
        scenario.evidence.update(model_match='일치',generation_latency_ms=250.,completion_latency_ms=elapsed)
    return scenario


def run_protocol(case):
    manifest = case.manifest
    phase = 'after_during' if case.args.case == 'warm' else 'initial'
    baseline = selected_scenario(manifest,phase,case.args.case+'-baseline',case.startup_ns)
    case.current = baseline
    case.surface.select(baseline.sid)
    numeric = case.expect(baseline)
    warning = case.expect(baseline,warning_oracle(manifest,phase))
    case.start(phase)
    case.arm(baseline)
    case.collect(baseline)
    if case.args.case == 'during':
        case.wait(lambda:case.first_row.is_set() or case.exited.is_set(),'actual recovery first row')
        if not case.first_row.is_set() or case.exited.is_set():
            raise RuntimeError('inconclusive fixture overlap; recovery ended before source update')
        latest = Scenario('during-first-call',manifest['sid'],numeric_rows(manifest['after_during']))
        case.arm(latest)
        numeric = case.expect(latest)
        terminal_basis = selected_scenario(manifest,'after_during','during-terminal-basis',0)
        warning = case.expect(terminal_basis,warning_oracle(manifest,'after_during'))
        write_raw(case.home,latest.sid,[manifest['during_append']])
        latest.source_ns = case.trace.emit('source_close',scenario=latest.name)
        case.collect(latest)
        for status in ('created','completed'):
            response = response_scenario('during-'+status,latest.sid,latest.rows,status,4000.,
                                         manifest['during_append']['condition'],'during-'+latest.rows[-1]['key'])
            case.evidence_update(response,status,4000.)
            if status == 'completed':
                terminal_basis.source_ns = response.source_ns
    case.finish(numeric)
    case.finish(warning)
    if case.args.case != 'warm':
        return
    warm = manifest['warm']
    case.home = Path(warm['home'])
    sid = warm['sid']
    add_session(case.home,sid,'Eligible warm UUID first call')
    pending = Scenario('warm-unbound-pending',sid,unattributed=True,
                       activity=dict(attempt='warm-request',status='pending',response_id=''))
    case.surface.select(sid)
    pending_frame = case.expect(pending)
    case.evidence_update(pending,'pending',condition=warm['plan']['condition'])
    case.finish(pending_frame)
    first = Scenario('warm-first-call',sid,numeric_rows([warm['plan']]))
    first.evidence = dict(output_speed=None)
    case.arm(first)
    first_frame = case.expect(first)
    write_raw(case.home,sid,[warm['plan']])
    first.source_ns = case.trace.emit('source_close',scenario=first.name)
    case.collect(first)
    case.finish(first_frame)
    for status in ('created','completed'):
        response = response_scenario('warm-'+status,sid,first.rows,status,1000.,warm['plan']['condition'],'warm-request')
        frame = case.expect(response)
        case.evidence_update(response,status,1000.,warm['plan']['condition'])
        case.finish(frame)
    case.wait(case.warm_done.is_set,'eligible warm warning completion')


def audit_case(case):
    rows = events(case.directory)
    health = [r for r in rows if r['stage'].startswith('health_') or r['stage'] in
        ('warm_eligible_scope_verified','fixture_limiter','checkpoint_install_exit')]
    assert any(r['stage'] == 'health_workload_verified' for r in health),'actual proof workload was not verified'
    reads = [r for r in health if r['stage'] == 'health_operation_exit' and r['operation'] == 'warning_worker']
    checkpoints = [r for r in health if r['stage'] == 'health_checkpoint_committed']
    assert reads and any(r['success'] for r in reads) and checkpoints,health
    assert all(r['read_rows']==r['observed_rows'] for r in reads if r['success']),reads
    case.report['health'] = health
    case.report['worker_pass_failures'] = [r for r in reads if not r['success']]
    if case.args.case == 'restart':
        assert reads[0]['success'] and reads[0]['checkpoint']['restored'],reads[0]
        assert all(r['read_rows'] == r['observed_rows'] == 0 for r in reads),reads
        assert checkpoints[0]['checkpoint']['restored'] and checkpoints[0]['read_rows'] == 0,checkpoints[0]
    if case.args.case == 'cold':
        assert any(r['stage'] == 'health_recovery_entry' for r in health)
        assert any(r['success'] and r['checkpoint']['recovery'] == 'missing checkpoint' for r in reads),reads
        checkpoint = checkpoints[0]
        assert checkpoint['writes']['checkpoint_sections'] > 0,checkpoint
        prepared = json.loads((case.args.fixture/'preparation.json').read_text(encoding='utf-8'))
        stages = [r for r in rows if r['stage'] == 'prepared_stage_committed' and r['ns'] <= checkpoint['ns']]
        allowed = {'owners','seen','checkpoint_chunks','checkpoint_sections'}
        assert all(count == 0 or name in allowed for r in stages for name,count in r['writes'].items()),stages
        before,after = prepared['source'],checkpoint['source']
        collected = next(r for r in rows if r['stage']=='collector_publication' and r['scenario']=='cold-baseline')
        assert before[:3] == after[:3] and collected['source_epoch'] != before[3][0],(before,after,collected)
        case.report['cold_startup_contract'] = 'Fresh collector validates unchanged facts; unchanged prepared generations remain reusable.'
        case.report['automatic_checkpoint_with_metadata_alignment'] = dict(
            setup_source=before,published_source=after,checkpoint=checkpoint,fact_stages=stages)
    checkpoint_tokens = {tuple(r['token']):r for r in checkpoints}
    warning_frames = []
    warm_tokens = {tuple(r['token']) for r in health if r['stage'] == 'warm_eligible_scope_verified'}
    for result in case.report['samples']:
        if result['kind'] != 'warning':continue
        revision = result['rendered_data']['_overlay_revision']
        matches = [r for r in rows if r['stage'] == 'analysis_publication' and r['ns'] <= result['frame_ns']
                   and r.get('revisions',{}).get(case.manifest['sid']) == revision]
        assert matches,revision
        token = tuple(matches[-1]['token'])
        assert token in checkpoint_tokens,(token,checkpoint_tokens)
        committed = checkpoint_tokens[token]
        assert committed['ns'] <= result['frame_ns']
        warning_frames.append(dict(name=result['name'],token=token,frame_ns=result['frame_ns'],
            active_warning=result['expected_warning'],completion=committed))
    assert warning_frames,'active warning content never reached a native warning frame'
    case.report['warning_frames'] = warning_frames
    if case.args.case == 'during':
        exit_ns = next(r['ns'] for r in health if r['stage'] == 'health_recovery_exit')
        publications = {r['scenario']:r.get('published_ns',r['ns']) for r in rows
                        if r['stage'] == 'collector_publication' and r.get('scenario','').startswith('during-')}
        assert all(publications.get(name,exit_ns) < exit_ns for name in
                   ('during-first-call','during-created','during-completed')),publications
        case.report['overlap'] = dict(recovery_exit_ns=exit_ns,publications=publications)
    if case.args.case == 'warm':
        completions = [r for r in checkpoints if tuple(r['basis_token']) in warm_tokens]
        assert warm_tokens and completions,health
        case.report['warm_warning_completion'] = completions[-1]
        case.report['warm_update_claim'] = 'Terminal completion joined an eligible call in an already populated bulk peer scope.'



def suite(args,directory,report):
    fixture = args.fixture or ROOT/'artifacts'/('nr-'+uuid.uuid4().hex[:8])
    report['fixture'] = str(fixture)
    if args.fixture:
        manifest=json.loads((fixture/'fixture.json').read_text(encoding='utf-8'))
        report['setup_contract']='Retained raw fixture and partial store; ordinary revalidation is permitted within one 360-second pre-clock window.'
    else:
        fixture.mkdir()
        manifest=create_fixture(fixture,recovery_shape(args.shape))
    report['fixture_shape'] = manifest
    context = mp.get_context('spawn')
    parent,child = context.Pipe()
    worker = context.Process(target=prepare_fixture,args=(child,fixture))
    try:
        worker.start()
        report['preparation_pid'] = worker.pid
        child.close()
        if not parent.poll(370):
            raise TimeoutError('canonical preparation child deadline')
        value = parent.recv()
        assert value['kind'] == 'prepared',value
        worker.join(10)
        assert worker.exitcode == 0 and not worker.is_alive()
        report['preparation'] = value['result']
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
        child.close()
    prepared,seed = fixture/'quota.prepared.sqlite',fixture/'missing-checkpoint.sqlite'
    with sqlite3.connect(prepared.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(seed) as target:
        source.backup(target)
    report['cases'] = []
    for name in ('cold','restart','during','warm'):
        if name == 'during':
            with sqlite3.connect(seed.as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(prepared) as target:
                source.backup(target)
        path = directory/name
        command = [sys.executable,'-B',str(ROOT/'tools/run_ui_checks.py'),'--kind','gui','--timeout',
            '420' if name == 'warm' else '180','--diagnostics',str(path/'ui'),'--',sys.executable,'-B',
            str(ROOT/'tools/benchmark_overlay_recovery.py'),'--case',name,'--fixture',str(fixture),
            '--case-directory',str(path/'trace')]
        result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',
                                env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUTF8='1'))
        path.mkdir(parents=True,exist_ok=True)
        (path/'runner-stdout.txt').write_text(result.stdout,encoding='utf-8')
        (path/'runner-stderr.txt').write_text(result.stderr,encoding='utf-8')
        outcome = dict(case=name,exit_code=result.returncode,report=str(path/'trace/report.json'))
        for key,file in (('result',path/'trace/report.json'),('ui_result',path/'ui/result.json')):
            if file.exists():
                outcome[key] = json.loads(file.read_text(encoding='utf-8'))
        report['cases'].append(outcome)
        if result.returncode:
            raise RuntimeError('native case failed; inspect retained '+str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/overlay-latency/measurement')
    parser.add_argument('--shape',type=Path)
    parser.add_argument('--case',choices=('cold','restart','during','warm'))
    parser.add_argument('--fixture',type=Path)
    parser.add_argument('--case-directory',type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('native Windows Qt is required')
    allowed = (ROOT/'artifacts/overlay-latency/measurement').resolve()
    directory = (args.case_directory.resolve() if args.case_directory else args.output.resolve()/
                 (datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]))
    if not directory.is_relative_to(allowed):
        parser.error('output must remain inside artifacts/overlay-latency/measurement')
    if args.case and args.fixture is None:
        parser.error('internal case requires a prepared private fixture')
    if args.fixture is not None:
        args.fixture = args.fixture.resolve()
        if not args.fixture.is_relative_to((ROOT/'artifacts').resolve()) or not (args.fixture/'fixture.json').is_file():
            parser.error('fixture must be a declared private artifact')
    directory.mkdir(parents=True)
    profile = directory/'profile'
    for key in ('HOME','USERPROFILE','LOCALAPPDATA','APPDATA','CODEX_HOME','TEMP','TMP'):
        target = profile/key.lower()
        target.mkdir(parents=True)
        os.environ[key] = str(target)
    report = dict(version=2,claim='Counts-only synthetic raw ingress through normal analysis to separate exact numeric and active-warning native frames.',
        excludes=['actual-corpus startup','physical presentation','Codex route discovery','upstream model generation','operating-system cache-cold startup'],
        simulated_model_timings=True,case=args.case,samples=[],errors=[],owned_pids=[os.getpid()],
        gui_pid=os.getpid() if args.case else None,host_before=host_snapshot())
    case = None
    try:
        report['code'] = code_identity(directory)
        if args.case:
            report['inputs_before'] = recovery_input_identity(args.fixture)
            manifest = json.loads((args.fixture/'fixture.json').read_text(encoding='utf-8'))
            case = RecoveryCase(args,directory,report,manifest)
            try:
                run_protocol(case)
            finally:
                case.stop()
            audit_case(case)
        else:
            suite(args,directory,report)
    except BaseException:
        report['errors'].append(traceback.format_exc())
    finally:
        if not args.case:
            try:
                report['owned_process_audit'] = recovery_cim_audit(directory,report)
                assert not report['owned_process_audit']['surviving'],report['owned_process_audit']
            except BaseException:
                report['errors'].append(traceback.format_exc())
        report['host_after'] = host_snapshot()
        report['code_after'] = code_identity(directory,name='sources-after.json')
        if report.get('code') != report['code_after']:
            report['errors'].append('code identity changed during observation')
        report['failure_count'] = len(report['errors'])
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(str(directory/'report.json'),flush=True)
    return int(bool(report['errors']))


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
