"""One-way retirement of legacy cache workers. Never issues model requests."""
import json
from pathlib import Path
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit

from .observer_control import atomic_write
from .observer_state import ProcessLock,read_json
from .observer_task import ObserverTask
from .proxy_target import option
from . import proxy_identity as identity

FLAGS=('--cache-worker','--cache-observe-only')
MARKER='codexon-cache-control'


def legacy_command(command):return any(flag in command for flag in FLAGS)


def select_route(manager,index=None):
    from .usage_paths import index_location
    from .launch_context import resolve_homes
    default=index_location() if Path(resolve_homes()[0]).resolve()==manager.home.resolve() else manager.directory/'usage-index.sqlite'
    manager.retirement_index=Path(index or default).resolve()
    address=read_json(manager.retirement_index.with_name('cache-route.json')).get('url')
    if not address:address=manager.config()[1].get('openai_base_url')
    parsed=urlsplit(address or '')
    if parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.username and parsed.path in ('','/'):
        manager.url=address.rstrip('/')


def migrate_preferences(manager):
    from .launch_context import preferences,preference_path
    document=preferences();paths=document.get('cache_paths') or {}
    if paths.get('index_path') and Path(paths['index_path']).resolve()==manager.retirement_index:
        document['connection_paths']=document.pop('cache_paths')
        atomic_write(preference_path(),json.dumps(document,ensure_ascii=False).encode())


def settle_old_collector(manager):
    """Stop an old reader before it can recreate its retired journal."""
    from .usage_collection import CollectionChannel
    from .version import VERSION
    channel=CollectionChannel([str(manager.home)],manager.retirement_index,manager.evidence)
    try:snapshot=channel.read_header() or {}
    finally:channel.close()
    source=snapshot.get('collection') or {}
    if source.get('pid') and source.get('version')!=VERSION:
        from .app_services import AppServices
        AppServices(manager,snapshot.get('homes') or [str(manager.home)],manager.retirement_index,manager.evidence).stop_collection()


def migrate_exit_record(manager,command,state):
    """Keep explicit shutdown/resume intent while removing the retired role."""
    from .proxy_target import ProxyTarget
    path=manager.directory/'app-services.json';record=read_json(path)
    scope=record.get('scope') or {};ordinary=ProxyTarget(manager).scope
    if (scope.get('index')!=str(manager.retirement_index) or
            any(scope.get(k)!=ordinary[k] for k in ('home','evidence','url'))):return
    record['scope']=ordinary;record['state']=state
    source=record.get('source')
    if source and legacy_command(source.get('command',[])):
        source.update(command=command,role='observer',state_phase=state.get('phase'))
    atomic_write(path,json.dumps(record,ensure_ascii=False).encode())


