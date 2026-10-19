"""Sustained observed slowdowns, comparison boundaries and recovery."""
from copy import deepcopy
from types import SimpleNamespace
import weakref

from cachemonitor.speed_health import classify, SpeedHealth, MAX_BASELINE
from cachemonitor import speed_health as speed_module
from cachemonitor.analysis_engine import AnalysisEngine
import cachemonitor.analysis_engine as analysis_module
from cachemonitor.core import Session
from cachemonitor.analytics import call_identity


def session(speeds, sid='s', home='h', start=1000, **changes):
    rows=[]
    for i,speed in enumerate(speeds):
        rows.append(dict(key=f'{sid}-{i}',ts=start+i*100,input=80000,cached=72000,
                         output=2000,reasoning=500,model='model',effort='high',service_tier='Standard',
                         transport='WebSocket',transport_source='response_id',
                         response_status='completed',timing_valid=True,
                         completion_latency_ms=2000/speed*1000) | changes)
    source = Session(sid, home, title=sid).view(start)
    source.update(history=rows, usage_revision=1, collection_complete=True)
    return source


def health(s):return classify([s])[(s['home'],s['id'])]


def test_sustained_drop_freezes_baseline_and_requires_two_recoveries():
    normal=[100]*10
    assert not health(session(normal+[35,35]))['active']
    result=health(session(normal+[35]*10))
    assert result['active'] and result['baseline_speed']==100
    assert result['recent_speed']==35 and result['drop_percent']==65
    assert result['baseline_count']==10
    assert health(session(normal+[35]*3+[100]))['active']
    assert not health(session(normal+[35]*3+[100,100]))['active']
    # An isolated outlier and a broad normal distribution are not an incident.
    assert not health(session(normal+[35,100,35,100,35]))['active']
    assert not health(session([20,180]*5+[35]*3))['active']


def test_invalid_short_or_changed_conditions_cannot_complete_a_streak():
    for change in ({'response_status':'failed'},{'timing_valid':False},
                   {'mode_conflict':True},{'observation_missing':True},
                   {'transport_source':'log_time'},{'output':80},
                   {'effort':'low'},{'model':'other'},{'service_tier':'Priority'},
                   {'input':200000},{'cached':0}):
        s=session([100]*10+[35]*3)
        s['history'][-2].update(change)
        assert not health(s)['active'],change
    s=session([100]*10+[35]*3)
    s['history'][-1]['ts']+=8*86400
    assert not health(s)['active']


def test_peer_baseline_is_causal_same_home_and_never_child_aggregate():
    old=session([100]*10,'old')
    new=session([35]*3,'new',start=3000)
    result=classify([old,new])[('h','new')]
    assert result['active'] and result['baseline_source']=='peers'
    assert not classify([dict(old,home='different'),new])[('h','new')]['active']
    future=session([100]*10,'future',start=5000)
    assert not classify([future,new])[('h','new')]['active']
    insufficient=session([100]*3+[35]*3)
    assert not health(insufficient)['active']


def test_correction_and_duplicate_response_do_not_keep_false_evidence():
    s=session([100]*10+[35]*3)
    assert health(s)['active']
    with_duplicate=deepcopy(s)
    with_duplicate['history'].append(deepcopy(with_duplicate['history'][-1]))
    assert health(with_duplicate)['active']
    corrected=deepcopy(s)
    corrected['history'][-2]['completion_latency_ms']=20000
    assert not health(corrected)['active']
    duplicated=session([100]*10+[35]*2)
    duplicated['history'].append(deepcopy(duplicated['history'][-1]))
    assert not health(duplicated)['active']


def ingest(engine, sources, *changed):
    for source in changed:
        source['usage_revision'] += 1
    engine.ingest(sources)


def expected(engine):
    return classify(s['prepared'] for s in engine.sessions.values())


