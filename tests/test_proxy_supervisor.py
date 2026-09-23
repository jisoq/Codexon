from pathlib import Path
import pytest
from cachemonitor.observer_control import ObserverManager, URL
from cachemonitor.proxy_supervisor import Protection, Supervisor
from cachemonitor.model_evidence import home_key


class Task:
    def __init__(self):self.calls=[]
    def remove(self):self.calls.append('remove')
    def configure(self,*args,**kwargs):self.calls.append(('configure',kwargs))


def test_runtime_publication_failure_keeps_supervising_and_recovers(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    m.health_state='healthy'
    monkeypatch.setattr(m,'health',lambda **kwargs:{'pid':123})
    from cachemonitor import proxy_supervisor as module
    original=module.atomic_write
    def locked(*args):raise PermissionError('runtime locked')
    monkeypatch.setattr(module,'atomic_write',locked)
    s=Supervisor(m,'openai')
    assert s.tick() and s.tick() and s.publish_failures==2
    assert m.state()['enabled'] and m.config()[1]['openai_base_url']==URL
    monkeypatch.setattr(module,'atomic_write',original)
    assert s.tick() and s.publish_failures==0
    import json
    assert json.loads(m.runtime_path.read_text())['publication_failures']==2


def test_atomic_write_retries_windows_sharing_failure(tmp_path,monkeypatch):
    from cachemonitor import observer_control as module
    target=tmp_path/'state.json';target.write_bytes(b'old')
    original=module.os.replace;calls=[]
    def replace(source,destination):
        calls.append(1)
        if len(calls)<3:
            error=PermissionError('locked');error.winerror=5;raise error
        return original(source,destination)
    monkeypatch.setattr(module.os,'replace',replace)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    module.atomic_write(target,b'new')
    assert target.read_bytes()==b'new' and len(calls)==3
    assert not list(tmp_path.glob('*.tmp'))
    def denied(*args):
        error=PermissionError('denied');error.winerror=5;raise error
    monkeypatch.setattr(module.os,'replace',denied)
    with pytest.raises(PermissionError):module.atomic_write(target,b'not committed')
    assert target.read_bytes()==b'new' and not list(tmp_path.glob('*.tmp'))


def manager(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    m=ObserverManager(home,tmp_path/'data')
    m.task=Task()
    m.legacy_task=Task()
    m.check_task=Task()
    monkeypatch.setattr('cachemonitor.observer_control.startup_value',lambda *args:None)
    m.config_path.write_text(f'# preserve\nopenai_base_url="{URL}"\nmodel="m"\n')
    m.write_state({'home':home_key(home),'enabled':True,'phase':'active','previous_url':'https://api.openai.com/v1'})
    return m


def test_protection_never_trips_for_remote_errors_cancellation_or_unknown_probes():
    now=[0];p=Protection(lambda:now[0])
    for probe in ('healthy','unknown'):
        for i in range(10):
            now[0]+=5
            assert p.observe({'relay_errors':1000,'client_disconnects':1000,'storage_errors':1000,
                              'storage_failure_streak':0},probe) is None
    assert p.observe(None,'unknown',exited=True)


@pytest.mark.parametrize('probe,health',[('refused',None),('identity_mismatch',None),
    ('healthy',{'storage_failure_streak':3}),('healthy',{'internal_failure_streak':3})])
def test_protection_requires_three_spaced_confirmations(probe,health):
    now=[0];p=Protection(lambda:now[0])
    assert p.observe(health,probe) is None
    now[0]=1;assert p.observe(health,probe) is None
    now[0]=5;assert p.observe(health,probe) is None
    now[0]=10;assert p.observe(health,probe)


def test_worker_exit_restores_direct_route_without_gui_and_latches_off(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    def health(**kwargs):m.health_state='refused';return None
    monkeypatch.setattr(m,'health',health)
    s=Supervisor(m,'openai')
    s.child=type('Child',(),{'pid':123,'poll':lambda self:1})()
    assert s.tick()
    assert m.config()[1]['openai_base_url']=='https://api.openai.com/v1'
    assert '# preserve' in m.config_path.read_text()
    assert m.state()['phase']=='faulted' and not m.state()['enabled']
    assert m.state()['incident']['reason']
    assert m.task.calls==['remove']
    assert not s.tick()


def test_restoration_failure_is_visible_and_never_claims_off(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    monkeypatch.setattr(m,'set_url',lambda _:(_ for _ in ()).throw(OSError('read-only config')))
    monkeypatch.setattr(m,'health',lambda **kw:None)
    s=Supervisor(m,'openai')
    assert not s.trip('failed')
    assert m.state()['phase']=='recovery_failed' and m.state()['enabled']
    assert m.config()[1]['openai_base_url']==URL
    assert m.status()['phase']=='recovery_failed'


def test_manual_routing_change_is_preserved_and_autostart_removed(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    m.config_path.write_text('openai_base_url="https://custom.example/v1"\n')
    monkeypatch.setattr(m,'health',lambda **kw:{'pid':123})
    s=Supervisor(m,'openai')
    assert s.tick()
    assert m.config()[1]['openai_base_url']=='https://custom.example/v1'
    assert m.task.calls==['remove'] and not m.state()['enabled']


def test_turn_on_cancellation_after_validation_cannot_apply_route(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    m.config_path.write_text('model="keep"\n')
    def test():m.cancelled.set()
    monkeypatch.setattr(m,'test_connection',test)
    monkeypatch.setattr(m,'health',lambda **kw:None)
    with pytest.raises(RuntimeError,match='취소'):m.turn_on()
    assert m.config()[1]=={'model':'keep'}
    assert not m.state()['enabled']


def test_off_registration_cannot_spawn_worker_at_next_login(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch)
    m.write_state({'home':home_key(m.home),'enabled':False,'phase':'off'})
    monkeypatch.setattr(m,'health',lambda **kw:None)
    m.health_state='refused'
    monkeypatch.setattr('cachemonitor.proxy_supervisor.subprocess.Popen',
                        lambda *a,**kw:pytest.fail('OFF must not start a worker'))
    s=Supervisor(m,'openai');s.run()
    assert s.phase=='stopped' and s.child is None


def test_failed_turn_on_reports_rollback_failure_without_false_off(tmp_path,monkeypatch):
    m=manager(tmp_path,monkeypatch);m.config_path.write_text('model="keep"\n')
    def fail_after_apply():
        m.set_url(m.url)
        raise RuntimeError('verification failed')
    monkeypatch.setattr(m,'test_connection',fail_after_apply)
    monkeypatch.setattr(m,'recover_direct',lambda:(_ for _ in ()).throw(OSError('config locked')))
    monkeypatch.setattr(m,'health',lambda **kw:None)
    with pytest.raises(RuntimeError,match='설정 복구 실패'):m.turn_on()
    assert m.status()['configured'] and m.status()['phase']=='recovery_failed'
    assert m.state()['incident']['recovery_error']=='config locked'
