import copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from cachemonitor.proxy_update import ProxyUpdate
from cachemonitor.observer_state import ProcessLock,read_json
from cachemonitor.version import PROXY_VERSION


class Target:
    cache=False
    scope={'home':'isolated','url':'loopback'}
    def __init__(self,m):self.m=m
    def enabled(self):return self.m.enabled
    def capture(self,health):
        return dict(instance=health['instance'],version=health['version'],control_id=health['control_id'],
            command=list(self.m.command),registration=dict(autostart=self.m.autostart),processes=['owned'])
    def replacement(self,source):return ['new.exe',*source['command'][1:]]
    def stopped(self,source):return self.m.version is None and not self.m.port_busy and not self.m.lock_busy
    def exited(self,source):return self.m.version is None
    def ready(self,h,source,command,version,distribution=None):
        return bool(h and h['version']==version and h['instance']!=source['instance'] and not h['draining']
            and self.m.command==command and not self.m.wrong_role)


class Task:
    def __init__(self,m):self.m=m;self.starts=[]
    def start(self,command,autostart=False):
        m=self.m
        assert m.version is None and not m.port_busy and not m.lock_busy
        self.starts.append((list(command),autostart));m.command=list(command)
        m.version=PROXY_VERSION if command[0]=='new.exe' else 'old'
        m.instance='replacement'+str(len(self.starts));m.draining=False
        if m.fail_new and command[0]=='new.exe':m.version=None


class Manager:
    def __init__(self,path,cache=False):
        self.directory=path;self.control_lock=path/'control.lock'
        self.enabled=True;self.version='old';self.instance='original';self.autostart=False
        self.command=['old.exe','--model-proxy','--cache-worker' if cache else '--managed']
        self.fail_new=False;self.wrong_role=False;self.port_busy=False;self.lock_busy=False
        self.control='a'*32;self.draining=False;self.requests=0;self.connections=100
        self.task=Task(self);self.health_state='healthy'
    def health(self,**kwargs):
        path=self.directory/('proxy-control-'+self.control+'.json')
        control=read_json(path)
        if control.get('action')=='drain':
            self.draining=True
            if not self.requests:self.version=None;path.unlink(missing_ok=True)
        self.health_state='healthy' if self.version else 'refused'
        if not self.version:return None
        return dict(version=self.version,pid=123,control_id=self.control,instance=self.instance,
            status='ok',draining=self.draining,active_connections=self.connections)


@pytest.fixture(params=[False,True],ids=['observer','cache-worker'])
def setup(tmp_path,monkeypatch,request):
    m=Manager(tmp_path,request.param);now=[0.]
    target=Target(m);target.cache=request.param
    u=ProxyUpdate(m,clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n),target=target)
    monkeypatch.setattr(u,'preflight',lambda:{'sha256':'verified'})
    u.publish('queued',source_instance='original',cancel_requested=False)
    return m,u


def test_idle_100_connections_drain_without_new_fields_preserves_autostart(setup):
    m,u=setup;u.run()
    assert read_json(u.path)['phase']=='complete'
    assert m.task.starts==[(['new.exe',*m.command[1:]],False)]


def test_sent_response_and_usage_finish_before_replacement(setup):
    m,u=setup;m.requests=1;original=u.sleep
    def finish(n):
        assert not m.task.starts
        m.requests=0;original(n)
    u.sleep=finish
    # Ready checks also sleep, after the response has completed.
    def sleep(n):
        if m.requests:finish(n)
        else:original(n)
    u.sleep=sleep;u.run()
    assert read_json(u.path)['phase']=='complete'


def test_preflight_failure_does_not_drain_or_register(setup,monkeypatch):
    m,u=setup
    def fail():raise RuntimeError('bad runtime')
    monkeypatch.setattr(u,'preflight',fail);u.run()
    assert read_json(u.path)['phase']=='failed'
    assert not m.draining and not m.task.starts and m.version=='old'