def appended(source, speeds, **changes):
    start = len(source['history'])
    for offset, speed in enumerate(speeds):
        ordinal = start + offset
        source['history'].append(dict(source['history'][0],
            key=f"{source['id']}-{ordinal}", ts=1000 + ordinal * 100,
            completion_latency_ms=2000 / speed * 1000) | changes)


def test_incremental_appends_corrections_and_removals_match_complete_replay():
    source = session([100] * 10 + [35] * 2)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    # This invalid row must reset the two-call streak, even though it is never
    # retained in a healthy peer pool.
    appended(source, [35], timing_valid=False)
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.results[('h', 's')]['active']
    assert tracker.metrics['proof_rows'] == 1
    appended(source, [35])
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.results[('h', 's')]['active']
    assert tracker.metrics['replayed_scopes'] == 0
    appended(source, [35, 35])
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert tracker.results[('h', 's')]['active']
    for recovered in range(2):
        appended(source, [100])
        ingest(engine, [source], source)
        assert tracker.collect(engine) == expected(engine)
        assert tracker.results[('h', 's')]['active'] is (recovered == 0)
    ingest(engine, [])
    assert tracker.collect(engine) == {}
    assert tracker.metrics['retained_rows'] == tracker.metrics['baseline_rows'] == tracker.metrics['live_states'] == 0


def test_changed_peer_scope_does_not_replay_unrelated_conditions():
    reference = session([100] * 10, 'reference')
    peer = session([35] * 3, 'peer', start=3000)
    other = session([90] * 500, 'other', model='other')
    sources = [reference, peer, other]
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, sources)
    assert tracker.collect(engine) == expected(engine)
    old_revision = engine.sessions[('h', 'other')]['content_revision']
    reference['history'][5]['completion_latency_ms'] = 100000
    ingest(engine, sources, reference)
    assert tracker.collect(engine) == expected(engine)
    assert engine.sessions[('h', 'other')]['content_revision'] == old_revision
    assert tracker.metrics['proof_rows'] == 10
    assert tracker.metrics['replay_rows'] == tracker.metrics['processed_rows'] == 13
    assert tracker.metrics['scanned_rows'] == 23
    assert tracker.metrics['replayed_scopes'] == 1
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['scanned_rows'] == tracker.metrics['processed_rows'] == 0
    reference['history'].pop(2)
    ingest(engine, sources, reference)
    assert tracker.collect(engine) == expected(engine)
    ingest(engine, [peer, other])
    assert tracker.collect(engine) == expected(engine)


def test_incremental_replay_preserves_condition_resets_late_rows_and_dedup_owners():
    source = session([100] * 10 + [35] * 3, 'a')
    source['history'][11]['model'] = 'other'
    duplicate = session([], 'b')
    duplicate['history'] = [deepcopy(source['history'][0])]
    peer = session([35] * 3, 'peer', start=4000)
    sources = [source, duplicate, peer]
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, sources)
    assert tracker.collect(engine) == expected(engine)
    # Confirmation moves the canonical response away from a pending owner.
    duplicate['history'][0].update(ts=999, model='other')
    source['_collection_pending'] = True
    ingest(engine, sources, source, duplicate)
    assert tracker.collect(engine) == expected(engine)
    late = session([100], 'late', start=950)
    sources.append(late)
    ingest(engine, sources)
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['replayed_scopes'] == 1
    # Removing the winning duplicate restores the remaining response owner.
    sources.remove(duplicate)
    ingest(engine, sources)
    assert tracker.collect(engine) == expected(engine)
    source['history'][-1]['ts'] += 8 * 86400
    ingest(engine, sources, source)
    assert tracker.collect(engine) == expected(engine)


def test_capacity_fallback_releases_incremental_state_and_keeps_exact_results(monkeypatch):
    source = session([100] * 10)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    with monkeypatch.context() as limited:
        limited.setattr(speed_module, 'MAX_METADATA_BYTES', 1)
        appended(source, [35, 35, 35])
        ingest(engine, [source], source)
        assert tracker.collect(engine) == expected(engine)
        assert tracker.metrics['capacity_fallback']
        assert tracker.metrics['retained_rows'] == tracker.metrics['live_states'] == 0
    appended(source, [100, 100])
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.metrics['capacity_fallback']
    assert tracker.metrics['retained_rows'] > 0

