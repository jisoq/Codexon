from contextlib import closing

import pytest

from cachemonitor.index import UsageIndex
from cachemonitor.usage_collection import CollectionChannel, CollectionClient, CollectorService
from test_collection_preparation import cached_sources
from test_collection_transport import snapshot
from test_core import TID, usage


def complete(service, now):
    from time import monotonic
    deadline=monotonic()+5
    while monotonic()<deadline:
        snapshot=service.poll(now,detailed=False)
        assert not snapshot['usage_errors']
        if snapshot['usage_collection_complete'] and not snapshot['index']['loading']:
            return snapshot
    pytest.fail('Demanded collection did not complete')


def test_reopening_full_history_reuses_saved_bodies_and_releases_evicted_weight(cached_sources,monkeypatch):
    source=cached_sources
    rebuilt=[]
    rebuilding=UsageIndex.rebuild_steps
    def track(index,key,now):
        rebuilt.append(key)
        yield from rebuilding(index,key,now)
    monkeypatch.setattr(UsageIndex,'rebuild_steps',track)
    with closing(CollectorService([source.home],source.target,source.evidence)) as service, \
            closing(CollectionClient([source.home],source.target,source.evidence)) as client:
        client.set_demand([(None,TID)],aggregate=True)
        complete(service,20001)
        assert not rebuilt
        full_weight=service.index.replay.weight
        assert full_weight>0
        for number in range(3):
            client.set_demand([(None,TID)])
            selected=complete(service,20002+number*2)
            assert selected['catalog_scope']=='selected'
            assert set(service.index.monitor.sessions)=={(str(source.home),TID)}
            assert service.index.replay.weight<full_weight
            assert service.channel.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==2
            assert not rebuilt
            client.set_demand([(None,TID)],aggregate=True)
            restored=complete(service,20003+number*2)
            assert restored['catalog_scope']=='full'
            assert set(service.index.monitor.sessions)=={(str(source.home),sid) for sid in source.histories}
            assert all(hasattr(session,'_replay_body') for session in service.index.monitor.sessions.values())
            assert service.index.replay.weight==full_weight
            assert not rebuilt


def test_reopening_history_rebuilds_changed_source(cached_sources,monkeypatch):
    import json
    source=cached_sources
    rebuilt=[]
    rebuilding=UsageIndex.rebuild_steps
    def track(index,key,now):
        rebuilt.append(key)
        yield from rebuilding(index,key,now)
    monkeypatch.setattr(UsageIndex,'rebuild_steps',track)
    with closing(CollectorService([source.home],source.target,source.evidence)) as service, \
            closing(CollectionClient([source.home],source.target,source.evidence)) as client:
        client.set_demand([(None,TID)],aggregate=True)
        complete(service,20001)
        client.set_demand([(None,TID)])
        complete(service,20002)
        other=source.home/'second.jsonl'
        with other.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(usage('r3',tid=source.other,input=700,cached=100))+'\n')
        client.set_demand([(None,TID)],aggregate=True)
        complete(service,20003)
        assert (str(source.home),source.other) in rebuilt
        assert (str(source.home),TID) not in rebuilt
        assert service.channel.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==2


@pytest.mark.parametrize('partial',['selected','loading','incomplete'])
def test_only_complete_full_publication_retires_saved_bodies(tmp_path,partial):
    with closing(CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')) as channel:
        full=snapshot(channel)
        full['catalog_scope']='full'
        full['sessions'].append(dict(home=channel.homes[0],id='other',history=[dict(key='two',output=7)],
            collection_complete=True,status='idle'))
        channel.publish(full,'epoch',1)
        narrowed={**full,'ts':101.,'sessions':full['sessions'][:1],'index':dict(full['index'])}
        if partial=='selected':narrowed['catalog_scope']='selected'
        elif partial=='loading':narrowed['index']['loading']=True
        else:narrowed['usage_collection_complete']=False
        channel.publish(narrowed,'epoch',2)
        assert set(channel.published)=={(channel.homes[0],'session')}
        assert channel.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==2
        assert not channel.removed_sessions
        authoritative={**full,'ts':102.,'sessions':full['sessions'][:1]}
        channel.publish(authoritative,'epoch',3)
        assert channel.db.execute('SELECT sid FROM sessions').fetchall()==[('session',)]
        assert channel.removed_sessions=={(channel.homes[0],'other')}
