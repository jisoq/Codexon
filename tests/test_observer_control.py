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


@pytest.mark.parametrize('fresh', [False, True])
def test_restart_requires_fresh_runtime_and_matching_live_health(tmp_path, monkeypatch, fresh):
    from types import SimpleNamespace
    control,_=manager(tmp_path,monkeypatch)
    control.config_path.write_text(f'openai_base_url="{control.url}"\n')
    control.write_state(dict(home=home_key(control.home),enabled=True,upstream='chatgpt'))
    runtime=dict(phase='active',instance='old',pid=123)
    probes=[];started=[];removed=[]
    def health(**kwargs):
        probes.append(True)
        control.health_state='refused'
        if started and fresh and len(probes)>=4:
            runtime.update(instance='new',pid=456)
            return dict(status='ok',version=PROXY_VERSION,lifecycle='managed',control_id='new',pid=456)
        return None
    control.task=SimpleNamespace(start=lambda *a,**kw:started.append(True))
    control.legacy_task=SimpleNamespace(remove=lambda:removed.append(True))
    monkeypatch.setattr(control,'runtime',lambda:runtime.copy())
    monkeypatch.setattr(control,'health',health)
    monkeypatch.setattr(control,'status',lambda:dict(running=fresh))
    monkeypatch.setattr('cachemonitor.observer_control.time.sleep',lambda _:None)
    if fresh:
        assert control.attach_supervisor()['running']
        assert removed==[True]
    else:
        with pytest.raises(RuntimeError):control.attach_supervisor()
        assert not removed
    assert started==[True] and len(probes)>=4


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


def test_local_readiness_does_not_generate_or_read_observations(tmp_path,monkeypatch):
    control,_=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "test-model"\n')
    before=control.config_path.read_bytes();auth=control.home.joinpath('auth.json').read_bytes()
    # An unavailable CLI and unrelated/corrupt evidence cannot block local readiness.
    control.directory.mkdir(parents=True)
    control.evidence.write_bytes(b'unrelated evidence must remain untouched')
    monkeypatch.setattr('subprocess.Popen',lambda *a,**kw:pytest.fail('No CLI or model invocation'))
    monkeypatch.setattr('sqlite3.connect',lambda *a,**kw:pytest.fail('No observation lookup'))
    result=control.check_ready()
    assert result['validated'] and not result['configured']
    assert control.config_path.read_bytes()==before and control.home.joinpath('auth.json').read_bytes()==auth
    assert control.evidence.read_bytes()==b'unrelated evidence must remain untouched'
    assert 'proof_model' not in control.state()


@pytest.mark.parametrize('failure',['unreachable','degraded','draining','observation_disabled',
                                    'incompatible','missing_instance','cancelled','config_changed'])
def test_local_readiness_failure_never_applies_route(tmp_path,monkeypatch,failure):
    control,_=manager(tmp_path,monkeypatch)
    control.config_path.write_text('model = "keep"\n')
    health={'status':'ok','instance':'test','version':PROXY_VERSION}
    if failure=='unreachable':health=None
    elif failure=='degraded':health['status']='degraded'
    elif failure=='draining':health['draining']=True
    elif failure=='observation_disabled':health['observation_enabled']=False
    elif failure=='incompatible':health['version']='unknown'
    elif failure=='missing_instance':health.pop('instance')
    original=control.prepare
    def prepare():
        original()
        if failure=='cancelled':control.cancelled.set()
        if failure=='config_changed':control.config_path.write_text('model = "changed"\n')
        monkeypatch.setattr(control,'health',lambda **_:health)
    monkeypatch.setattr(control,'prepare',prepare)
    with pytest.raises(RuntimeError,match='프록시 준비'):
        control.check_ready()
    assert not control.state().get('proof_at')
    assert 'openai_base_url' not in control.config()[1]
    assert control.config()[1]['model']==('changed' if failure=='config_changed' else 'keep')


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


def _interrupted_atomic_write(path,control,publisher):
    import os
    from pathlib import Path
    from cachemonitor.observer_control import atomic_write
    original=Path.write_bytes
    def write(file,data):
        if '.codexon-atomic-' not in file.name:return original(file,data)
        with file.open('wb') as target:
            written=target.write(data[:len(data)//2]);target.flush();os.fsync(target.fileno())
            control.send((str(file),written));control.recv()
            target.write(data[len(data)//2:])
    Path.write_bytes=write
    if publisher=='collection_endpoint':
        from cachemonitor.collection_transport import LiveServer
        server=LiveServer(path,lambda request:({},b''))
        server.close()
    else:atomic_write(path,b'new configuration'*8192)


@pytest.mark.integration
@pytest.mark.parametrize('publisher',['configuration','collection_endpoint'])
def test_atomic_write_recovers_only_dead_owned_partial_files(tmp_path,publisher):
    import multiprocessing as mp
    from pathlib import Path
    from cachemonitor.observer_control import atomic_write
    path=tmp_path/'config.toml';path.write_bytes(b'original configuration')
    unknown=tmp_path/('config.toml.'+'a'*32+'.tmp');unknown.write_bytes(b'unknown old owner')
    context=mp.get_context('spawn')
    for _ in range(3):
        parent,pipe=context.Pipe()
        child=context.Process(target=_interrupted_atomic_write,args=(str(path),pipe,publisher))
        child.start();pipe.close()
        try:
            assert parent.poll(15),child.exitcode
            temporary,written=parent.recv();temporary=Path(temporary)
            assert written>0 and temporary.stat().st_size==written
            assert len(list(tmp_path.glob('*.codexon-atomic-*.tmp')))==1
            # A concurrent writer must not delete the paused writer's file.
            atomic_write(tmp_path/'other.json',b'complete')
            assert temporary.exists()
            child.kill();child.join(10)
            assert not child.is_alive() and child.exitcode!=0
            assert path.read_bytes()==b'original configuration'
        finally:
            if child.is_alive():child.kill()
            child.join(10);parent.close()
    resumed=context.Process(target=atomic_write,args=(str(path),b'completed configuration'))
    resumed.start();resumed.join(15)
    try:
        assert not resumed.is_alive() and resumed.exitcode==0
        assert path.read_bytes()==b'completed configuration'
        assert not list(tmp_path.glob('*.codexon-atomic-*.tmp'))
        assert unknown.read_bytes()==b'unknown old owner'
    finally:
        if resumed.is_alive():resumed.kill();resumed.join(10)