def test_invalid_duplicate_owner_changes_and_metadata_revisions_preserve_peers():
    reference = session([100] * 8, 'z-reference')
    duplicate = session([], 'a-owner')
    duplicate['history'] = [dict(row, ts=900 + i, timing_valid=False)
                            for i, row in enumerate(reference['history'][:3])]
    # A repeated key within the winner is also removed by AnalysisEngine.
    duplicate['history'].append(deepcopy(duplicate['history'][0]))
    peer = session([35] * 3, 'peer', start=3000)
    sources = [reference, duplicate, peer]
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, sources)
    keys = [call_identity(*owner, r['key']) for owner, s in engine.sessions.items()
            for r in s['prepared']['history']]
    assert len(keys) == len(set(keys))
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.results[('h', 'peer')]['active']
    duplicate['_collection_pending'] = True
    ingest(engine, sources, duplicate)
    assert tracker.collect(engine) == expected(engine)
    assert tracker.results[('h', 'peer')]['active']
    assert tracker.metrics['replayed_scopes'] == 1
    ingest(engine, [reference, peer])
    assert tracker.collect(engine) == expected(engine)


def test_metadata_only_replacement_releases_obsolete_observed_rows(monkeypatch):
    class ObservedRow(dict):
        pass
    original_compact = analysis_module.compact_record
    monkeypatch.setattr(analysis_module, 'compact_record',
                        lambda row: ObservedRow(original_compact(row)))
    source = session([100] * 10 + [35] * 3)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    obsolete = weakref.ref(engine.sessions[('h', 's')]['prepared']['history'][0])
    # Presentation metadata changes prepared row dictionaries but no speed
    # inputs. Projected samples must not retain those dictionaries.
    source['title'] = 'Changed title'
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['processed_rows'] == 0
    assert obsolete() is None


def test_peer_scope_capacity_falls_back_without_losing_observed_results(monkeypatch):
    monkeypatch.setattr(speed_module, 'MAX_TRACKED_SCOPES', 2)
    first = session([100] * 10 + [35] * 3, 'first', model='first-model')
    second = session([100] * 10, 'second', model='second-model')
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [first, second])
    assert tracker.collect(engine) == expected(engine)
    replacement = session([100] * 10, 'second', model='replacement-model')
    ingest(engine, [first, replacement], replacement)
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.metrics['capacity_fallback']
    third = session([100] * 10, 'third', model='third-model')
    ingest(engine, [first, replacement, third])
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['capacity_fallback']
    assert tracker.metrics['retained_rows'] == tracker.metrics['live_states'] == 0
    ingest(engine, [first, replacement])
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.metrics['capacity_fallback']
    ingest(engine, [replacement, first])
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['processed_rows'] == tracker.metrics['scanned_rows'] == 0


def test_condition_return_resets_active_frozen_baseline():
    source = session([100] * 10 + [35] * 3)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    first_incident = tracker.results[('h', 's')]['incident_id']
    appended(source, [100], model='other')
    appended(source, [35])
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert not tracker.results[('h', 's')]['active']
    appended(source, [35, 35])
    ingest(engine, [source], source)
    assert tracker.collect(engine) == expected(engine)
    assert tracker.results[('h', 's')]['incident_id'] != first_incident
    assert tracker.metrics['replayed_scopes'] == 0


def test_old_active_baseline_is_not_evicted_at_seven_days():
    source = session([100] * 10 + [35] * 11)
    for i, row in enumerate(source['history'][10:]):
        row['ts'] = 3000 + i * 86400
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    assert tracker.results[('h', 's')]['active']
    assert tracker.results[('h', 's')]['baseline_keys'] == [f's-{i}' for i in range(10)]
    for i in range(2):
        source['history'].append(dict(source['history'][0],
            key=f'recovery-{i}', ts=3000 + (11 + i) * 86400))
        ingest(engine, [source], source)
        assert tracker.collect(engine) == expected(engine)
        assert tracker.results[('h', 's')]['active'] is (i == 0)