def remove_hooks(home,*,before_write=lambda path,data:None):
    path=Path(home)/'hooks.json'
    if not path.exists():return False
    original=path.read_bytes();document=json.loads(original.decode('utf-8-sig'))
    hooks=document.get('hooks',{})
    if not isinstance(hooks,dict):raise ValueError('Invalid hooks document')
    changed=False
    for event,entries in hooks.items():
        if not isinstance(entries,list):raise ValueError('Invalid hook entries')
        retained=[entry for entry in entries if entry.get('description')!=MARKER]
        changed|=len(retained)!=len(entries);hooks[event]=retained
    if not changed:return False
    data=(json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode()
    before_write(path,data)
    if path.read_bytes()!=original:raise RuntimeError('Hook file changed during retirement')
    atomic_write(path,data);return True


def import_usage(source,index):
    """Commit each transmitted response once before deleting its old database."""
    source=Path(source);index=Path(index)
    if not source.exists():return 0
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as old:
        old.row_factory=sqlite3.Row;old.execute('PRAGMA query_only=ON')
        if not old.execute("SELECT 1 FROM sqlite_master WHERE name='cache_jobs'").fetchone():raise ValueError('Unexpected retirement database')
        rows=old.execute('SELECT * FROM cache_jobs WHERE started IS NOT NULL').fetchall()
    index.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(index)) as db:
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables-{'files','events','metadata','usage_archive'}:raise ValueError('Unexpected usage database')
        db.execute('CREATE TABLE IF NOT EXISTS usage_archive(home TEXT,response_id TEXT,data TEXT,PRIMARY KEY(home,response_id))')
        for r in rows:
            row=dict(r);usage=json.loads(row.get('usage') or '{}')
            rid=row.get('response_id') or 'maintenance:'+row['id']
            data=dict(home=row['home'],sid=row['sid'],key=rid,ts=row.get('ended') or row['started'],
                request_start=row['started'],request_end=row.get('ended'),model=row.get('model'),effort=row.get('effort'),
                service_tier=row.get('tier') or '미확인',purpose=row.get('purpose') or 'maintenance',
                state=row['state'] if row['state'] not in ('reserved','sent') else 'unknown',
                **{k:usage.get(k) for k in ('input','cached','written','output','reasoning','total')})
            previous=db.execute('SELECT data FROM usage_archive WHERE home=? AND response_id=?',(row['home'],rid)).fetchone()
            encoded=json.dumps(data,ensure_ascii=False,sort_keys=True)
            if previous and json.loads(previous[0])!=data:raise RuntimeError('Conflicting archived response')
            db.execute('INSERT OR IGNORE INTO usage_archive VALUES(?,?,?)',(row['home'],rid,encoded))
        db.commit()
        for r in rows:
            rid=r['response_id'] or 'maintenance:'+r['id']
            if not db.execute('SELECT 1 FROM usage_archive WHERE home=? AND response_id=?',(r['home'],rid)).fetchone():
                raise RuntimeError('Usage retirement verification failed')
    return len(rows)


def validate(manager,command):
    if not (legacy_command(command) or '--managed' in command) or '--model-proxy' not in command:raise RuntimeError('Unknown retired worker role')
    pairs=[('--codex-home',manager.home),('--evidence-path',manager.evidence)]
    if legacy_command(command):pairs.append(('--observation-index',manager.retirement_index))
    for flag,path in pairs:
        value=option(command,flag)
        if not value or Path(value).resolve()!=Path(path).resolve():raise RuntimeError('Retired worker path mismatch')
    if int(option(command,'--port',8768))!=urlsplit(manager.url).port:raise RuntimeError('Retired worker port mismatch')


