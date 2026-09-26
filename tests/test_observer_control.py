import json
import tomllib
import time
import pytest

from cachemonitor.observer_control import ObserverManager, URL
from cachemonitor.model_evidence import home_key
from cachemonitor.version import PROXY_VERSION


def manager(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    (home/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt'}))
    control=ObserverManager(home,tmp_path/'app')
    registry={'value':'previous startup command'}
    def startup(value=...):
        if value is ...:return registry['value']
        registry['value']=value
    monkeypatch.setattr('cachemonitor.observer_control.startup_value',startup)
    monkeypatch.setattr(control,'start',lambda upstream:None)
    monkeypatch.setattr(control,'health',lambda **_:{'responses':0,'service':'cachemonitor-model-observer','status':'ok','version':PROXY_VERSION,'instance':'test'})
    class Task:
        calls=[]
        def configure(self,command,autostart):self.calls.append(('configure',autostart))
        def remove(self):self.calls.append(('remove',False))
    control.task=Task()
    monkeypatch.setattr(control,'cleanup_legacy_check',lambda:None)
    return control,registry


def validated(control):
    control.write_state({'home':home_key(control.home),'proof_at':time.time(),'proof_instance':'test','enabled':False})


def test_enable_disable_preserves_comments_unrelated_edits_and_startup(tmp_path,monkeypatch):
    control,registry=manager(tmp_path,monkeypatch)
    before='# Personal settings\nmodel = "gpt-6-astra"\n\n[features]\nalpha = true\n'
    control.config_path.write_text(before)
    validated(control)
    result=control.enable()
    assert result['enabled'] and result['configured'] and result['running']
    assert ('configure',False) in control.task.calls
    changed=control.config_path.read_text()
    assert '# Personal settings' in changed
    assert tomllib.loads(changed)['features']['alpha'] is True
    control.config_path.write_text(changed.replace('alpha = true','alpha = false'))
    # Repeated enable does not replace the original restoration point with the proxy URL.
    control.enable();control.disable()
    restored=tomllib.loads(control.config_path.read_text())
    assert 'openai_base_url' not in restored
    assert restored['features']['alpha'] is False
    assert registry['value']=='previous startup command'
    assert any(p.read_text()==before for p in (control.directory/'backups').glob('*.toml'))


def test_failed_start_never_changes_config_or_startup(tmp_path,monkeypatch):
    control,registry=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "m"\n')
    monkeypatch.setattr(control,'start',lambda _: (_ for _ in ()).throw(RuntimeError('failed')))
    with pytest.raises(RuntimeError):control.prepare()
    assert control.config_path.read_text()=='model = "m"\n'
    assert registry['value']=='previous startup command'
    assert not control.state_path.exists()


def test_disable_preserves_manual_routing_change(tmp_path,monkeypatch):
    control,_=manager(tmp_path,monkeypatch)
    validated(control)
    control.enable()
    control.config_path.write_text('openai_base_url = "https://custom.example/v1"\n')
    control.disable()
    assert control.config()[1]['openai_base_url']=='https://custom.example/v1'


def test_custom_provider_and_missing_rollback_are_not_overwritten(tmp_path,monkeypatch):
    control,_=manager(tmp_path,monkeypatch)
    for data in ('model_provider = "custom"\n',f'openai_base_url = "{URL}"\n'):
        control.config_path.write_text(data)
        with pytest.raises((ValueError,RuntimeError)):control.prepare()
        assert control.config_path.read_text()==data


def test_stale_enabled_flag_and_background_poll_never_reroute(tmp_path,monkeypatch):
    control,_=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "safe"\n')
    control.write_state({'home':home_key(control.home),'enabled':True,'upstream':'chatgpt'})
    before=control.config_path.read_bytes()
    assert not control.ensure()['enabled']
    assert control.config_path.read_bytes()==before


@pytest.mark.parametrize('instance,age',[('wrong',0),('test',301)])
def test_apply_requires_recent_test_for_same_service(tmp_path,monkeypatch,instance,age):
    control,registry=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "safe"\n')
    control.write_state({'home':home_key(control.home),'proof_at':time.time()-age,'proof_instance':instance})
    with pytest.raises(RuntimeError):control.enable()
    assert control.config_path.read_text()=='model = "safe"\n'
    assert registry['value']=='previous startup command'
def test_cache_worker_reuses_single_configured_route_without_model_probe(tmp_path,monkeypatch):
    from cachemonitor.cache_worker_control import CacheWorkerManager
    from cachemonitor.version import PROXY_VERSION
    from types import SimpleNamespace
    home=tmp_path/'home';home.mkdir();(home/'config.toml').write_text('openai_base_url="http://127.0.0.1:18771"\n')
    index=tmp_path/'analysis'/'index.sqlite';index.parent.mkdir()
    monkeypatch.chdir(tmp_path)
    manager=CacheWorkerManager(home,'analysis/index.sqlite','model-evidence.sqlite');calls=[]
    assert manager.index==index.resolve() and manager.evidence==(tmp_path/'model-evidence.sqlite').resolve()
    assert manager.directory==tmp_path.resolve() and manager.control_lock.is_absolute()
    manager.health_state='ok'
    monkeypatch.setattr(manager,'health',lambda **kw:dict(cache_management=True,version=PROXY_VERSION,active_connections=3))
    manager.task=SimpleNamespace(inspect=lambda:dict(registered=True,autostart=True),
        configure=lambda command,**kw:calls.append((command,kw)),
        start=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Second proxy started')))
    assert manager.ensure()['configured'] and manager.url=='http://127.0.0.1:18771'
    assert manager.turn_on()['shared_cache_worker']
    assert '--cache-worker' in calls[0][0] and '--cache-observe-only' not in calls[0][0]
    assert calls[0][0][-1]=='18771'
    assert calls[0][0][calls[0][0].index('--upstream')+1]=='openai'
    (home/'auth.json').write_text('{"auth_mode":"chatgpt"}')
    assert manager.command()[manager.command().index('--upstream')+1]=='chatgpt'
    assert manager.command('openai')[manager.command('openai').index('--upstream')+1]=='openai'
    assert not manager.turn_off()['configured']
    assert 'openai_base_url' not in (home/'config.toml').read_text()
    restored=CacheWorkerManager(home,index,tmp_path/'model-evidence.sqlite')
    assert restored.url=='http://127.0.0.1:18771'


def test_cache_worker_recovery_removes_task_and_drains_without_forced_stop(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import observer_control
    from cachemonitor.cache_worker_control import CacheWorkerManager
    home=tmp_path/'custom';home.mkdir()
    (home/'config.toml').write_text('openai_base_url="http://127.0.0.1:18772"\nmodel="preserve"\n')
    manager=CacheWorkerManager(home,tmp_path/'index.sqlite',tmp_path/'evidence.sqlite')
    manager.health_state='ok'
    identity='a'*32;removed=[]
    monkeypatch.setattr(observer_control,'startup_value',lambda *a:None)
    monkeypatch.setattr(manager,'health',lambda **kw:dict(cache_management=True,control_id=identity,active_connections=1))
    for field in ('task','legacy_task'):
        setattr(manager,field,SimpleNamespace(remove=lambda f=field:removed.append(f),inspect=lambda:dict(registered=False)))
    result=manager.recover_direct()
    assert not result['configured'] and 'task' in removed
    assert json.loads((tmp_path/('proxy-control-'+identity+'.json')).read_text())==dict(action='drain',id=identity)
    assert (home/'config.toml').read_text()=='model="preserve"\n'
