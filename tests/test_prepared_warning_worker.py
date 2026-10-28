from collections import Counter
from contextlib import closing
from copy import deepcopy
import gc
from hashlib import sha256
from threading import Condition, Event, current_thread, get_ident
from time import monotonic
from types import SimpleNamespace
import weakref

import pytest

from cachemonitor import prepared_ingest as ingest
from cachemonitor import prepared_speed_writer as speed_writer
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.prepared_store import PreparedReader, PreparedStore, ReadCancelled, Snapshot, WriteDeferred
from cachemonitor.speed_health import SpeedHealth, classify


@pytest.fixture
def numeric_source(tmp_path,monkeypatch):
    session=Session('owner','home',title='Owner')
    for number in range(15):
        session.add_usage(100+number*10,f'call-{number}',
            dict(input_tokens=80000,cached_input_tokens=72000,output_tokens=2000),
            'gpt-6-astra','request','medium',service_tier='Standard')
    source=deepcopy(session.view(1000))
    source.update(usage_revision=1,collection_complete=True)
    for number,row in enumerate(source['history']):
        row.update(reasoning=500,transport='WebSocket',transport_source='response_id',
            response_status='completed',timing_valid=True,
            completion_latency_ms=20000 if number<12 else 100000)
    owner=('home','owner')
    def catalog(revision):
        return dict(scope='scope',path='source',selected_homes=['home'],
            catalog_identity=('epoch',revision),membership_complete=True,
            owners={owner:dict(body_digest=sha256(repr(source).encode()).hexdigest(),
                source_ordinal=0,state={},owner_lineage=dict(parent_thread_id=None))})
    def read_source(client,key,captured,*,limits,**kwargs):
        value=deepcopy(source)
        return value,ingest._weight(value,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    def append():
        source['history'].append(dict(source['history'][-1],key='new-call',ts=260,output=2001,total=82001))
        source['usage_revision']+=1
    def reference():
        engine=AnalysisEngine();engine.ingest([deepcopy(source)])
        return classify(state['prepared'] for state in engine.sessions.values())
    assert reference()[owner]['active'] is True
    return SimpleNamespace(source=source,owner=owner,catalog=catalog,append=append,
        reference=reference,path=tmp_path/'worker.sqlite')


def publish_numeric(store,warnings,value,revision):
    coordinator=ingest.PreparedCohort(store,None,None,speed_writer=warnings)
    changes=list(coordinator.convert(value.catalog(revision)))
    assert changes and changes[-1]['ready']
    return coordinator,store.token


def observe_wait(monkeypatch,callback):
    class ObservedCondition(Condition):
        def wait_for(self,predicate,timeout=None):
            def checked():
                ready=predicate()
                if not ready:callback()
                return ready
            return super().wait_for(checked,timeout)
    monkeypatch.setattr(speed_writer,'Condition',ObservedCondition)


def test_new_collector_epoch_does_not_replay_unchanged_peer_history(numeric_source, monkeypatch):
    value = numeric_source
    peer = deepcopy(value.source)
    peer.update(home='other-home', id='peer')
    for row in peer['history']:
        row['key'] = 'peer-' + row['key']
    peer_owner = ('other-home', 'peer')

    def catalog(revision, epoch):
        result = value.catalog(revision)
        result['catalog_identity'] = (epoch, revision)
        result['selected_homes'].append('other-home')
        result['owners'][peer_owner] = dict(body_digest=sha256(repr(peer).encode()).hexdigest(),
            source_ordinal=1, state={}, owner_lineage=dict(parent_thread_id=None))
        return result

    def read_source(client, owner, captured, *, limits, **kwargs):
        source = deepcopy(peer if owner == peer_owner else value.source)
        return source, ingest._weight(source, limits.owner_source_bytes)

    reads = Counter()
    original_rows = Snapshot.iter_speed_rows

    def observed_rows(snapshot, ranges):
        for owner, ordinal, row in original_rows(snapshot, ranges):
            if current_thread().name == 'prepared-speed-warning':
                reads[owner] += 1
            yield owner, ordinal, row

    monkeypatch.setattr(ingest, 'read_source', read_source)
    monkeypatch.setattr(Snapshot, 'iter_speed_rows', observed_rows)
    completed = Event()
    with PreparedStore(value.path) as store, closing(speed_writer.PreparedWarningWorker(
            store, preparation=ingest.preparation_signature(), wake=completed.set)) as warnings:
        cohort = ingest.PreparedCohort(store, None, None, speed_writer=warnings)
        assert list(cohort.convert(catalog(1, 'old-producer')))[-1]['ready']
        assert completed.wait(5)
        assert warnings.commit_completed().error is None
        completed.clear()
        reads.clear()
        value.append()
        assert list(cohort.convert(catalog(2, 'new-producer')))[-1]['ready']
        assert completed.wait(5)
        assert warnings.commit_completed().error is None
        assert reads[value.owner] > 0
        assert reads[peer_owner] == 0
        reference = AnalysisEngine()
        reference.ingest([deepcopy(value.source), peer])
        assert warnings.results == classify(state['prepared'] for state in reference.sessions.values())


def test_begin_write_deferred_keeps_the_completed_result(numeric_source,monkeypatch):
    value=numeric_source
    completed=Event();reductions=[]
    original_collect=SpeedHealth.collect_prepared
    def collect(tracker,source):
        reductions.append(source.token)
        return original_collect(tracker,source)
    monkeypatch.setattr(SpeedHealth,'collect_prepared',collect)
    with PreparedStore(value.path) as store,closing(speed_writer.PreparedWarningWorker(
            store,preparation=ingest.preparation_signature(),wake=completed.set)) as warnings:
        _,numeric=publish_numeric(store,warnings,value,1)
        assert completed.wait(5),'warning completion was not delivered'
        def blocked_begin(*args,**kwargs):raise WriteDeferred('before metadata stage creation')
        with monkeypatch.context() as patch:
            patch.setattr(store,'begin',blocked_begin)
            with pytest.raises(WriteDeferred,match='before metadata'):
                warnings.commit_completed()
        assert store.token==numeric and store.stage is None
        result=warnings.commit_completed()
        assert result is not None and result.error is None
        assert store.token.generation==numeric.generation+1
        assert warnings.results==value.reference()
        assert reductions==[numeric]


def test_wal_cancel_keeps_queued_latest_and_waits_for_admission(numeric_source,monkeypatch):
    value=numeric_source
    entered=Event();release=Event();completed=Event();paused=Event()
    allow_admission=Event();allow_reads=Event();allow_reads.set()
    holder={};pins=[]
    def waiting():
        warnings=holder.get('warnings')
        if (warnings is not None and not allow_reads.is_set()
                and warnings._active is None and warnings._pending is not None):paused.set()
    observe_wait(monkeypatch,waiting)
    original_pin=PreparedReader.pin
    def pin(reader,expected=None,**kwargs):
        if current_thread().name=='prepared-speed-warning':
            assert allow_reads.is_set(),'optional read opened before writer admission'
            pins.append(expected)
        return original_pin(reader,expected,**kwargs)
    monkeypatch.setattr(PreparedReader,'pin',pin)
    original_collect=SpeedHealth.collect_prepared
    def collect(tracker,source):
        if not entered.is_set():
            entered.set()
            assert release.wait(5),'test did not release the active warning job'
        return original_collect(tracker,source)
    monkeypatch.setattr(SpeedHealth,'collect_prepared',collect)
    with PreparedStore(value.path) as store:
        warnings=speed_writer.PreparedWarningWorker(store,preparation=ingest.preparation_signature(),wake=completed.set)
        holder['warnings']=warnings
        original_admission=store._wal_admission
        try:
            coordinator,first=publish_numeric(store,warnings,value,1)
            assert entered.wait(5),'first warning job never opened its public pin'
            value.append()
            coordinator,latest=publish_numeric(store,warnings,value,2)
            allow_reads.clear()
            def admission():
                if not allow_admission.is_set():raise WriteDeferred('optional pin owns WAL')
                original_admission()
                allow_reads.set()
            monkeypatch.setattr(store,'_wal_admission',admission)
            controller=SimpleNamespace(stopped=Event())
            assert not PreparedWriter._resume_writes(controller,store,warnings)
            release.set()
            assert paused.wait(5),'cancelled worker did not preserve the latest pending target'
            assert pins==[first]
            assert coordinator.current_speeds()[value.owner]==dict(active=None,status='pending')
            assert not PreparedWriter._resume_writes(controller,store,warnings)
            assert pins==[first]
            completed.clear()
            allow_admission.set()
            assert PreparedWriter._resume_writes(controller,store,warnings)
            assert completed.wait(5),'latest target did not resume after writer admission'
            result=warnings.commit_completed()
            assert result is not None and result.error is None
            assert pins==[first,latest]
            assert warnings.results==value.reference()
            with PreparedReader(value.path) as reader,reader.pin(store.token) as view:
                rows=list(view.iter_calls(value.owner))
                assert len(rows)==16 and rows[-1]['key']=='new-call'
        finally:
            release.set();allow_admission.set();allow_reads.set()
            monkeypatch.setattr(store,'_wal_admission',original_admission)
            warnings.close()
        assert not warnings.thread.is_alive()


@pytest.mark.parametrize('resume_path',['write_retry','maintenance'])
def test_cancel_before_start_keeps_target_until_writer_admission(numeric_source,monkeypatch,resume_path):
    value=numeric_source
    paused=Event();completed=Event();allow_admission=Event();allow_reads=Event()
    holder={};pins=[]
    def waiting():
        warnings=holder.get('warnings')
        if warnings is not None and warnings._target is not None and not allow_reads.is_set():paused.set()
    observe_wait(monkeypatch,waiting)
    original_pin=PreparedReader.pin
    def pin(reader,expected=None,**kwargs):
        if current_thread().name=='prepared-speed-warning':
            assert allow_reads.is_set(),'paused worker opened a read transaction'
            pins.append(expected)
        return original_pin(reader,expected,**kwargs)
    monkeypatch.setattr(PreparedReader,'pin',pin)
    with PreparedStore(value.path) as store:
        warnings=speed_writer.PreparedWarningWorker(store,preparation=ingest.preparation_signature(),wake=completed.set)
        holder['warnings']=warnings
        original_admission=store._wal_admission
        try:
            warnings.cancel_optional()
            coordinator,numeric=publish_numeric(store,warnings,value,1)
            assert paused.wait(5),'worker never observed the paused target'
            assert not pins
            def admission():
                if not allow_admission.is_set():raise WriteDeferred('writer remains blocked')
                original_admission()
                allow_reads.set()
            monkeypatch.setattr(store,'_wal_admission',admission)
            controller=SimpleNamespace(stopped=Event())
            def resume():
                if resume_path=='write_retry':return PreparedWriter._resume_writes(controller,store,warnings)
                return not PreparedWriter._maintain(controller,store,speed_writer=warnings)
            assert not resume()
            assert coordinator.current_speeds()[value.owner]['active'] is None
            assert not pins
            allow_admission.set()
            deadline=monotonic()+5
            while not resume():assert monotonic()<deadline,'writer admission never resumed optional reads'
            assert completed.wait(5),'queued target did not start after admission'
            result=warnings.commit_completed()
            assert result is not None and result.error is None
            assert pins==[numeric] and warnings.results==value.reference()
        finally:
            allow_admission.set();allow_reads.set()
            monkeypatch.setattr(store,'_wal_admission',original_admission)
            warnings.close()
        assert not warnings.thread.is_alive()


def test_cancelled_restore_releases_sections_and_source_while_idle(numeric_source,monkeypatch):
    value=numeric_source
    with PreparedStore(value.path) as store,closing(speed_writer.PreparedSpeedWriter(
            store,preparation=ingest.preparation_signature())) as synchronous:
        publish_numeric(store,synchronous,value,1)
        numeric=store.token
    captured={};idle=Event();completed=Event()
    observe_wait(monkeypatch,lambda:idle.set() if 'header' in captured else None)
    def cancelled_restore(tracker,source,sections,**kwargs):
        captured.update(header=sections[0],count=len(sections),source=weakref.ref(source))
        raise ReadCancelled('cancelled during checkpoint restore')
    monkeypatch.setattr(speed_writer,'restore_checkpoint',cancelled_restore)
    with PreparedStore(value.path) as store,closing(speed_writer.PreparedWarningWorker(
            store,preparation=ingest.preparation_signature(),wake=completed.set)) as warnings:
        coordinator=ingest.PreparedCohort(store,None,None,speed_writer=warnings)
        assert list(coordinator.convert(value.catalog(1)))==[]
        assert coordinator.current_speeds()[value.owner]['active'] is None
        assert completed.wait(5) and idle.wait(5),'cancelled restore did not reach its idle boundary'
        gc.collect()
        assert captured['source']() is None,'idle worker retained the borrowed source'
        header=captured['header'];count=captured['count']
        assert not any(type(ref) is tuple and len(ref)==count and ref and ref[0] is header
            for ref in gc.get_referrers(header)),'idle worker retained the complete section tuple'
        failure=warnings.commit_completed()
        assert failure is not None and failure.error[0]=='ReadCancelled'
        assert store.token.generation==numeric.generation+1
        assert warnings.results[value.owner]==dict(active=None,status='unavailable')


def test_numeric_publication_advances_during_warning_recovery(numeric_source,monkeypatch):
    value=numeric_source
    entered=Event();release=Event();completed=Event()
    main=get_ident();calls=[]
    collect=SpeedHealth.collect_prepared
    def blocked(tracker,source):
        assert get_ident()!=main,'numeric publication invoked the warning reducer'
        calls.append(source.token)
        entered.set()
        assert release.wait(5),'test did not release warning work'
        return collect(tracker,source)
    monkeypatch.setattr(SpeedHealth,'collect_prepared',blocked)
    def forbidden(*args,**kwargs):raise AssertionError('numeric preparation scanned warning history')
    monkeypatch.setattr(speed_writer,'certify_speed_prefix',forbidden)
    with PreparedStore(value.path) as store:
        warnings=speed_writer.PreparedWarningWorker(store,preparation=ingest.preparation_signature(),wake=completed.set)
        try:
            coordinator,first=publish_numeric(store,warnings,value,1)
            assert entered.wait(5),'warning worker did not start'
            assert coordinator.current_speeds()[value.owner]==dict(active=None,status='pending')
            value.append()
            assert list(coordinator.convert(value.catalog(2)))[-1]['ready']
            numeric=store.token
            assert numeric.generation>first.generation
            assert coordinator.current_speeds()[value.owner]['active'] is None
            with PreparedReader(value.path) as reader,reader.pin(numeric) as view:
                rows=list(view.iter_calls(value.owner))
                assert len(rows)==16 and rows[-1]['key']=='new-call' and rows[-1]['output']==2001
            release.set()
            assert completed.wait(5),'warning completion was not observable'
            result=warnings.commit_completed()
            assert result is not None and result.error is None
            assert store.token.generation==numeric.generation+1
            assert warnings.results==value.reference() and warnings.results[value.owner]['active'] is True
            with PreparedReader(value.path) as reader,reader.pin(store.token) as view:
                assert list(view.iter_calls(value.owner))==rows
            assert first in calls and numeric in calls
        finally:
            release.set();warnings.close()
        assert not warnings.thread.is_alive()


@pytest.mark.parametrize('legacy_packet',[False,True])
@pytest.mark.parametrize('own_change,child_change,pending',[
    ({},{'output':80,'reasoning':20,'total':80080},True),
    ({'output':80,'reasoning':20,'total':80080},{},False),
    ({'response_status':'failed'},{},False),
])
def test_pending_warning_uses_latest_own_call(numeric_source,monkeypatch,own_change,child_change,pending,legacy_packet):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.prepared_delivery import PreparedPublisher
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.speed_health import scope
    value=numeric_source
    child=deepcopy(value.source)
    child.update(id='child',source='subagent',parent_thread_id='owner')
    for number,row in enumerate(child['history']):
        row.update(key=f'child-call-{number}',ts=row['ts']+1000)
    child['history'][-1].update(child_change)
    value.source['history'][-1].update(own_change)
    sources={value.owner:value.source,('home','child'):child}
    assert (scope(value.source['history'][-1]) is not None) is pending
    assert (scope(child['history'][-1]) is None) is pending
    def read_source(client,key,captured,*,limits,**kwargs):
        source=deepcopy(sources[key])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    catalog=value.catalog(1)
    catalog['owners']={key:dict(body_digest=sha256(repr(source).encode()).hexdigest(),
        source_ordinal=number,state={},owner_lineage=dict(parent_thread_id=source.get('parent_thread_id')))
        for number,(key,source) in enumerate(sources.items())}
    entered=Event();release=Event()
    collect=SpeedHealth.collect_prepared
    def blocked(tracker,source):
        entered.set()
        assert release.wait(5),'test did not release the warning reducer'
        return collect(tracker,source)
    monkeypatch.setattr(SpeedHealth,'collect_prepared',blocked)
    with PreparedStore(value.path) as store:
        warnings=speed_writer.PreparedWarningWorker(store,preparation=ingest.preparation_signature())
        try:
            if legacy_packet:
                from cachemonitor import overlay_data
                finalizer=overlay_data._summary_view
                def old_summary(*args,**kwargs):
                    own=finalizer(*args,**kwargs);own.pop('_speed_eligible',None)
                    return own
                monkeypatch.setattr(overlay_data,'_summary_view',old_summary)
            coordinator=ingest.PreparedCohort(store,None,None,speed_writer=warnings)
            assert list(coordinator.convert(catalog))[-1]['ready']
            assert entered.wait(5)
            with PreparedReader(value.path) as reader,reader.pin(store.token) as view:
                for key in sources:
                    assert ('_speed_eligible' not in view.derived(*key,'summary')['own']) is legacy_packet
            latest_queries=[]
            projected=Snapshot.projected_calls
            def project(view,owner,fields,**query):
                if query.get('keys') is not None:latest_queries.append((owner,tuple(query['keys'])))
                return projected(view,owner,fields,**query)
            monkeypatch.setattr(Snapshot,'projected_calls',project)
            publication=PreparedPublisher(0).publish(dict(homes=['home']),store,coordinator.current_speeds())
            data=next(row for row in publication['overlay_sessions'] if row['id']=='owner')
            expected={(key,(source['history'][-1]['key'],)) for key,source in sources.items()}
            assert set(latest_queries)==(expected if legacy_packet else set())
            assert set(data['members'])==set(sources)
            assert data['latest']['key'].startswith('child-call-')
            app=QApplication.instance() or QApplication([])
            content=OverlayContent();content.set_content(data)
            assert ('출력 속도 경고 분석 중' in str(content.states())) is pending
            if content.rows():content.pin_call(content.call_id(content.rows()[0]))
            assert ('출력 속도 경고 분석 중' in str(content.states())) is pending
            assert (data['speed_health'].get('active') is None) is pending
        finally:
            release.set();warnings.close()