def retire(manager,*,restore_only=False,clock=time.monotonic,sleep=time.sleep):
    from .launch_context import command_arguments
    from .model_evidence import home_key
    from .version import PROXY_VERSION
    if not hasattr(manager,'retirement_index'):select_route(manager)
    health=manager.health(timeout=3)
    storage=manager.retirement_index.parent
    if not (storage/'cache-control.sqlite').exists() and (manager.directory/'cache-control.sqlite').exists():storage=manager.directory
    database=storage/'cache-control.sqlite'
    receipt_path=manager.directory/'retirement.json'
    pending=read_json(receipt_path)
    if not (health or {}).get('cache_management') and not database.exists() and not pending and not manager.retirement_index.with_name('cache-route.json').exists():
        remove_hooks(manager.home);return False
    task=ObserverTask(str(manager.home),role='CacheWorker')
    registration=task.inspect()
    with ProcessLock(manager.control_lock,timeout=30):
        command=pending.get('command')
        if registration.get('registered'):
            command=[registration['executable'],*command_arguments('worker '+registration.get('arguments',''))[1:]]
            validate(manager,command)
        old=None
        if (health or {}).get('cache_management'):
            old=identity.process_identity(health['pid'])
            if not old or identity.listener_pids(manager.url)!=[old['pid']]:raise RuntimeError('Retired listener ownership mismatch')
            actual=identity.process_command(old['pid']);validate(manager,actual)
            command=command or actual
        if not command and registration.get('running'):raise RuntimeError('Retired registration ownership unknown')
        enabled=pending.get('enabled',manager.config()[1].get('openai_base_url')==manager.url)
        state=manager.state()
        upstream=option(command or [],'--upstream','chatgpt')
        if database.exists():
            with closing(sqlite3.connect(database)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'cache_preferences','cache_jobs'}.issubset(tables):raise ValueError('Unexpected retirement database')
                db.execute("INSERT OR REPLACE INTO cache_preferences VALUES('automatic','false')")
                db.execute("INSERT OR REPLACE INTO cache_preferences VALUES('enabled','false')")
                if 'cache_operating_grants' in tables:
                    db.execute("UPDATE cache_operating_grants SET stopped=COALESCE(stopped,'retired')")
                db.commit()
        atomic_write(receipt_path,json.dumps(dict(command=command,enabled=enabled,state=state)).encode())
        task.suspend()
        if old:
            if not legacy_command(command):manager.task.suspend()
            control=health.get('control_id','')
            import re
            if not re.fullmatch('[0-9a-f]{32}',control):raise RuntimeError('Retired worker lacks safe shutdown')
            atomic_write(manager.directory/('proxy-control-'+control+'.json'),json.dumps(dict(id=control,action='drain')).encode())
            deadline=clock()+30
            while identity.same_process(old) or not identity.port_free(manager.url):
                if clock()>=deadline:raise RuntimeError('Retired response settlement incomplete; connection preserved')
                sleep(.2)
            final=read_json(manager.directory/('proxy-control-'+control+'.json'))
            if final.get('action')!='stopped' or not final.get('storage_flushed'):raise RuntimeError('Retired usage flush unconfirmed')
        deadline=clock()+30
        while task.inspect().get('running') or not identity.locks_free([storage/'cache-worker.lock']):
            if clock()>=deadline:raise RuntimeError('Retired task or storage settlement incomplete')
            sleep(.2)
        new_command=manager.supervisor_command(upstream)
        for flag in ('--upstream-url','--upstream-ca'):
            value=option(command or [],flag)
            if value:new_command.extend((flag,value))
        new_state=dict(state,home=home_key(manager.home),url=manager.url,upstream=upstream,previous_url=state.get('previous_url'),
            enabled=bool(enabled and not restore_only),pending=False,phase='starting' if enabled and not restore_only else 'off')
        manager.write_state(new_state)
        try:
            manager.task.configure(new_command,autostart=False)
            if enabled and not restore_only and not manager.health(timeout=1):
                manager.task.start(new_command,autostart=False)
                deadline=clock()+30
                while True:
                    current=manager.health(timeout=1)
                    if current and current.get('version')==PROXY_VERSION and current.get('lifecycle')=='managed' and not current.get('cache_management'):break
                    if clock()>=deadline:raise RuntimeError('Observation proxy readiness unconfirmed')
                    sleep(.2)
            if restore_only and manager.config()[1].get('openai_base_url')==manager.url:manager.set_url(new_state['previous_url'])
        except Exception:
            # Do not revive paid maintenance permission. Preserve the old payload
            # and route until an independently verified transition can complete.
            current=manager.health(timeout=1)
            if current and not current.get('cache_management'):
                from .proxy_target import ProxyTarget
                from .proxy_drain import ProxyDrain
                target=ProxyTarget(manager);source=target.capture(current)
                if Path(source['command'][0]).resolve()!=Path(new_command[0]).resolve():
                    raise RuntimeError('Unconfirmed replacement preserved; rollback deferred')
                ProxyDrain(manager,target,clock=clock,sleep=sleep).run(source)
            manager.write_state(state or dict(home=home_key(manager.home),enabled=False,phase='failed'))
            if command and identity.port_free(manager.url):task.start(command,autostart=False)
            raise
        settle_old_collector(manager)
        migrate_exit_record(manager,new_command,new_state)
        if registration.get('registered'):task.remove()
        # The new observer uses its own lock in the evidence directory. Only the
        # old worker lock guards the files being removed here.
        import_usage(database,manager.retirement_index)
        remove_hooks(manager.home)
        for name in ('cache-control.sqlite','cache-control.sqlite-wal','cache-control.sqlite-shm','cache-route.json','cache-worker.lock'):
            path=(storage/name).resolve()
            if path.parent!=storage.resolve():raise RuntimeError('Retirement path mismatch')
            path.unlink(missing_ok=True)
        migrate_preferences(manager)
        receipt_path.unlink(missing_ok=True)
        return True
