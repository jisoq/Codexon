"""Memory pressure must preserve analysis and allow subsequent publication."""
import time

import pytest

from cachemonitor.prepared_delivery import PreparedPublisher
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionChannel, empty_snapshot


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


def test_publication_retry_requests_snapshot_without_restarting_worker():
    from types import SimpleNamespace
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.app_errors import detect
    events=[]
    window=SimpleNamespace(worker=SimpleNamespace(isRunning=lambda:True,
        request_snapshot=lambda:events.append('sync')),analysis_errors=[{'publication_failed':True}],
        client_cache={},pending_logical=None,render=lambda:events.append('render'))
    Dashboard.retry_analysis(window)
    assert events==['sync','render']
    fault=detect('analysis',window.analysis_errors[0],time.time())
    assert fault is not None and fault.code=='publication'
