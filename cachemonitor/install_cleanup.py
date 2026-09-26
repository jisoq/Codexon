"""Bounded, resumable cleanup after an installed GUI and its services are ready.

No timers, process termination or scheduled cleanup jobs. Unknown references and
failed inspections keep the payload, as do pending activation and rollback.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from contextlib import ExitStack

from .observer_state import ProcessLock, read_json
from .observer_control import atomic_write


def linked(path):
    try:return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except AttributeError:return path.is_symlink()
    except FileNotFoundError:return False


def safe_path(root, path):
    """Check lexical containment AND each existing ancestor before resolving."""
    root=Path(root).absolute();path=Path(path).absolute()
    if path==root or not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError('outside-installation')
    if linked(root):raise ValueError('reparse-point')
    cursor=path
    while cursor!=root:
        if linked(cursor):raise ValueError('reparse-point')
        cursor=cursor.parent
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('outside-installation')
    return path


def payload(root, value, kind):
    path=safe_path(root,Path(value))
    relative=path.relative_to(root)
    if kind=='product':
        if len(relative.parts)!=3 or relative.parts[0]!='versions' or relative.parts[2]!='Codexon':
            raise ValueError('unknown-product-layout')
    else:
        if len(relative.parts)!=3 or relative.parts[0]!='maintenance' or relative.parts[2]!='CodexonRecovery.exe':
            raise ValueError('unknown-recovery-layout')
    return safe_path(root,root/relative.parts[0]/relative.parts[1])


def referenced(path, text):
    # Also accept JSON-escaped paths and forward slashes in command arguments.
    haystack=str(text).replace('\\\\','\\').replace('/','\\').casefold()
    needle=str(path).replace('/','\\').rstrip('\\').casefold()
    return needle+'\\' in haystack or needle+'"' in haystack or haystack==needle


def discover(root, current, previous):
    found={}; rejected=[]
    records=[current,*[read_json(p) for p in root.glob('installation-*.json')]]
    records += [dict(product=r['previous']) for r in list(records) if r.get('previous')]
    for record in records:
        for kind in ('product','recovery'):
            if not record.get(kind):continue
            try:
                path=payload(root,record[kind],kind)
                found[path]=dict(path=str(path),kind=kind)
            except (ValueError,OSError) as exc:
                rejected.append(dict(path=str(record[kind]),status='deferred',reason=str(exc)))
    # Keep partial deletions discoverable even when their manifests are gone.
    for item in previous.get('items',[]):
        if item.get('kind') not in ('product','recovery'):continue
        try:
            path=safe_path(root,Path(item['path']))
            area={'product':'versions','recovery':'maintenance'}[item['kind']]
            if path.parent!=root/area:continue
            found.setdefault(path,dict(path=str(path),kind=item['kind']))
        except (ValueError,OSError,KeyError):continue
    protected={payload(root,current[k],k) for k in ('product','recovery')}
    return [v for k,v in found.items() if k not in protected],rejected


def external_references(homes):
    texts=[]
    for home in homes:
        path=Path(home)/'hooks.json'
        if path.exists():texts.append(path.read_text(encoding='utf-8-sig'))
    if os.name=='nt':
        import winreg
        from .install_management import STARTUP_KEY
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,STARTUP_KEY) as key:
                count=winreg.QueryInfoKey(key)[1]
                for n in range(count):texts.append(str(winreg.EnumValue(key,n)[1]))
        except FileNotFoundError:pass
    return texts


def remove_tree(root,path):
    safe_path(root,path)
    if not path.exists():return
    # Do not traverse even an internal junction. A partial deletion can safely
    # be retried; an external target must never be visited.
    for parent,dirs,files in os.walk(path,followlinks=False):
        for name in [*dirs,*files]:safe_path(root,Path(parent)/name)
    shutil.rmtree(path)


def queue(root):
    """Installer records candidates; only a ready GUI may delete them."""
    root=Path(root).absolute()
    current=read_json(root/'installation.json')
    path=root/'cleanup.json'
    candidates,rejected=discover(root,current,read_json(path))
    atomic_write(path,json.dumps(dict(product=current['product'],checked_at=time.time(),
        items=[*rejected,*[dict(i,status='deferred',reason='transition-pending') for i in candidates]],
        tasks=[]),ensure_ascii=False,indent=2).encode())


def reconcile_tasks(tasks, candidates, current, processes, scopes, services):
    from . import install_tasks
    from .launch_context import command_arguments
    changes=[]
    for task in tasks:
        touched=[Path(i['path']) for i in candidates if referenced(i['path'],task.get('actions',[]))]
        if not touched:continue
        outcome=dict(name=task['name'],status='deferred',reason='unverified-or-active')
        changes.append(outcome)
        role=install_tasks.owned_role(task,scopes|{str(p/'Codexon'/'Codexon.exe') for p in touched})
        if not role or task.get('running') or task.get('state')==2:continue
        if any(any(referenced(p,proc.get('ExecutablePath','')) for p in touched) for proc in processes):continue
        if task.get('count')!=1 or len(task['actions'])!=1:continue
        action=task['actions'][0]
        if not any(referenced(p,action['executable']) for p in touched):continue
        command=[action['executable'],*command_arguments('worker '+action.get('arguments',''))[1:]]
        replacement=None
        if role in ('CacheWorker','ProxySupervisor','UsageCollector'):
            if role=='UsageCollector':
                from .proxy_target import option
                if '--usage-collector' not in command or not services.index:continue
                if Path(option(command,'--index-path','')).resolve()!=Path(services.index).resolve():continue
                homes=[str(Path(command[n+1]).resolve()) for n,arg in enumerate(command[:-1]) if arg=='--codex-home']
                if not homes or not set(homes).issubset({str(Path(h).resolve()) for h in services.homes}):continue
                expected=getattr(services,'evidence',None) or Path(services.index).with_name('model-evidence.sqlite')
                if Path(option(command,'--evidence-path',str(expected))).resolve()!=Path(expected).resolve():continue
                if not services.collection:continue
                replacement=[str(Path(current['product'])/'Codexon.exe'),*command[1:]]
            else:
                if not services.target:continue
                try:replacement=services.target.replacement({'command':command})
                except (ValueError,RuntimeError):continue
        elif role=='ConnectionCheck':
            from .proxy_target import option
            m=services.manager
            if not m or '--check' not in command:continue
            if (Path(option(command,'--codex-home','')).resolve()!=m.home.resolve()
                    or Path(option(command,'--data-dir','')).resolve()!=m.directory.resolve()
                    or option(command,'--proxy-url')!=m.url):continue
        elif role not in ('Desktop','Installer'):
            if not services.target:continue
            probe=list(command)
            if role=='ProxyUpdate':
                if '--proxy-update' not in probe:continue
                probe[probe.index('--proxy-update')]='--model-proxy'
            try:services.target.validate_command(probe)
            except (ValueError,RuntimeError):continue
        # Obsolete roles are retired only after readiness and process exit.
        try:
            install_tasks.change(task,replacement)
            outcome.update(status='migrated' if replacement else 'removed',reason='')
        except (OSError,ValueError,RuntimeError) as exc:outcome['reason']=str(exc)
    return changes


def cleanup(root, executable, services):
    """Called only by the ready current GUI, with service transition locks held."""
    from .install_management import processes_under
    from . import install_tasks
    root=Path(root).absolute();executable=Path(executable).absolute()
    with ProcessLock(root/'install.lock'):
        current=read_json(root/'installation.json')
        if Path(current.get('product',''))/'Codexon.exe'!=executable:
            raise RuntimeError('not-current-installation')
        if (root/'activation-pending.json').exists():raise RuntimeError('activation-pending')
        journal=root/'cleanup.json';previous=read_json(journal)
        candidates,rejected=discover(root,current,previous)
        report=dict(product=current['product'],checked_at=time.time(),items=list(rejected),tasks=[])
        # Persist the full candidate list BEFORE mutating registrations/files.
        report['items'] += [dict(i,status='deferred',reason='inspection-pending') for i in candidates]
        atomic_write(journal,json.dumps(report,ensure_ascii=False,indent=2).encode())
        try:
            processes=processes_under(root)
            tasks=install_tasks.inventory()
            scopes={str(root),*[str(Path(h).resolve()) for h in services.homes]}
            if services.index:scopes.add(str(Path(services.index).resolve()))
            report['tasks']=reconcile_tasks(tasks,candidates,current,processes,scopes,services)
            tasks=install_tasks.inventory()
            homes=set(services.homes)
            for record in [current,*[read_json(p) for p in root.glob('installation-*.json')]]:
                homes.update(h for h in record.get('homes',[]) if isinstance(h,str) and Path(h).is_absolute())
            refs=external_references(homes)
            for item in report['items'][len(rejected):]:
                path=Path(item['path'])
                try:
                    safe_path(root,path)
                    # A service or installer may have started since inventory.
                    if any(referenced(path,p.get('ExecutablePath','')) or referenced(path,p.get('CommandLine',''))
                           for p in processes_under(root)):reason='process-running'
                    elif any(referenced(path,t.get('actions',[])) for t in tasks):reason='task-reference'
                    elif any(referenced(path,text) for text in refs):reason='startup-or-hook-reference'
                    else:
                        remove_tree(root,path)
                        item.update(status='removed',reason='');continue
                    item.update(status='deferred',reason=reason)
                except (OSError,ValueError,RuntimeError) as exc:item.update(status='deferred',reason=str(exc))
        except (OSError,ValueError,RuntimeError) as exc:
            report['error']=str(exc)
        finally:atomic_write(journal,json.dumps(report,ensure_ascii=False,indent=2).encode())
        return report


def after_services(services):
    """Use the existing startup/status flow to notice readiness, then run once.

    Failed file deletion is retried on the next GUI start, not every status tick.
    Source runs, isolated probes and MSIX overlays never clean installations.
    """
    if not getattr(sys,'frozen',False) or not getattr(services,'gui_ready',False) or services.closing.is_set():return
    if getattr(services,'cleanup_attempted',False):return
    from .install_dispatch import packaged_context
    if packaged_context():return
    exe=Path(sys.executable).absolute()
    if exe.name!='Codexon.exe' or exe.parent.name!='Codexon' or exe.parent.parent.parent.name!='versions':return
    root=exe.parent.parent.parent.parent
    from .proxy_update import BUSY
    from .proxy_identity import deployment
    from .version import PROXY_VERSION
    with ExitStack() as locks:
        if services.manager:
            m=services.manager
            locks.enter_context(ProcessLock(m.directory/'proxy-update.lock'))
            locks.enter_context(ProcessLock(m.control_lock))
            update=read_json(m.directory/'proxy-update.json')
            if update.get('phase') in BUSY or update.get('phase')=='failed' and not update.get('restored'):return
            health=m.health(timeout=1)
            if services.target.enabled() or health:
                if not health:return
                source=services.target.capture(health)
                if not services.target.ready(health,{**source,'instance':''},[str(exe),*source['command'][1:]],
                                             PROXY_VERSION,deployment(exe)):return
        if services.collection:
            from .usage_collection import CollectionChannel
            channel=CollectionChannel(services.homes,services.index,services.evidence)
            try:
                locks.enter_context(ProcessLock(channel.companion('.collector-update.lock')))
                snapshot=channel.read()
                source=services.collector_identity(channel,snapshot)
                if not source or Path(source['process']['executable']).resolve()!=exe.resolve():return
                if not snapshot or time.time()-snapshot.get('ts',0)>30:return
            finally:channel.close()
        services.cleanup_report=cleanup(root,exe,services)
        services.cleanup_attempted=True
