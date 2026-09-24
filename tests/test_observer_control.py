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
    control.check_task=Task()
    monkeypatch.setattr(control,'configure_check',lambda:None)
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
    assert ('configure',True) in control.task.calls
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
