import json

from cachemonitor.model_evidence import EvidenceStore, EvidenceReader, compare, summary
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.index import UsageIndex
from test_index import finish
from test_core import fixture_home


def test_observer_data_location_is_independent_of_msix_localappdata(monkeypatch,tmp_path):
    from cachemonitor.model_evidence import default_path
    first=default_path()
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'package-virtualized-local'))
    assert default_path()==first
    assert '.cachemonitor' in first.parts


def test_default_index_joins_shared_evidence_but_explicit_qa_index_is_isolated(tmp_path,monkeypatch):
    from cachemonitor.model_evidence import default_path
    home,_=fixture_home(tmp_path)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    index=UsageIndex([home])
    try:assert index.model_evidence.path==default_path()
    finally:index.close()
    index=UsageIndex([home],tmp_path/'qa/index.sqlite')
    try:assert index.model_evidence.path==tmp_path/'qa/model-evidence.sqlite'
    finally:index.close()
    index=UsageIndex([home],tmp_path/'qa/index.sqlite',model_evidence_path=tmp_path/'explicit-wire.sqlite')
    try:assert index.model_evidence.path==tmp_path/'explicit-wire.sqlite'
    finally:index.close()


def test_late_evidence_refreshes_index_and_analysis_without_changing_usage(tmp_path):
    home,_=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    store=EvidenceStore(tmp_path/'model-evidence.sqlite')
    engine=AnalysisEngine()
    try:
        first=finish(index)['sessions']
        assert first[0]['history'][0]['model_match']=='확인 불가'
        engine.ingest(first)
        before=first[0]['history'][0]
        store.write(home,'attempt',10000,'WebSocket',before['key'],'gpt-6-astra','gpt-5.6-luna','completed')
        second=finish(index,10011)['sessions']
        after=second[0]['history'][0]
        assert second[0]['usage_revision']>first[0]['usage_revision']
        assert engine.ingest(second)
        assert after['model_match']=='불일치'
        assert after['transport']=='WebSocket'
        assert after['transport_source']=='response_id'
        assert before['transport_source']=='log_time'
        for field in ('input','output','cached','key','effort'):
            assert after[field]==before[field]
        assert after['model']=='gpt-6-astra' and after['model_source']=='wire'
        assert not engine.ingest(finish(index,10012)['sessions'])
        # Unknown source homes and legacy IDs never receive somebody else's evidence.
        assert index.model_evidence.enrich(tmp_path/'other',[before])[0]['model_match']=='확인 불가'
    finally:
        index.close();store.close()
    restored=UsageIndex([home],tmp_path/'index.sqlite')
    try: assert finish(restored)['sessions'][0]['history'][0]['model_match']=='불일치'
    finally:restored.close()


def test_transport_wire_evidence_priority_conflict_scope_and_missing_model(tmp_path):
    store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(tmp_path/'wire.sqlite')
    home=tmp_path/'home'
    rows=[{'key':'r','transport':'WebSocket','transport_source':'log_time',
           'transport_endpoint':'/old-path','transport_turn':'old-turn'}]
    try:
        # A response with no model fields still directly proves its wire protocol.
        store.write(home,'a',1,'HTTP/SSE','r',status='created')
        reader.poll();row=reader.enrich(home,rows)[0]
        assert row['transport']=='HTTP/SSE' and row['transport_source']=='response_id'
        assert row['model_match']=='확인 불가'
        assert row['transport_endpoint']==row['transport_turn']==''
        assert reader.enrich(tmp_path/'other',rows)[0]['transport_source']=='log_time'
        assert reader.enrich(home,[{'key':'other'}])[0].get('transport_source') is None
        store.write(home,'b',2,'HTTP/SSE','r',status='completed')
        reader.poll();assert reader.enrich(home,rows)[0]['transport_source']=='response_id'
        store.write(home,'c',3,'WebSocket','r',status='completed')
        reader.poll();row=reader.enrich(home,rows)[0]
        assert row['transport']=='미확인' and row['transport_source']=='conflict'
        # Missing wire data does not promote a time-based guess to direct evidence.
        store.write(home,'d',4,'','empty',status='completed')
        reader.poll()
        assert reader.enrich(home,[{**rows[0],'key':'empty'}])[0]['transport_source']=='log_time'
    finally:reader.close();store.close()


