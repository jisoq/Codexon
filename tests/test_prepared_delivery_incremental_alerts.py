"""Live publications reconcile notification heads only for committed changes."""
import pytest

from cachemonitor.analysis_delivery import MODEL_KEYS
from cachemonitor.prepared_delivery import PreparedPublisher
from cachemonitor.prepared_health import split_summary
from cachemonitor import prepared_store as storage


def commit(store, revision, updates, removed=()):
    stage=store.begin(('source',revision))
    for number,confirmed in updates.items():
        sid=f'owner-{number:03d}';key=('home',sid)
        row=dict(key=sid+'-call',ts=101,session_ordinal=0,model_observation_ts=101,
            model_alert_confirmed=confirmed,requested_model='asked',response_model='reported')
        summary,sections=split_summary(dict(own=dict(home='home',id=sid,title=sid,
            cache_misses=dict(count=0,events=[]),cache_health=dict(incident=None))))
        stage.owner(*key,dict(home='home',id=sid,title=sid),
            dict(dict.fromkeys(storage.DEPENDENCIES,revision),source_digest=f'{revision:064x}'),
            source_ordinal=number,summary=summary,choices={},
            model_alerts={row['key']:tuple(row.get(name) for name in MODEL_KEYS)} if confirmed else {})
        stage.calls(key,[row],reconcile=True)
        stage.sections(key,sections,reconcile=True)
    for number in removed:
        key=('home',f'owner-{number:03d}')
        with store.read_current() as view:previous=view.owner(*key)
        stage.owner(*key,previous['metadata'],previous['dependencies'],source_ordinal=number)
        stage.remove_owner(key)
    return stage.publish(('source',revision),ready=True)


def test_selected_publication_reads_only_changed_global_notification_heads(tmp_path,monkeypatch):
    with storage.PreparedStore(tmp_path/'alerts.sqlite') as store:
        commit(store,1,{number:number==5 for number in range(65)})
        publisher=PreparedPublisher(100)
        publisher.adopt(store,[('home',f'owner-{number:03d}') for number in range(65)],selected=[])
        assert [item['sid'] for item in publisher.publish(dict(homes=['home']),store)['model_candidates']]==['owner-005']

        reads=[]
        heads=storage.Snapshot.publication_heads
        def selected_heads(view,*,after=None,owners=None):
            assert owners is not None,'A committed owner delta must not scan all notification heads'
            page=heads(view,after=after,owners=owners)
            reads.extend(row['owner'] for row in page.rows)
            return page
        monkeypatch.setattr(storage.Snapshot,'publication_heads',selected_heads)

        changed=('home','owner-006')
        commit(store,2,{6:True})
        publisher.adopt(store,[changed],selected=[])
        assert reads==[changed]
        assert [item['sid'] for item in publisher.publish(dict(homes=['home']),store)['model_candidates']]==['owner-006']
        publisher.reset()
        assert {item['sid'] for item in publisher.publish(dict(homes=['home']),store)['model_candidates']}=={'owner-005','owner-006'}

        reads.clear()
        commit(store,3,{6:False})
        publisher.adopt(store,[changed],selected=[])
        corrected=publisher.publish(dict(homes=['home']),store)['model_candidates']
        assert reads==[changed]
        assert len(corrected)==1 and corrected[0]['sid']=='owner-006'
        assert corrected[0]['model_alert_confirmed'] is False
        publisher.reset()
        assert [item['sid'] for item in publisher.publish(dict(homes=['home']),store)['model_candidates']]==['owner-005']

        reads.clear()
        commit(store,4,{},removed=[5])
        publisher.adopt(store,(),[('home','owner-005')],selected=[])
        publisher.reset()
        assert publisher.publish(dict(homes=['home']),store)['model_candidates']==[]
        assert reads==[]
        assert not store.db.in_transaction


def test_failed_incremental_adoption_restores_notification_replay(tmp_path,monkeypatch):
    with storage.PreparedStore(tmp_path/'alerts.sqlite') as store:
        key=('home','owner-000')
        commit(store,1,{0:True})
        publisher=PreparedPublisher(100)
        publisher.adopt(store,[key],selected=[])
        original=store.token
        publisher.publish(dict(homes=['home']),store)
        commit(store,2,{0:False})
        with monkeypatch.context() as limited:
            limited.setattr(publisher,'STATE_BYTES',1)
            with pytest.raises(storage.CapacityExceeded):publisher.adopt(store,[key],selected=[])
        assert publisher.token==original
        assert not store.db.in_transaction
        publisher.reset()
        assert publisher.pending_alerts[(key,'owner-000-call')]['model_alert_confirmed'] is True
        publisher.adopt(store,[key],selected=[])
        corrected=publisher.publish(dict(homes=['home']),store)['model_candidates']
        assert len(corrected)==1 and corrected[0]['model_alert_confirmed'] is False


def test_notification_head_projection_keeps_exact_keys_and_pages(tmp_path):
    with storage.PreparedStore(tmp_path/'heads.sqlite') as store:
        commit(store,1,{number:True for number in range(8)})
        selected=[('home','owner-001'),('home','owner-006')]
        with store.read_current() as view:
            assert {row['owner'] for row in view.pages(view.publication_heads,owners=selected)}==set(selected)
            assert list(view.pages(view.publication_heads,owners=[]))==[]
            with pytest.raises(ValueError):view.publication_heads(owners=selected*2)
