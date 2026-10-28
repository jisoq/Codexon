"""Memory pressure must preserve analysis and allow subsequent publication."""
import time

import pytest

from cachemonitor.prepared_delivery import PreparedPublisher
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionChannel, empty_snapshot


def test_startup_progress_arrives_while_writer_initialization_is_blocked(tmp_path,monkeypatch):
    from threading import Event
    from cachemonitor import prepared_runtime
    entered,release=Event(),Event()
    def blocked_store(*args,**kwargs):
        entered.set()
        assert release.wait(10),'writer initialization was not released'
        raise InterruptedError('controlled startup cancellation')
    monkeypatch.setattr(prepared_runtime,'PreparedStore',blocked_store)
    writer=PreparedWriter([],tmp_path/'source.sqlite',None,tmp_path/'quota.sqlite')
    try:
        assert entered.wait(5)
        assert writer.output.receiver.poll(5)
        message=writer.output.take()
        assert message==dict(kind='progress',scope='startup',stage='starting')
        assert not writer.output.receiver.poll()
    finally:
        writer.stopped.set();release.set();writer.close()


def test_startup_advisory_never_blocks_or_overwrites_reliable_delivery():
    from threading import Event,Thread
    from cachemonitor.prepared_runtime import Mailbox
    stopped=Event();output=Mailbox(stopped)
    publication=dict(kind='snapshot',value={'sessions':[]})
    first=dict(kind='progress',scope='startup',stage='collecting',processed=1,total=20)
    try:
        for done in range(1,21):
            assert output.offer_progress(dict(first,processed=done))
        assert output.receiver.poll()
        delivered=Event()
        def publish():
            output.put(publication);delivered.set()
        producer=Thread(target=publish,daemon=True);producer.start()
        assert delivered.wait(2),'pending progress held canonical publication'
        producer.join(2)
        assert not output.offer_progress(first)
        assert output.take()==publication
        assert not output.receiver.poll(),'replacing progress added another wake token'
        assert output.offer_progress(first)
        assert output.take()==first and not output.receiver.poll()
        reliable=dict(kind='error',error='visible failure')
        assert output.put(reliable)
        assert not output.offer_progress(first)
        next_delivered=Event()
        def publish_after_error():
            output.put(publication);next_delivered.set()
        following=Thread(target=publish_after_error,daemon=True);following.start()
        assert output.take()==reliable
        assert next_delivered.wait(2)
        assert output.take()==publication
        following.join(2)
        with pytest.raises(ValueError):output.offer_progress(dict(kind='progress',id=1))
    finally:
        stopped.set();output.stop();output.close()


