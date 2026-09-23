from pathlib import Path
from types import SimpleNamespace
import pytest
from cachemonitor.proxy_update import ProxyUpdate
from cachemonitor.observer_state import read_json
from cachemonitor.version import PROXY_VERSION


class Task:
    def __init__(self,m):self.m=m;self.commands=[];self.stops=0
    def stop(self):
        assert self.m.health() is None  # never kill an active relay
        self.stops+=1
    def start(self,command,**kwargs):
        self.commands.append(command)
        self.m.version='old' if command[0]=='CacheMonitor.exe' else PROXY_VERSION
        if self.m.fail_new and self.m.version==PROXY_VERSION:self.m.version=None
        self.m.instance='replacement'


class Manager:
    def __init__(self,path):
        self.directory=path;self.control_lock=path/'control.lock';self.url='http://localhost:8768'
        self.version='old';self.instance='original';self.connections=0;self.enabled=True;self.fail_new=False
        self.task=Task(self);self.health_state='healthy';self.draining=False;self.drain_ticks=0
    def health(self,**kwargs):
        command=read_json(self.directory/'proxy-control-owned.json')
        if command.get('action')=='drain' and not self.draining:
            self.draining=True;self.drain_ticks=2
            (self.directory/'proxy-control-owned.json').unlink()
        if self.draining:
            self.drain_ticks-=1
            if self.drain_ticks<=0:self.version=None;self.draining=False
        self.health_state='healthy' if self.version else 'refused'
        if not self.version:return None
        return dict(control_id='owned',version=self.version,pid=1,instance=self.instance,
                    active_connections=self.connections,status='ok',draining=self.draining)
    def runtime(self):return dict(pid=1,phase='active')
    def state(self):return dict(enabled=self.enabled,upstream='chatgpt')
    def config(self):return b'unchanged',{'openai_base_url':self.url}
    def supervisor_command(self,upstream):return ['new.exe','--proxy-supervisor']


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr('cachemonitor.proxy_update.process_executable',lambda _: 'CacheMonitor.exe')
    m=Manager(tmp_path);now=[0]
    def sleep(seconds):now[0]+=seconds
    u=ProxyUpdate(m,clock=lambda:now[0],sleep=sleep)
    monkeypatch.setattr(u,'preflight',lambda:None)
    u.publish('queued',source_instance='original',cancel_requested=False)
    return m,u


def test_success_preserves_route_and_waits_for_drain(setup):
    m,u=setup;before=m.config()
    u.run()
    assert read_json(u.path)['phase']=='complete'
    assert m.config()==before and m.task.stops==1
    assert m.task.commands==[['new.exe','--proxy-supervisor']]


def test_failed_new_worker_rolls_back(setup):
    m,u=setup;m.fail_new=True
    u.run()
    assert m.version=='old'
    assert read_json(u.path)['phase']=='failed'
    assert '복구' in read_json(u.path)['message']
    assert m.task.commands[-1]==['CacheMonitor.exe','--proxy-supervisor']


def test_waiting_connections_are_not_disconnected_and_cancel_works(setup):
    m,u=setup;m.connections=2
    original=u.sleep
    def cancel(seconds):
        assert not m.task.commands and not m.task.stops
        assert not (m.directory/'proxy-control-owned.json').exists()
        u.publish('waiting',cancel_requested=True);original(seconds)
    u.sleep=cancel;u.run()
    assert read_json(u.path)['phase']=='cancelled' and m.version=='old'


def test_preflight_failure_does_not_touch_live_proxy(setup,monkeypatch):
    m,u=setup
    def fail():raise RuntimeError('bad runtime')
    monkeypatch.setattr(u,'preflight',fail);u.run()
    assert read_json(u.path)['phase']=='failed'
    assert not m.task.commands and not m.task.stops and m.version=='old'


def test_restarted_updater_recovers_interrupted_switch(setup):
    m,u=setup
    u.publish('switching',rollback_command=['CacheMonitor.exe','--proxy-supervisor'],previous_version='old')
    u.run()
    assert m.version=='old' and m.task.commands[-1][0]=='CacheMonitor.exe'
    assert read_json(u.path)['phase']=='failed'


def test_no_upgrade_needed(setup):
    m,u=setup;m.version=PROXY_VERSION;u.run()
    assert read_json(u.path)['phase']=='complete' and not m.task.commands


def test_disabled_while_waiting_cancels(setup):
    m,u=setup;m.enabled=False;u.run()
    assert read_json(u.path)['phase']=='cancelled' and not m.task.commands


def test_different_instance_aborts_before_drain(setup):
    m,u=setup;m.instance='different';u.run()
    assert read_json(u.path)['phase']=='failed' and not m.task.stops

def test_drain_allows_windows_connection_refusal_to_arrive(setup,monkeypatch):
    m,u=setup;calls=[]
    def health(timeout):
        calls.append(timeout)
        m.health_state='refused' if timeout>=3 else 'unknown'
        return None
    monkeypatch.setattr(m,'health',health)
    u.drain()
    assert calls==[3,3]
