"""Narrow worker rows preserve the complete display and process contracts."""
import copy
import json
import pickle

from cachemonitor.analysis_records import compact_record, public_record, public_records
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_delivery import SnapshotPublisher
from cachemonitor.overlay_data import OverlaySummaries
from test_comparison import history


def test_display_aliases_round_trip_without_changing_prices_or_types():
    row=dict(key='call',turn='turn',model='gpt-6-sol',service_tier='Standard',
             session_ordinal=1,home='fixture',sid='session',cost=1.,cache_incident_id=None,
             call_id='call',request_id='turn',analysis_model='gpt-6-sol',
             price_model='gpt-6-sol',price_tier='Standard',price_assumed=False,
             long_context=False,cache_degradation=False,price_profile='saved price profile')
    original=copy.deepcopy(row)
    compact=compact_record(row)
    assert type(compact) is dict and len(compact)==len(row)-8
    assert public_record(compact)==original and row==original
    exceptional={**row,'price_model':'different-price-model','price_tier':'Fast',
                 'price_assumed':True,'long_context':0,'cache_degradation':None}
    restored=public_record(compact_record(exceptional))
    assert restored==exceptional and type(restored['long_context']) is int
    assert restored['price_profile']=='saved price profile'
    assert public_record({'key':'unprepared'})=={'key':'unprepared'}


def test_view_boundaries_match_full_rows_after_usage_and_metadata_changes(monkeypatch):
    import cachemonitor.analysis_engine as implementation
    snapshot=dict(ts=110,sessions=[history()],homes=['fixture'],errors=[],unassigned=[],
                  index=dict(loading=False,done=1,files=1))
    q=dict(page=2,start=0,end=110,now=110,period='all',model='',source='',archived=True,
           unit='response',method='mean',metric='cost',band=None,granularity='auto',presentation=True,
           explorer=dict(view='calls',search='',sort='cost_desc'),table_windows={'records':dict(start=0,size=128)})
    compact=AnalysisEngine()
    full=AnalysisEngine()
    for iteration in range(3):
        if iteration==1:
            snapshot['sessions'][0]['history'][-1]['output']+=9
            snapshot['sessions'][0]['history'][-1]['total']+=9
        elif iteration==2:
            snapshot['sessions'][0]['title']='Changed title'
        with monkeypatch.context() as patch:
            patch.setattr(implementation,'compact_record',lambda row:row)
            full.ingest(copy.deepcopy(snapshot['sessions']))
        compact.ingest(copy.deepcopy(snapshot['sessions']))
        for page in (0,1,2):
            expected=full.page_query({**q,'page':page})
            actual=compact.page_query({**q,'page':page})
            assert actual==expected
            assert json.loads(json.dumps(actual))==json.loads(json.dumps(expected))
            assert pickle.loads(pickle.dumps(actual))==expected
        source=next(iter(full.sessions.values()))
        row=source['prepared']['history'][-1]
        assert compact.record(row['home'],row['sid'],row['key'])==row
        expected_overlay=SnapshotPublisher().publish(snapshot,full,OverlaySummaries().collect(full))
        actual_overlay=SnapshotPublisher().publish(snapshot,compact,OverlaySummaries().collect(compact))
        for value in (expected_overlay,actual_overlay):value.pop('epoch')
        assert actual_overlay==expected_overlay


def test_public_expansion_keeps_shared_rows_and_does_not_mutate_worker_values():
    source=dict(key='call',turn='',model='model',service_tier='Standard',home='fixture',sid='session',
                session_ordinal=1,cost=None)
    value={'rows':[source],'lookup':(source,)}
    expanded=public_records(value)
    assert expanded['rows'][0] is expanded['lookup'][0]
    assert 'call_id' not in source
    assert type(expanded['rows'][0]) is dict
    assert expanded['rows'][0]['call_id']=='call'
