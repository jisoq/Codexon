import json
import time
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from cachemonitor.quota import clean_limits,quota_display
from cachemonitor.index import UsageIndex,sanitized
from cachemonitor.dashboard import Dashboard
from test_core import event,fixture_home


def limits(plan='pro',primary_minutes=10080):
    return {'limit_id':'codex','plan_type':plan,
            'primary':{'used_percent':16.0,'window_minutes':primary_minutes,'resets_at':time.time()+86400},
            'secondary':None,'credits':{'balance':'PRIVATE_BALANCE'}}


def test_window_duration_identifies_weekly_without_inventing_pro_unlimited():
    now=time.time(); raw=limits()
    clean=clean_limits(raw)
    quota={**clean,'observed_at':now}
    assert quota_display(quota,'weekly',now)['text']=='84'
    assert quota_display(quota,'five_hour',now)['text']=='?'
    assert 'credits' not in clean
    raw=limits('plus',300)
    raw['secondary']={'used_percent':30,'window_minutes':10080,'resets_at':now+100}
    quota={**clean_limits(raw),'observed_at':now}
    assert quota_display(quota,'weekly',now)['text']=='70'
    assert quota_display(quota,'five_hour',now)['text']=='84'
    assert quota_display(quota,'weekly',now+101)['text']=='?'
    assert quota_display(None,'weekly',now)['text']=='?'
    raw['limit_id']='model_specific'
    assert clean_limits(raw) is None
    invalid=limits('pro',300)
    invalid['primary']['used_percent']=float('nan')
    assert quota_display({**clean_limits(invalid),'observed_at':now},'five_hour',now)['text']=='?'


def test_only_quota_fields_are_sanitized_and_latest_observation_wins(tmp_path):
    raw=event('event_msg',type='token_count',rate_limits=limits(),info=None,body='PRIVATE_BODY')
    clean=sanitized(raw)
    assert 'PRIVATE' not in json.dumps(clean)
    home,path=fixture_home(tmp_path)
    with path.open('a',encoding='utf8') as output:
        output.write(json.dumps(raw)+'\n')
    idx=UsageIndex([home],tmp_path/'cache.sqlite')
    try:
        snap=idx.poll()
        assert snap['quota']['windows']['weekly']['used_percent']==16
        revision=snap['sessions'][0]['usage_revision']
        changed=limits()
        changed['primary']['used_percent']=17
        with path.open('a',encoding='utf8') as output:
            output.write(json.dumps(event('event_msg',10050,type='token_count',rate_limits=changed,info=None))+'\n')
        after=idx.poll()
        assert after['quota']['windows']['weekly']['used_percent']==17
        assert after['sessions'][0]['usage_revision']==revision
        observed=snap['quota']['observed_at']
        idx.observe_quota(home,clean_limits(limits('plus',300)),observed-1)
        assert idx.quota_by_home[str(home)]['plan_type']=='pro'
        # The second home cannot replace the allowance of the configured first home.
        idx.observe_quota(tmp_path/'other',clean_limits(limits('plus',300)),observed+1)
        assert idx.poll()['quota']['plan_type']=='pro'
    finally: idx.close()
    # Existing indexed files still seed quota without replaying all usage bytes.
    idx=UsageIndex([home],tmp_path/'cache.sqlite')
    try:
        assert idx.poll()['quota']['plan_type']=='pro'
        assert idx.bytes_read==0
    finally: idx.close()


def test_tray_menu_switches_and_persists_actual_quota(tmp_path):
    app=QApplication.instance() or QApplication([])
    settings=QSettings(str(tmp_path/'tray.ini'),QSettings.IniFormat)
    w=Dashboard([],start_worker=False,settings=settings)
    try:
        w.snapshot['quota']={**clean_limits(limits()),'observed_at':time.time()}
        w.refresh_tray()
        assert w.quota_mode=='weekly' and '84%' in w.tray.toolTip()
        assert w._quota_icon_signature[0]=='84'
        w.quota_actions['five_hour'].trigger()
        assert settings.value('tray/quotaMode')=='five_hour'
        assert w.quota_actions['five_hour'].isChecked() and not w.quota_actions['weekly'].isChecked()
        assert w._quota_icon_signature[0]=='?' and '미확인' in w.tray.toolTip()
        w.refresh_tray()
        assert w._quota_icon_signature[0]=='?'
    finally:
        w.quitting=True; w.tick.stop(); w.tray.hide(); w.close()
    restored=Dashboard([],start_worker=False,settings=settings)
    try:
        assert restored.quota_mode=='five_hour'
    finally:
        restored.quitting=True; restored.tick.stop(); restored.tray.hide(); restored.close()
