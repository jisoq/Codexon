"""Legacy retirement without model calls, lost usage or revived permissions."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import pytest

from cachemonitor.retired_cache import import_usage,remove_hooks,retire
from cachemonitor.observer_control import ObserverManager,atomic_write
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.version import PROXY_VERSION


def legacy_db(path):
    with closing(sqlite3.connect(path)) as db,db:
        db.executescript('''CREATE TABLE cache_preferences(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE cache_operating_grants(stopped TEXT);
            INSERT INTO cache_operating_grants VALUES(NULL);
            CREATE TABLE cache_jobs(id TEXT PRIMARY KEY,home TEXT,sid TEXT,state TEXT,started REAL,ended REAL,
                response_id TEXT,model TEXT,effort TEXT,tier TEXT,usage TEXT,purpose TEXT);''')
        db.execute('INSERT INTO cache_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('job','home','thread','completed',1,2,
            'response','gpt-6.1-sol','xhigh','default',json.dumps(dict(input=100,cached=80,written=0,output=5,reasoning=0)), 'maintenance'))


def test_historical_usage_import_is_durable_idempotent_and_preserves_unknown(tmp_path):
    old=tmp_path/'cache-control.sqlite';legacy_db(old);index=tmp_path/'index.sqlite'
    with closing(sqlite3.connect(old)) as db,db:
        db.execute("INSERT INTO cache_jobs VALUES('unknown','home','thread','sent',3,NULL,NULL,'gpt-6.1-sol','xhigh','default',NULL,'maintenance')")
    assert import_usage(old,index)==2;assert import_usage(old,index)==2
    with closing(sqlite3.connect(index)) as db,db:
        records={rid:json.loads(data) for rid,data in db.execute('SELECT response_id,data FROM usage_archive')}
        assert len(records)==2 and records['response']['cached']==80
        assert records['maintenance:unknown']['input'] is None
        assert records['maintenance:unknown']['state']=='unknown'
        from cachemonitor.usage_archive import sessions
        view=sessions(db,10,{'home'})[0]
        assert {r['key'] for r in view['history']}==set(records)
        assert next(r for r in view['history'] if r['key']=='maintenance:unknown')['input'] is None


def test_conflicting_usage_stops_retirement_without_overwrite(tmp_path):
    old=tmp_path/'cache-control.sqlite';legacy_db(old);index=tmp_path/'index.sqlite';import_usage(old,index)
    with closing(sqlite3.connect(old)) as db,db:db.execute("UPDATE cache_jobs SET usage='{}'")
    with pytest.raises(RuntimeError,match='Conflicting'):import_usage(old,index)
    assert old.exists()


def test_receipt_free_release_requires_clean_exit_and_durable_settlement(tmp_path):
    from cachemonitor.retired_cache import verify_legacy_settlement
    old=tmp_path/'cache-control.sqlite';legacy_db(old)
    verify_legacy_settlement(old,0)
    with pytest.raises(RuntimeError,match='did not exit cleanly'):verify_legacy_settlement(old,1)
    with closing(sqlite3.connect(old)) as db,db:db.execute("UPDATE cache_jobs SET state='sent'")
    with pytest.raises(RuntimeError,match='settlement incomplete'):verify_legacy_settlement(old,0)
    with closing(sqlite3.connect(old)) as db,db:db.execute("UPDATE cache_jobs SET state='unknown',usage=NULL")
    verify_legacy_settlement(old,0)
    assert old.exists()


def test_owned_hooks_removed_with_unrelated_entries_preserved(tmp_path):
    path=tmp_path/'hooks.json';other=dict(description='my hook',hooks=[dict(command='custom')])
    path.write_text(json.dumps({'hooks':{'Stop':[dict(description='codexon-cache-control',hooks=[]),other]}}),encoding='utf-8')
    assert remove_hooks(tmp_path);assert json.loads(path.read_text())['hooks']['Stop']==[other]
    assert not remove_hooks(tmp_path)


@pytest.fixture
def legacy(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir();directory=tmp_path/'data'
    m=ObserverManager(home,directory,'http://127.0.0.1:18769');m.retirement_index=directory/'index.sqlite'
    m.set_url(m.url);old=directory/'cache-control.sqlite';legacy_db(old)
    command=[sys.executable,'--model-proxy','--cache-worker','--codex-home',str(home),'--evidence-path',str(m.evidence),
        '--observation-index',str(m.retirement_index),'--port','18769','--upstream','chatgpt','--upstream-url','http://127.0.0.1:19001']
    m.write_state(dict(home=home_key(home),url=m.url,enabled=True,previous_url=None,phase='active'))
    runtime=dict(old=True,work=2,clock=0.,new=None,removed=False,fail=False,starts=[])
    control='a'*32
    class Task:
        def __init__(self,old=False):self.old=old
        def inspect(self):return dict(registered=self.old and not runtime['removed'],running=0,executable=command[0],arguments='synthetic')
        def suspend(self):pass
        def configure(self,c,autostart=False):runtime['configured']=c;assert not autostart
        def start(self,c,autostart=False):
            runtime['starts'].append(c)
            if '--cache-worker' in c:runtime['old']=True
            elif not runtime['fail']:runtime['new']=dict(version=PROXY_VERSION,lifecycle='managed',instance='new',pid=2)
        def remove(self):runtime['removed']=True
    m.task=Task();old_task=Task(True)
    monkeypatch.setattr('cachemonitor.retired_cache.ObserverTask',lambda *a,**k:old_task)
    monkeypatch.setattr('cachemonitor.launch_context.command_arguments',lambda _:['worker',*command[1:]])
    monkeypatch.setattr('cachemonitor.launch_context.preferences',lambda *a,**k:{})
    monkeypatch.setattr('cachemonitor.retired_cache.identity.process_identity',lambda pid:dict(pid=pid,executable=command[0]))
    monkeypatch.setattr('cachemonitor.retired_cache.identity.listener_pids',lambda _: [1] if runtime['old'] else [2] if runtime['new'] else [])
    monkeypatch.setattr('cachemonitor.retired_cache.identity.process_command',lambda _:command)
    def same(_):
        if read_json(directory/('proxy-control-'+control+'.json')).get('action')=='drain' and runtime['old']:
            with closing(sqlite3.connect(old)) as db,db:
                assert db.execute("SELECT value FROM cache_preferences WHERE key='automatic'").fetchone()==('false',)
            runtime['work']-=1
            if runtime['work']<=0:
                runtime['old']=False
                atomic_write(directory/('proxy-control-'+control+'.json'),json.dumps(dict(action='stopped',storage_flushed=True)).encode())
        return runtime['old']
    monkeypatch.setattr('cachemonitor.retired_cache.identity.same_process',same)
    monkeypatch.setattr('cachemonitor.retired_cache.identity.port_free',lambda _:not runtime['old'] and not runtime['new'])
    monkeypatch.setattr('cachemonitor.retired_cache.identity.locks_free',lambda _:not runtime['old'])
    def health(**_):return dict(cache_management=True,instance='old',pid=1,control_id=control,storage_flush_receipt=True) if runtime['old'] else runtime['new']
    monkeypatch.setattr(m,'health',health)
    def sleep(seconds):runtime['clock']+=seconds
    return m,runtime,lambda:runtime['clock'],sleep


@pytest.mark.parametrize('exit_code',[0,1])
def test_legacy_worker_without_receipt_uses_owned_process_exit(legacy,monkeypatch,exit_code):
    from contextlib import nullcontext
    m,runtime,clock,sleep=legacy;health=m.health
    def without_receipt(**kwargs):
        value=health(**kwargs)
        if value and value.get('cache_management'):value.pop('storage_flush_receipt',None)
        return value
    monkeypatch.setattr(m,'health',without_receipt)
    monkeypatch.setattr('cachemonitor.retired_cache.exit_monitor',lambda saved:nullcontext(lambda:exit_code))
    if exit_code:
        with pytest.raises(RuntimeError,match='did not exit cleanly'):retire(m,clock=clock,sleep=sleep)
        assert m.retirement_index.with_name('cache-control.sqlite').exists() and not runtime['removed']
    else:
        assert retire(m,clock=clock,sleep=sleep)
        assert runtime['removed'] and not m.retirement_index.with_name('cache-control.sqlite').exists()


@pytest.mark.parametrize('role',['--cache-worker','--cache-observe-only'])
@pytest.mark.parametrize('enabled',[True,False])
def test_worker_retirement_preserves_route_usage_and_disabled_state(legacy,role,enabled,monkeypatch):
    m,r,clock,sleep=legacy
    original=m.retirement_index.with_name('cache-control.sqlite')
    old_command=list(__import__('cachemonitor.retired_cache',fromlist=['identity']).identity.process_command(1))
    old_command[old_command.index('--cache-worker')]=role
    monkeypatch.setattr('cachemonitor.retired_cache.identity.process_command',lambda _:old_command)
    monkeypatch.setattr('cachemonitor.launch_context.command_arguments',lambda _:['worker',*old_command[1:]])
    if not enabled:m.set_url(None)
    assert retire(m,clock=clock,sleep=sleep)
    assert r['removed'] and not original.exists()
    assert m.config()[1].get('openai_base_url')==(m.url if enabled else None)
    assert '--managed' in r['configured'] and not any(x in r['configured'] for x in ('--cache-worker','--cache-observe-only','--observation-index'))
    assert '--upstream-url' in r['configured']
    with closing(sqlite3.connect(m.retirement_index)) as db,db:assert db.execute('SELECT COUNT(*) FROM usage_archive').fetchone()==(1,)
    assert not retire(m,clock=clock,sleep=sleep)


def test_failed_new_proxy_keeps_old_payload_and_revoked_permissions(legacy):
    m,r,clock,sleep=legacy;r['fail']=True
    with pytest.raises(RuntimeError,match='readiness'):retire(m,clock=clock,sleep=sleep)
    assert not r['removed'] and m.retirement_index.with_name('cache-control.sqlite').exists()
    assert '--cache-worker' in r['starts'][-1]
    with closing(sqlite3.connect(m.retirement_index.with_name('cache-control.sqlite'))) as db,db:
        assert db.execute('SELECT stopped FROM cache_operating_grants').fetchone()==('retired',)


def test_busy_request_blocks_deletion_and_new_proxy(legacy):
    m,r,clock,sleep=legacy;r['work']=1000
    with pytest.raises(RuntimeError,match='settlement'):retire(m,clock=clock,sleep=sleep)
    assert not r['starts'] and not r['removed'] and m.retirement_index.with_name('cache-control.sqlite').exists()


@pytest.mark.parametrize('resume',[True,False])
def test_retirement_preserves_closed_app_resume_intent(legacy,resume):
    m,r,clock,sleep=legacy;m.set_url(None)
    scope=dict(home=str(m.home),evidence=str(m.evidence),url=m.url,index=str(m.retirement_index))
    source=dict(command=['old.exe','--model-proxy','--cache-worker'],role='cache-worker')
    atomic_write(m.directory/'app-services.json',json.dumps(dict(scope=scope,phase='stopped',resume=resume,direct_url=None,state={},source=source)).encode())
    assert retire(m,clock=clock,sleep=sleep)
    record=read_json(m.directory/'app-services.json')
    assert record['resume']==resume and record['phase']=='stopped'
    assert record['scope']['index'] is None and record['state']['home']==home_key(m.home)
    assert record['source']['role']=='observer' and '--cache-worker' not in record['source']['command']


def test_product_has_no_cache_executor_or_control_interface():
    import inspect
    from cachemonitor.model_proxy import create_app
    root=Path(__file__).resolve().parents[1]
    for name in ('cache_capture','cache_control','cache_db','cache_execution','cache_hooks','cache_integration',
                 'cache_operating','cache_panel','cache_policy','cache_scheduler','cache_worker_control'):
        assert not (root/'cachemonitor'/(name+'.py')).exists()
    assert 'cache_capture' not in inspect.signature(create_app).parameters
    assert 'scheduler' not in inspect.signature(create_app).parameters


def test_dashboard_and_worker_share_separate_observation_directory(tmp_path,monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.proxy_target import ProxyTarget
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    home=tmp_path/'home';home.mkdir();index=tmp_path/'usage'/'index.sqlite';evidence=tmp_path/'observations'/'custom.sqlite'
    index.parent.mkdir();evidence.parent.mkdir()
    atomic_write(index.with_name('cache-route.json'),json.dumps(dict(url='http://127.0.0.1:18992')).encode())
    monkeypatch.setattr('cachemonitor.launch_context.preferences',lambda *args,**kwargs:{})
    window=Dashboard([str(home)],start_worker=False,live_limits=False,index_path=index,model_evidence_path=evidence,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    try:
        m=window.observer_panel.manager
        assert m.directory.resolve()==evidence.parent.resolve() and m.evidence.resolve()==evidence.resolve()
        assert m.retirement_index==index.resolve() and m.url=='http://127.0.0.1:18992'
        assert ProxyTarget(m).locks==[evidence.parent/'proxy-supervisor.lock']
        command=m.supervisor_command('chatgpt')
        assert command[command.index('--evidence-path')+1]==str(evidence)
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.parametrize('entry',['proxy_update','proxy_supervisor'])
def test_proxy_entry_points_use_explicit_observation_filename(tmp_path,monkeypatch,entry):
    import importlib
    module=importlib.import_module('cachemonitor.'+entry);captured={}
    evidence=tmp_path/'observations'/'custom.sqlite'
    monkeypatch.setattr(sys,'argv',['worker','--codex-home',str(tmp_path/'home'),'--evidence-path',str(evidence),'--port','18993'])
    if entry=='proxy_update':
        monkeypatch.setattr(module,'ProxyUpdate',lambda m:SimpleNamespace(run=lambda:captured.update(manager=m,command=m.command('chatgpt'))))
    else:
        monkeypatch.setattr(module,'Supervisor',lambda m,upstream,worker_command:SimpleNamespace(run=lambda:captured.update(manager=m,command=worker_command)))
    module.main()
    assert captured['manager'].evidence==evidence.resolve()
    assert captured['command'][captured['command'].index('--evidence-path')+1]==str(evidence.resolve())


def test_app_service_start_retires_before_adopting_proxy(tmp_path,monkeypatch):
    from cachemonitor.app_services import AppServices
    events=[]
    manager=SimpleNamespace(cleanup_legacy_check=lambda:events.append('cleanup'),adopt_registrations=lambda:events.append('adopt'),
        resume=lambda:events.append('resume') or {})
    owner=AppServices(None,[],collection=False);owner.manager=manager
    monkeypatch.setattr('cachemonitor.retired_cache.retire',lambda m:events.append('retire'))
    owner.start()
    assert events==['retire','cleanup','adopt','resume']