def test_unclassified_connection_waits_without_blocking_ingress(setup,monkeypatch):
    m,u=setup;pending=[True];health=m.health;wait=u.sleep
    def observed(**kwargs):
        value=health(**kwargs)
        if value:value['websocket_states']={'unknown':int(pending[0])}
        return value
    def finish(n):
        if pending[0]:
            assert not m.draining and not m.task.starts
            assert not (m.directory/('proxy-control-'+m.control+'.json')).exists()
            pending[0]=False
        wait(n)
    monkeypatch.setattr(m,'health',observed);u.sleep=finish
    u.run()
    assert read_json(u.path)['phase']=='complete' and len(m.task.starts)==1


def test_failed_start_restores_exact_role_command_and_autostart(setup):
    m,u=setup;old=list(m.command);m.fail_new=True;u.run()
    assert m.version=='old' and m.command==old
    assert m.task.starts[-1]==(old,False)
    assert read_json(u.path)['restored'] and read_json(u.path)['phase']=='failed'


@pytest.mark.parametrize('block',['port_busy','lock_busy'])
def test_exit_requires_process_port_and_role_lock(setup,block):
    m,u=setup;setattr(m,block,True);u.run()
    assert read_json(u.path)['phase']=='failed' and not m.task.starts


def test_missing_legacy_control_never_forces_stop(setup):
    m,u=setup;m.control='';u.run()
    assert read_json(u.path)['phase']=='failed'
    assert m.version=='old' and not m.draining and not m.task.starts


def test_restart_adopts_already_running_verified_target(setup):
    m,u=setup;source=u.target.capture(m.health());command=u.target.replacement(source)
    u.publish('verifying',source=source,command=command)
    m.version=PROXY_VERSION;m.instance='already-started';m.command=command
    u.run();assert read_json(u.path)['phase']=='complete' and not m.task.starts


def test_restart_after_old_exit_starts_once(setup):
    m,u=setup;source=u.target.capture(m.health());command=u.target.replacement(source)
    u.publish('starting',source=source,command=command)
    m.version=None;u.run();u.run()
    assert read_json(u.path)['phase']=='complete' and len(m.task.starts)==1


def test_restart_after_verified_rollback_keeps_restored_process(setup):
    m,u=setup;source=u.target.capture(m.health());command=u.target.replacement(source)
    u.publish('rollback',source=source,command=command)
    m.instance='already-restored';u.run()
    assert read_json(u.path)['restored'] and not m.task.starts and m.version=='old'


def test_cancel_or_disable_during_drain_never_resurrects(setup):
    m,u=setup;m.requests=1;original=u.sleep
    def disable(n):m.enabled=False;original(n)
    u.sleep=disable;u.run()
    assert read_json(u.path)['phase']=='cancelled' and not m.task.starts


def test_concurrent_updater_does_not_launch_twice(setup):
    m,u=setup
    with ProcessLock(m.directory/'proxy-update.lock'):u.run()
    assert not m.task.starts
    u.run();u.run()
    assert len(m.task.starts)==1


def test_different_source_instance_is_not_signalled(setup):
    m,u=setup;m.instance='foreign';u.run()
    assert read_json(u.path)['phase']=='failed' and not m.draining


def test_wrong_target_never_completes(setup):
    m,u=setup;m.wrong_role=True
    with pytest.raises(RuntimeError):u.run()
    assert read_json(u.path)['phase']!='complete'


def test_windows_listener_and_exited_instance_are_not_health_timeouts():
    import os,socket,subprocess,sys
    from cachemonitor.proxy_identity import process_identity,same_process,listener_pids,port_free
    if os.name!='nt':pytest.skip('Windows process and listener ownership')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        url='http://127.0.0.1:'+str(listener.getsockname()[1])
        assert listener_pids(url)==[os.getpid()] and not port_free(url)
    assert port_free(url)
    process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'])
    identity=process_identity(process.pid);assert identity and same_process(identity)
    process.wait(timeout=5)
    assert not same_process(identity)
