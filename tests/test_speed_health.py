"""Sustained observed slowdowns, comparison boundaries and recovery."""
from copy import deepcopy

from cachemonitor.speed_health import classify


def session(speeds, sid='s', home='h', start=1000, **changes):
    rows=[]
    for i,speed in enumerate(speeds):
        rows.append(dict(key=f'{sid}-{i}',ts=start+i*100,input=80000,cached=72000,
                         output=2000,reasoning=500,model='model',effort='high',service_tier='Standard',
                         transport='WebSocket',transport_source='response_id',
                         response_status='completed',timing_valid=True,
                         completion_latency_ms=2000/speed*1000,**changes))
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


def test_open_details_rejects_stale_session_incident_and_missing_observation():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_navigation import navigation_target
    app=QApplication.instance() or QApplication([])
    s=session([100]*10+[35]*3)
    data=dict(s, speed_health=health(s))
    content=OverlayContent();content.set_content(data)
    target=navigation_target(data,'speed_alert')
    assert content.open_speed_detail(target)
    assert content.speed_detail and '세션 재생성을 권장합니다.' in content.detailBody.state['accessible']
    content.set_content(dict(data,speed_health={'active':False}))
    assert '출력 속도 저하' not in content.detailBody.state['accessible']
    assert '세션 재생성' not in content.detailBody.state['accessible']
    assert not any(link['id']=='speed-info' for link in content.monitor_links())
    content.set_content(dict(data,id='different'))
    assert not content.speed_detail and not content.open_speed_detail(target)
    content.set_content(data,note='수집 지연')
    assert not content.open_speed_detail(target)
    assert not any(link['id']=='speed-info' for link in content.monitor_links())
    content.set_content(dict(data,speed_health=dict(data['speed_health'],incident_id='new')))
    assert not content.open_speed_detail(target)
    content.deleteLater();app.processEvents()