def test_equal_timestamp_late_owner_replays_canonical_peer_order():
    reference = session([100] * 10, 'reference')
    peer = session([35] * 3, 'z', start=3000)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [reference, peer])
    assert tracker.collect(engine) == expected(engine)
    # Same timestamp, but canonical owner order places this before z's last
    # consumed row. It must not be appended after the saved scope frontier.
    early_owner = session([100], 'a', start=3200)
    ingest(engine, [reference, peer, early_owner])
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['replayed_scopes'] == 1


def test_unproven_engine_keeps_standalone_duplicate_semantics():
    first = session([100] * 10 + [35] * 3)
    duplicate = session([], 'duplicate')
    duplicate['history'] = [dict(first['history'][0], ts=0, timing_valid=False)]
    engine = SimpleNamespace(sessions={('h', s['id']): dict(prepared=s, revision=1)
                                     for s in (first, duplicate)})
    tracker = SpeedHealth()
    assert tracker.collect(engine) == classify([first, duplicate])
    assert tracker.metrics['fallback_reason'] == 'unproven input owner'
    assert tracker.metrics['retained_rows'] == tracker.metrics['live_states'] == 0
    # A fake revision is not trusted to skip the standalone classifier.
    duplicate['history'][0]['timing_valid'] = True
    assert tracker.collect(engine) == classify([first, duplicate])


def test_skipped_certificate_predecessor_cannot_hide_a_prior_correction():
    source = session([100] * 10 + [35] * 3)
    engine, tracker = AnalysisEngine(), SpeedHealth()
    ingest(engine, [source])
    assert tracker.collect(engine) == expected(engine)
    # The tracker misses a revision containing an old-record correction. The
    # next engine update is an append relative to that intervening revision.
    source['history'][2]['completion_latency_ms'] = 100000
    ingest(engine, [source], source)
    appended(source, [35])
    ingest(engine, [source], source)
    assert engine.sessions[('h', 's')]['speed_append_from'] is not None
    assert tracker.collect(engine) == expected(engine)
    assert tracker.metrics['replayed_scopes'] == 1
def test_main_monitor_keeps_current_speed_health_during_call_and_detail_selection():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_data import OverlaySummaries
    from cachemonitor.overlay_monitor import header_status
    app=QApplication.instance() or QApplication([])
    source=session([100]*10+[35]*3)
    engine=AnalysisEngine();engine.ingest([source])
    data=OverlaySummaries().collect(engine)[0]
    data['speed_health']=health(source)
    content=OverlayContent();content.set_content(data)
    assert content.speed_alert()['recent_speed']==35 and content.speed_alert()['baseline_speed']==100
    assert not header_status(content)[0]
    assert '출력 속도 저하' in next(r['tooltip'] for r in content.monitor_links() if r['id']=='tip-status')
    content.pin_call(content.call_id(content.rows()[0]))
    content.monitor_action('main-detail')
    assert content.main_expanded and content.graph_pinned
    assert content.speed_alert()['recent_speed']==35
    assert not content.detail_groups() and not content.detailBody.state['accessible']
    assert not any(link['id']=='speed-info' for link in content.monitor_links())
    content.set_content(dict(data,speed_health={'active':False}))
    assert not content.speed_alert()['active']
    content.set_content(data,note='수집 지연')
    assert not content.speed_alert()
    assert not any(r.get('interaction')=='navigate' for r in content.monitor_links())
    assert '수집 지연' in next(r['tooltip'] for r in content.monitor_links() if r['id']=='tip-status')
    assert any(r['id']=='main-detail' for r in content.monitor_links())
    content.deleteLater();app.processEvents()