def test_startup_collection_counts_are_sampled_and_unavailable_catalog_is_unmeasured(tmp_path,monkeypatch):
    from queue import Queue
    from types import SimpleNamespace
    from cachemonitor import prepared_runtime
    from cachemonitor.usage_collection import CollectionCatalogUnavailable
    clock=[100.];offers=[];requested=Queue();samples=Queue()
    real_read=prepared_runtime.read_catalog
    real_offer=prepared_runtime.Mailbox.offer_progress
    def read_catalog(*args,**kwargs):
        requested.put(True)
        action=samples.get(timeout=10)
        if action=='cancel':raise InterruptedError('controlled reader cancellation')
        if action=='unavailable':
            raise CollectionCatalogUnavailable('controlled catalog outage')
        return real_read(*args,**kwargs)
    def offer(output,value):
        offers.append((clock[0],value))
        return real_offer(output,value)
    monkeypatch.setattr(prepared_runtime,'read_catalog',read_catalog)
    monkeypatch.setattr(prepared_runtime.Mailbox,'offer_progress',offer)
    monkeypatch.setattr(prepared_runtime,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite');channel.start_live()
    snapshot=empty_snapshot(channel.homes,channel.path,time.time());snapshot['index'].update(done=1,files=10)
    writer=PreparedWriter(channel.homes,channel.path,None,tmp_path/'quota.sqlite')
    try:
        requested.get(timeout=10)
        samples.put('unavailable');writer.sync();requested.get(timeout=5)
        for sequence,done,at in ((1,1,100.),(2,2,100.2),(3,3,100.9),(4,4,101.)):
            clock[0]=at;snapshot['index']['done']=done;channel.publish(snapshot,'sampled-preparation',sequence)
            samples.put('read');writer.sync()
            requested.get(timeout=5)
        measured=[(at,value['processed']) for at,value in offers if 'processed' in value]
        assert measured==[(100.,1),(101.,4)]
        clock[0]=102.;samples.put('unavailable');writer.sync()
        requested.get(timeout=5)
        assert offers[-1][1]==dict(kind='progress',scope='startup',stage='collecting')
        count=len(offers)
        clock[0]=103.;samples.put('unavailable');writer.sync();requested.get(timeout=5)
        assert len(offers)==count
        assert all(value['scope']=='startup' for _,value in offers)
        assert writer.output.receiver.poll()
        assert writer.output.take()==dict(kind='progress',scope='startup',stage='collecting')
        assert not writer.output.receiver.poll()
    finally:
        writer.stopped.set();samples.put('cancel');writer.close();channel.close()


@pytest.mark.parametrize('failure',['adopt','publish'])
def test_writer_survives_publication_capacity_and_recovers_on_sync(tmp_path,monkeypatch,failure):
    from cachemonitor import prepared_delivery
    from cachemonitor.prepared_store import CapacityExceeded
    from test_data_contract import source

    instances=[]
    class Limited(PreparedPublisher):
        limited=True
        def __init__(self,*args):
            super().__init__(*args)
            instances.append(self)
        def adopt(self,*args,**kwargs):
            if self.limited and failure=='adopt':raise CapacityExceeded('pressure')
            return super().adopt(*args,**kwargs)
        def publish(self,*args,**kwargs):
            if self.limited and failure=='publish':raise CapacityExceeded('pressure')
            return super().publish(*args,**kwargs)
    monkeypatch.setattr(prepared_delivery,'PreparedPublisher',Limited)
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite')
    channel.start_live()
    snapshot=empty_snapshot(channel.homes,channel.path,time.time())
    value=source();value['home']=channel.homes[0]
    snapshot.update(sessions=[value],usage_collection_complete=True,request_activity=[],quota_by_home={})
    snapshot['index'].update(loading=False,usage_complete=True)
    channel.publish(snapshot,'pressure',1)
    writer=PreparedWriter(channel.homes,channel.path,None,tmp_path/'quota.sqlite')
    def receive(kind):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            assert writer.output.receiver.poll(max(0,deadline-time.monotonic()))
            message=writer.output.take()
            assert message['kind']!='fatal',message
            if message['kind']==kind:return message
        raise AssertionError('writer did not produce the requested state')
    try:
        assert receive('error')['capacity']
        assert writer.thread.is_alive()
        instances[0].limited=False
        writer.sync()
        result=receive('snapshot')
        assert result['value']['overlay_sessions'][0]['id']==value['id']
        assert writer.thread.is_alive()
    finally:
        writer.close()
        channel.close()


@pytest.mark.parametrize('change',['title','membership'])
def test_warning_completion_preserves_failed_canonical_adoption(tmp_path,monkeypatch,change):
    from copy import deepcopy
    from threading import Event
    from cachemonitor import prepared_delivery,prepared_speed_writer
    from cachemonitor.prepared_store import CapacityExceeded
    from test_data_contract import source

    fail_adoption=Event();allow_completion=Event();completed=Event()
    class Limited(PreparedPublisher):
        def adopt(self,*args,**kwargs):
            if fail_adoption.is_set():
                fail_adoption.clear()
                raise CapacityExceeded('canonical publication pressure')
            return super().adopt(*args,**kwargs)
    class Warnings(prepared_speed_writer.PreparedWarningWorker):
        def commit_completed(self):
            if not allow_completion.is_set():return None
            result=super().commit_completed()
            if result is not None:completed.set()
            return result
    monkeypatch.setattr(prepared_delivery,'PreparedPublisher',Limited)
    monkeypatch.setattr(prepared_speed_writer,'PreparedWarningWorker',Warnings)
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite')
    channel.start_live()
    snapshot=empty_snapshot(channel.homes,channel.path,time.time())
    value=source();value['home']=channel.homes[0]
    snapshot.update(sessions=[value],usage_collection_complete=True,request_activity=[],quota_by_home={})
    snapshot['index'].update(loading=False,usage_complete=True)
    channel.publish(snapshot,'pressure',1)
    writer=PreparedWriter(channel.homes,channel.path,None,tmp_path/'quota.sqlite')
    def receive(predicate):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            assert writer.output.receiver.poll(max(0,deadline-time.monotonic()))
            message=writer.output.take()
            assert message['kind']!='fatal',message
            if predicate(message):return message
        raise AssertionError('writer did not recover the canonical publication')
    try:
        baseline=receive(lambda row:row['kind']=='snapshot')
        updated=deepcopy(snapshot)
        updated['sessions'][0]['title']='Updated'
        if change=='membership':
            added=source();added.update(id='added',home=channel.homes[0],title='Added')
            added['history'][0]['key']='added-call'
            updated['sessions'].append(added)
        fail_adoption.set()
        channel.publish(updated,'pressure',2)
        assert receive(lambda row:row['kind']=='error')['capacity']
        allow_completion.set()
        writer.control.put('speed')
        assert completed.wait(10),'warning completion did not reach the canonical writer'
        result=receive(lambda row:row['kind']=='snapshot' and row['token']!=baseline['token'])
        sessions={row['id']:row for row in result['value']['sessions']}
        assert sessions[value['id']]['title']=='Updated'
        assert set(sessions)==({value['id'],'added'} if change=='membership' else {value['id']})
        assert writer.thread.is_alive()
    finally:
        allow_completion.set()
        writer.close()
        channel.close()


def test_query_evicts_optional_caches_before_one_retry(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.prepared_query import PreparedQuery
    from cachemonitor.prepared_store import CapacityExceeded
    query=PreparedQuery(tmp_path/'unused.sqlite')
    query.finance_cache.entries['old']=('cached',100)
    query.finance_cache.content_bytes=100
    closed=[];calls=[]
    query.engine=SimpleNamespace(close=lambda:closed.append(True))
    def run(*args):
        calls.append(True)
        if len(calls)==1:raise CapacityExceeded('pressure')
        assert query.engine is None and not query.finance_cache.entries
        return {'kind':'result'}
    monkeypatch.setattr(query,'_call',run)
    assert query({},None,None)=={'kind':'result'}
    assert len(calls)==2 and closed==[True]


@pytest.mark.parametrize('initial',[False,True])
def test_publication_retry_requests_snapshot_without_restarting_worker(initial):
    from types import SimpleNamespace
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.app_errors import detect
    events=[]
    window=SimpleNamespace(worker=SimpleNamespace(isRunning=lambda:True,
        request_snapshot=lambda:events.append('sync')),analysis_errors=[{'publication_failed':True}],
        preparation=object() if initial else None,sync_preparation=lambda:events.append('preparation'),
        client_cache={},pending_logical=None,render=lambda:events.append('render'))
    Dashboard.retry_analysis(window)
    assert events==(['sync','render','preparation'] if initial else ['sync','render'])
    assert bool(window.analysis_errors)!=initial
    fault=detect('analysis',dict(publication_failed=True),time.time())
    assert fault is not None and fault.code=='publication'


@pytest.mark.parametrize('abandon', [False, True], ids=['complete', 'restart'])
def test_publisher_fragments_each_project_path_through_mailbox(tmp_path, abandon):
    from dataclasses import replace
    from threading import Event

    from cachemonitor.analysis_delivery import SnapshotReceiver
    from cachemonitor.analysis_engine import retained_bytes
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner
    from cachemonitor.prepared_runtime import Mailbox
    from cachemonitor import prepared_store as storage
    from test_data_contract import source

    limits = replace(storage.Limits(), page_objects_bytes=64*1024)
    paths = [f'C:\\scratch\\{number:03d}-' + 'x'*2048 for number in range(48)]
    stopped = Event()
    output = Mailbox(stopped)
    receiver = SnapshotReceiver()

    try:
        with storage.PreparedStore(tmp_path/'project-paths.sqlite', limits=limits) as store:
            stage = store.begin(('source', 1))
            for ordinal, path in enumerate(paths):
                value = source()
                value.update(id=f'session-{ordinal:03d}', cwd=path,
                             project='unassigned:home', project_name='Scratch')
                stage_owner(stage, prepare_owner(value, owns=lambda *_: True),
                            dict(dict.fromkeys(storage.DEPENDENCIES, 1), source_digest=f'{1:064x}'), ordinal)
            stage.publish(('source', 1), ready=True)
            publisher = PreparedPublisher(0)
            publisher.adopt(store, [('home', f'session-{number:03d}') for number in range(48)],
                            selected=[])
            fragments = publisher.publish_pages(dict(homes=['home']), store)
            path_fragments = 0
            delivered = None

            def consume(fragment):
                message = dict(kind='snapshot', value=fragment)
                assert retained_bytes(message, limits.page_objects_bytes) <= limits.page_objects_bytes
                assert output.put(message)
                assert output.receiver.poll()
                return receiver.receive(output.take()['value'])

            if abandon:
                for fragment in fragments:
                    assert consume(fragment) is None
                    if fragment.get('filter_choices', {}).get('project_paths'):
                        assert fragment['more']
                        break
                else:
                    pytest.fail('publication did not split its project paths')
                fragments.close()
                assert receiver.filter_choices == {}
                publisher.reset()
                fragments = publisher.publish_pages(dict(homes=['home']), store)

            for fragment in fragments:
                if fragment.get('filter_choices', {}).get('project_paths'):
                    path_fragments += 1
                result = consume(fragment)
                if fragment['more']:
                    assert result is None
                else:
                    delivered = result
            assert path_fragments > 1
            assert delivered['filter_choices']['project_paths'] == {'unassigned:home': paths}
            assert delivered['filter_choices']['project_labels'] == {'unassigned:home': 'Scratch'}
            assert delivered['sessions'] == []
            assert publisher.owners == {}
    finally:
        stopped.set()
        output.stop()
        output.close()


def test_receiver_extends_project_paths_and_discards_abandoned_nested_choices():
    from cachemonitor.analysis_delivery import SnapshotReceiver

    receiver = SnapshotReceiver()

    def frame(sequence, paths, *, epoch='first', reset=False, more=False):
        return dict(protocol=2, epoch=epoch, sequence=sequence, reset=reset, more=more,
            sessions=[], overlay_sessions=[], removed=[], model_candidates=[], cache_candidates=[],
            filter_choices=dict(project_paths={'project': paths}))

    assert receiver.receive(frame(1, ['C:/one'], reset=True, more=True)) is None
    initial = receiver.receive(frame(2, ['C:/two']))
    assert initial['filter_choices']['project_paths'] == {'project': ['C:/one', 'C:/two']}

    assert receiver.receive(frame(3, ['C:/abandoned'], more=True)) is None
    assert receiver.filter_choices['project_paths'] == {'project': ['C:/one', 'C:/two']}
    assert receiver.receive(frame(1, ['C:/new-one'], epoch='replacement', reset=True, more=True)) is None
    final = receiver.receive(frame(2, ['C:/new-two'], epoch='replacement'))
    assert final['filter_choices']['project_paths'] == {'project': ['C:/new-one', 'C:/new-two']}
    assert initial['filter_choices']['project_paths'] == {'project': ['C:/one', 'C:/two']}
    assert receiver.receive(frame(4, ['C:/late'])) is None
