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


@pytest.mark.parametrize('scenario',['first_request','existing','delayed','missing','wrong_model'])
def test_connection_waits_for_first_observation_without_creating_the_database(tmp_path,monkeypatch,scenario):
    import threading
    from cachemonitor.model_evidence import EvidenceStore
    control,_=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "test-model"\n')
    before=control.config_path.read_bytes();auth=control.home.joinpath('auth.json').read_bytes()
    monkeypatch.setattr('cachemonitor.quota_live.locate_codex',lambda:'isolated-test-codex')
    workers=[];calls=[]
    if scenario=='existing':
        store=EvidenceStore(control.evidence)
        store.write(control.home,'old',1,'HTTP/SSE','old-response','wrong','wrong','completed');store.close()
    def record():
        store=EvidenceStore(control.evidence)
        try:store.write(control.home,'new',2,'HTTP/SSE','new-response','test-model',
                        'wrong' if scenario=='wrong_model' else 'test-model','completed')
        finally:store.close()
    class Probe:
        returncode=0
        def __init__(self,command,**kwargs):
            calls.append(command)
            assert control.evidence.exists()==(scenario=='existing')
        def communicate(self,timeout):
            if scenario=='delayed':
                worker=threading.Timer(.15,record);worker.start();workers.append(worker)
            elif scenario!='missing':record()
            return b'MODEL_OBSERVER_READY',b''
    monkeypatch.setattr('cachemonitor.observer_control.subprocess.Popen',Probe)
    if scenario=='missing':
        clock=iter(range(100))
        monkeypatch.setattr('cachemonitor.observer_control.time.monotonic',lambda:next(clock))
        monkeypatch.setattr('cachemonitor.observer_control.time.sleep',lambda _:None)
    try:
        if scenario in ('missing','wrong_model'):
            with pytest.raises(RuntimeError,match='연결 시험이 통과하지 못했습니다'):control.test_connection()
            assert not control.state().get('proof_at')
        else:
            result=control.test_connection()
            assert result['validated'] and not result['configured']
        assert len(calls)==1
        assert control.config_path.read_bytes()==before and control.home.joinpath('auth.json').read_bytes()==auth
        if scenario=='missing':assert not control.evidence.exists()
    finally:
        for worker in workers:worker.join(2)


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
