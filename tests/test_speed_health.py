"""Sustained observed slowdowns, comparison boundaries and recovery."""
from copy import deepcopy
from types import SimpleNamespace
import weakref

from cachemonitor.speed_health import classify, SpeedHealth, MAX_BASELINE


def session(speeds, sid='s', home='h', start=1000, **changes):
    rows=[]
    for i,speed in enumerate(speeds):
        rows.append(dict(key=f'{sid}-{i}',ts=start+i*100,input=80000,cached=72000,
                         output=2000,reasoning=500,model='model',effort='high',service_tier='Standard',
                         transport='WebSocket',transport_source='response_id',
                         response_status='completed',timing_valid=True,
                         completion_latency_ms=2000/speed*1000) | changes)
    return dict(home=home,id=sid,history=rows)


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


def revision_engine(sessions, previous=None):
    previous = previous or {}
    return SimpleNamespace(sessions={(source['home'], source['id']):
        dict(prepared=source, revision=previous.get((source['home'], source['id']), 0) + 1)
        for source in sessions})


def test_incremental_appends_corrections_and_removals_match_complete_replay():
    tracker = SpeedHealth()
    current = session([100] * 10)
    engine = revision_engine([current])
    assert tracker.collect(engine) == classify([current])
    for speeds in ([100] * 10 + [35], [100] * 10 + [35] * 3,
                   [100] * 10 + [35] * 3 + [100, 100]):
        current = session(speeds)
        engine = revision_engine([current], {key: state['revision'] for key, state in engine.sessions.items()})
        assert tracker.collect(engine) == classify([current])
        assert tracker.metrics['replayed_scopes'] == 0
    current['history'][2]['completion_latency_ms'] = 20000
    engine = revision_engine([current])
    assert tracker.collect(engine) == classify([current])
    current['history'].pop(1)
    engine = revision_engine([current], {key: state['revision'] for key, state in engine.sessions.items()})
    assert tracker.collect(engine) == classify([current])
    empty = revision_engine([])
    assert tracker.collect(empty) == {}
    assert tracker.metrics['retained_rows'] == tracker.metrics['baseline_rows'] == tracker.metrics['live_states'] == 0


def test_changed_peer_scope_does_not_replay_unrelated_conditions():
    reference = session([100] * 10, 'reference')
    peer = session([35] * 3, 'peer', start=3000)
    unrelated = session([90] * 500, 'other', model='other')
    sources = [reference, peer, unrelated]
    tracker = SpeedHealth()
    engine = revision_engine(sources)
    assert tracker.collect(engine) == classify(sources)
    old_revision = engine.sessions[('h', 'other')]['revision']
    reference['history'][5]['completion_latency_ms'] = 100000
    engine.sessions[('h', 'reference')] = dict(prepared=reference, revision=2)
    assert tracker.collect(engine) == classify(sources)
    assert engine.sessions[('h', 'other')]['revision'] == old_revision
    assert tracker.metrics['scanned_rows'] == 10
    assert tracker.metrics['processed_rows'] == 13
    assert tracker.metrics['replayed_scopes'] == 1
    assert tracker.metrics['baseline_rows'] <= MAX_BASELINE * 4 * 2
    assert tracker.metrics['live_states'] <= len(sources)
    assert tracker.collect(engine) == classify(sources)
    assert tracker.metrics['processed_rows'] == tracker.metrics['scanned_rows'] == 0


def test_incremental_replay_preserves_condition_resets_late_rows_and_dedup_owners():
    source = session([100] * 10 + [35] * 3, 'a')
    source['history'][11]['model'] = 'other'
    duplicate = session([], 'b')
    duplicate['history'] = [deepcopy(source['history'][0])]
    peer = session([35] * 3, 'peer', start=4000)
    sources = [source, duplicate, peer]
    tracker = SpeedHealth()
    engine = revision_engine(sources)
    assert tracker.collect(engine) == classify(sources)
    # The same response moves to the earlier owner and changes its condition.
    duplicate['history'][0].update(ts=999, model='other')
    engine.sessions[('h', 'b')] = dict(prepared=duplicate, revision=2)
    assert tracker.collect(engine) == classify(sources)
    # A newly observed historical response must precede already consumed peers.
    late = session([100], 'late', start=950)
    sources.append(late)
    engine.sessions[('h', 'late')] = dict(prepared=late, revision=1)
    assert tracker.collect(engine) == classify(sources)
    assert tracker.metrics['replayed_scopes'] == 1
    # Removing a winning duplicate restores the surviving response owner.
    sources.remove(duplicate)
    engine.sessions.pop(('h', 'b'))
    assert tracker.collect(engine) == classify(sources)
    source['history'][-1]['ts'] += 8 * 86400
    engine.sessions[('h', 'a')] = dict(prepared=source, revision=2)
    assert tracker.collect(engine) == classify(sources)