def test_conflicts_missing_fields_and_explicit_model_names(tmp_path):
    path=tmp_path/'evidence.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    home=tmp_path/'home';rows=[{'key':'r'}]
    try:
        store.write(home,'a',1,'WebSocket','r','gpt-6-astra','','created')
        reader.poll();assert reader.enrich(home,rows)[0]['model_match']=='확인 불가'
        store.write(home,'a',1,'WebSocket','r','gpt-6-astra','gpt-6-astra','completed')
        reader.poll();assert reader.enrich(home,rows)[0]['model_match']=='일치'
        store.write(home,'b',2,'HTTP/SSE','r','gpt-6-astra','other','completed')
        reader.poll();row=reader.enrich(home,rows)[0]
        assert row['model_match']=='관측 충돌' and row['model_evidence']=='관측 충돌'
        assert compare('gpt-6-astra','gpt-6-astra-2026-09-01')=='불일치'
        assert '1/2건' in summary([{'model_match':'일치'},{}])
    finally:reader.close();store.close()


def test_evidence_does_not_persist_untrusted_strings(tmp_path):
    store=EvidenceStore(tmp_path/'e.sqlite')
    store.write(tmp_path,'a',1,'WebSocket','r','model\nAuthorization: SECRET','{"prompt":"PRIVATE"}','arbitrary')
    text=json.dumps(store.db.execute('select * from model_observations').fetchall())
    store.close()
    assert 'SECRET' not in text and 'PRIVATE' not in text


def test_request_tier_is_joined_by_response_id_and_never_replaced_by_response_tier(tmp_path):
    path=tmp_path/'tier.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    home=tmp_path/'home'
    try:
        store.write(home,'a',1,'HTTP/SSE','r','m','m','completed',
                    requested_service_tier='priority',response_service_tier='default')
        reader.poll()
        row=reader.enrich(home,[{'key':'r','service_tier':'미확인'}])[0]
        assert row['service_tier']=='priority' and row['requested_service_tier']=='priority'
        assert row['response_service_tier']=='default' and row['service_tier_source']=='wire'
        assert reader.enrich(tmp_path/'other',[{'key':'r','service_tier':'미확인'}])[0]['service_tier']=='미확인'
        store.write(home,'b',2,'HTTP/SSE','only-response','m','m','completed',response_service_tier='priority')
        reader.poll()
        row=reader.enrich(home,[{'key':'only-response','service_tier':'미확인'}])[0]
        assert row['service_tier']=='미확인'
        store.write(home,'c',3,'HTTP/SSE','r','m','m','completed',requested_service_tier='default')
        reader.poll();row=reader.enrich(home,[{'key':'r'}])[0]
        assert row['service_tier']=='미확인' and row['mode_evidence']=='요청 등급 관측 충돌'
    finally:reader.close();store.close()


def test_overlay_never_promotes_response_default_to_request_standard(tmp_path):
    from cachemonitor.overlay_data import summarize_session
    from cachemonitor.pricing import token_cost
    path=tmp_path/'tier.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    home=tmp_path/'home'
    original=dict(key='r',ts=1,model='gpt-6-astra',service_tier='미확인',turn='turn',
                  input=10000,cached=9000,written=0,output=100,reasoning=50,rate=90)
    def display(row):
        priced={**row,**token_cost(row)}
        return summarize_session(dict(id='s',home=str(home),history=[priced])),priced
    try:
        initial,price=display(original)
        assert initial['assumed']==0 and price['cost'] is None
        for status in ('created','incomplete','completed'):
            store.write(home,'a',1,'WebSocket','r','gpt-6-astra','gpt-6-astra',status,response_service_tier='default')
            reader.poll();joined=reader.enrich(home,[original])[0];result,after=display(joined)
            assert joined['service_tier']=='미확인' and not after['price_assumed']
            assert after['cost']==price['cost']
            assert result['assumed']==0
            assert result['mode']=='미확인'  # Response tier does not fill the unobserved request mode.
        # Explicit Fast request remains Fast, including its existing cost policy.
        result,after=display({**joined,'service_tier':'priority'})
        assert result['mode']=='Fast' and after['price_tier']=='Fast'
        assert display(reader.enrich(tmp_path/'other',[original])[0])[0]['assumed']==0
        assert display(reader.enrich(home,[{**original,'key':'other'}])[0])[0]['assumed']==0
        # Conflicting, missing and different response rates cannot remove an assumption.
        store.write(home,'b',2,'WebSocket','r','gpt-6-astra','gpt-6-astra','completed',response_service_tier='priority')
        reader.poll();assert display(reader.enrich(home,[original])[0])[0]['assumed']==0
        for flags in ({'observation_missing':True},{'conflict':True},{}):
            key='single'+str(len(flags))+str(flags.get('conflict',False))
            store.write(home,key,3,'WebSocket',key,'gpt-6-astra','gpt-6-astra','completed',response_service_tier='priority',**flags)
            reader.poll();assert display(reader.enrich(home,[{**original,'key':key}])[0])[0]['assumed']==0
    finally:reader.close();store.close()
