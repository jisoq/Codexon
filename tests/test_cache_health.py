import copy
from cachemonitor.cache_health import CacheHealth
from cachemonitor.cache_misses import classify


def rows(values,model='model'):
    result=[dict(key=str(i),ts=100+i,input=inp,cached=cached,model=model,effort='high',service_tier='Standard') for i,(inp,cached) in enumerate(values)]
    misses={e['key'] for e in classify(result)['events']}
    for row in result:row['cache_miss']=row['key'] in misses
    return result


def test_growth_is_not_collapse_weighted_rate_and_unknown_policy():
    healthy=rows([(100,90)]*5+[(1000,90)]*2)
    state=CacheHealth().update(healthy)
    assert not state['incident']
    assert abs(state['recent_rate']-450/2300*100)<.001
    assert state['provisional']
    health=CacheHealth();r=rows([(1000,900)]*5+[(1000,300)]*2)
    state=health.update(r)
    assert state['state']=='캐시 저하 의심' and state['incident']['count']==2
    assert state['incident']['segment'][-1] is None


def test_missing_model_policy_boundary_and_recovery_keep_raw_misses():
    r=rows([(1000,900)]*5+[(1000,0)]*2)
    health=CacheHealth();first=health.update(r)
    assert first['state']=='캐시 저하 의심' and classify(r)['count']==2
    r=r+[dict(r[-1],key='missing',ts=108,cached=None,cache_miss=False)]
    state=health.update(r)
    assert state['state']=='판정 보류' and state['partial'] and not state['incident']['resolved']
    for i in (1,2):
        r=r+[dict(r[0],key=f'recover{i}',ts=108+i)]
        state=health.update(r)
    assert state['state']=='회복 확인' and state['incidents']==1
    assert classify(r)['count']==2
    switched=rows([(1000,900)]*5+[(1000,0)]*2)
    switched[-2]['model']=switched[-1]['model']='other'
    assert CacheHealth().update(switched)['incident'] is None
    switched[-2]['model']=switched[-1]['model']='model'
    switched[-2]['cache_policy']=switched[-1]['cache_policy']='explicit-new-policy'
    assert CacheHealth().update(switched)['incident'] is None


def test_backfill_and_correction_replay_global_first_and_incident():
    r=rows([(1000,0)]*5);health=CacheHealth();health.update(r)
    fixed=copy.deepcopy(r)
    for row in fixed:row.update(cached=900,cache_miss=False)
    assert health.update(fixed)['incident'] is None
    before=dict(fixed[0],key='earlier',ts=90)
    assert health.update([before]+fixed)['recent_rate']==90


def test_channel_routing_requires_confirmed_selected_scope_and_avoids_duplicates(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    w=Dashboard([],start_worker=False,live_limits=False,settings=QSettings(str(tmp_path/'route.ini'),QSettings.IniFormat))
    state=dict(visible=True,selected=True,confirmed=True)
    w.overlay=SimpleNamespace(can_present=lambda *_:state['visible'],selected_scope=lambda *_:state['selected'],
                              selection_confirmed=lambda:state['confirmed'],stop=lambda:None)
    sent=[];monkeypatch.setattr(w.tray,'showMessage',lambda *args:sent.append(args))
    event=dict(kind='cache_miss',title='캐시 저하 의심',detail='2회 · 근거',home='h',sid='s',count=2)
    try:
        w.notification_master.setChecked(True)
        w.show_confirmed_events([event]);assert not sent
        state['visible']=False;w.show_confirmed_events([event]);assert len(sent)==1
        w.settings.setValue('notifications/cacheScope','selected');state['selected']=False
        w.show_confirmed_events([event]);assert len(sent)==1
        state['confirmed']=False;w.show_confirmed_events([event]);assert len(sent)==1
        state['selected']=True;w.show_confirmed_events([event]);assert len(sent)==1
        w.settings.setValue('notifications/cacheScope','all')
        w.show_confirmed_events([event]);assert len(sent)==2
        w.notification_master.setChecked(False);w.show_confirmed_events([event]);assert len(sent)==2
    finally:w.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)