def test_capacity_fallback_releases_incremental_state_and_keeps_exact_results(monkeypatch):
    monkeypatch.setattr('cachemonitor.speed_health.MAX_TRACKED_CALLS', 12)
    tracker = SpeedHealth()
    source = session([100] * 10)
    assert tracker.collect(revision_engine([source])) == classify([source])
    source = session([100] * 10 + [35] * 3)
    assert tracker.collect(revision_engine([source], {('h', 's'): 1})) == classify([source])
    assert tracker.metrics['capacity_fallback'] and tracker.metrics['retained_rows'] == 0
    assert not tracker.calls and not tracker.replays
    source = session([100] * 10)
    assert tracker.collect(revision_engine([source], {('h', 's'): 2})) == classify([source])
    assert tracker.metrics['retained_rows'] == 10


def test_invalid_duplicate_owner_changes_and_metadata_revisions_preserve_peers():
    reference = session([100] * 8, 'reference')
    owner = session([], 'owner')
    owner['history'] = [dict(row, ts=900 + i, timing_valid=False)
                        for i, row in enumerate(reference['history'][:3])]
    peer = session([35] * 3, 'peer', start=3000)
    sources = [reference, owner, peer]
    engine = revision_engine(sources)
    tracker = SpeedHealth()
    assert tracker.collect(engine) == classify(sources)
    assert not tracker.results[('h', 'peer')]['active']
    for row in owner['history']:
        row['timing_valid'] = True
    engine.sessions[('h', 'owner')] = dict(prepared=owner, revision=2)
    assert tracker.collect(engine) == classify(sources)
    assert tracker.results[('h', 'peer')]['active']
    assert tracker.metrics['replayed_scopes'] == 1
    # A pricing/settings revision leaves observed speed inputs unchanged.
    reference['history'][0].update(cost=999, configured_model='new-setting')
    engine.sessions[('h', 'reference')] = dict(prepared=reference, revision=2)
    assert tracker.collect(engine) == classify(sources)
    assert tracker.metrics['processed_rows'] == 0
    # Entering the internal-review population removes that session from this
    # engine population, restoring the remaining canonical response owners.
    engine.sessions.pop(('h', 'owner'))
    sources.remove(owner)
    assert tracker.collect(engine) == classify(sources)


def test_metadata_only_replacement_releases_obsolete_observed_rows():
    class ObservedRow(dict):
        pass

    source = session([100] * 10)
    source['history'] = [ObservedRow(row) for row in source['history']]
    obsolete = weakref.ref(source['history'][0])
    tracker = SpeedHealth()
    tracker.collect(revision_engine([source]))
    source['history'] = [dict(row, cost=123) for row in source['history']]
    assert tracker.collect(revision_engine([source], {('h', 's'): 1})) == classify([source])
    assert tracker.metrics['processed_rows'] == 0
    assert obsolete() is None


def test_peer_scope_capacity_falls_back_without_losing_observed_results(monkeypatch):
    monkeypatch.setattr('cachemonitor.speed_health.MAX_TRACKED_SCOPES', 2)
    first = session([100] * 10 + [35] * 3, 'first', model='first-model')
    second = session([100] * 10, 'second', model='second-model')
    tracker = SpeedHealth()
    assert tracker.collect(revision_engine([first, second])) == classify([first, second])
    # Replacing a complete condition stays inside the scope budget.
    replacement = session([100] * 10, 'second', model='replacement-model')
    assert tracker.collect(revision_engine([first, replacement], {('h', 'first'): 1, ('h', 'second'): 1})) == classify([first, replacement])
    assert not tracker.metrics['capacity_fallback']
    third = session([100] * 10, 'third', model='third-model')
    sources = [first, replacement, third]
    engine = revision_engine(sources, {('h', 'first'): 1, ('h', 'second'): 2})
    assert tracker.collect(engine) == classify(sources)
    assert tracker.results[('h', 'first')]['active']
    assert tracker.metrics['capacity_fallback'] and tracker.metrics['retained_rows'] == 0
    assert not tracker.calls and not tracker.replays and not tracker.scope_counts
    engine.sessions=dict(reversed(list(engine.sessions.items())))
    assert tracker.collect(engine)==classify(sources)
    assert tracker.metrics['processed_rows']==tracker.metrics['scanned_rows']==0
    engine.sessions.pop(('h', 'third'))
    assert tracker.collect(engine) == classify([first, replacement])
    assert not tracker.metrics['capacity_fallback']


def test_detail_includes_current_health_without_another_entry():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    app=QApplication.instance() or QApplication([])
    source=session([100]*10+[35]*3)
    data=dict(source,speed_health=health(source))
    content=OverlayContent();content.set_content(data);content.set_layout(detail=True)
    assert '세션 재생성을 권장합니다.' in content.detailBody.state['accessible']
    assert not any(link['id']=='speed-info' for link in content.monitor_links())
    content.set_content(dict(data,speed_health={'active':False}))
    assert '세션 재생성' not in content.detailBody.state['accessible']
    content.set_content(data,note='수집 지연')
    assert '세션 재생성' not in content.detailBody.state['accessible']
    assert [r['id'] for r in content.monitor_links() if r.get('target')]==['session','collection']
    content.deleteLater();app.processEvents()
