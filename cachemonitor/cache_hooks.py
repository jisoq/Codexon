"""Explicit opt-in hook registration; never changes model or permission settings."""
import json
import os
from pathlib import Path
import subprocess
import sys

MARKER='codexon-cache-control'


def remove_installation(home,root):
    """Remove only owned entries that would point into the deleted payload."""
    from .launch_context import command_arguments
    path=Path(home)/'hooks.json'
    if not path.exists():return False
    original=path.read_bytes();document=json.loads(original.decode('utf-8-sig'))
    hooks=document.get('hooks',{})
    if not isinstance(hooks,dict):raise ValueError('Invalid hooks.json')
    root=Path(root).resolve();changed=False
    for event,entries in hooks.items():
        if not isinstance(entries,list):raise ValueError('Invalid hook entries')
        retained=[]
        for entry in entries:
            owned=False
            if entry.get('description')==MARKER:
                for hook in entry.get('hooks',[]):
                    args=command_arguments(hook.get('command','')) if hook.get('command') else []
                    if args and Path(args[0]).name.lower()=='codexonhook.exe' and Path(args[0]).resolve().is_relative_to(root):owned=True
            if owned:changed=True
            else:retained.append(entry)
        hooks[event]=retained
    if not changed:return False
    backup=path.with_name('hooks.before-codexon-uninstall.json')
    if not backup.exists():backup.write_bytes(original)
    temporary=path.with_name('hooks.codexon.tmp')
    temporary.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if path.read_bytes()!=original:
        temporary.unlink();raise RuntimeError('Hook file changed; try again')
    os.replace(temporary,path)
    return True


def command(database,observe_only=False,executable=None):
    if executable:
        parts=[str(Path(executable).resolve()),'--cache-hook']
    elif getattr(sys,'frozen',False):
        parts=[str(Path(sys.executable).with_name('CodexonHook.exe')),'--cache-hook']
    else:parts=[sys.executable,str(Path(__file__).resolve().parents[1]/'run.py'),'--cache-hook']
    parts+=['--database',str(Path(database).resolve())]
    if observe_only:parts+=['--observe-only']
    return subprocess.list2cmdline(parts), '& '+' '.join("'"+p.replace("'","''")+"'" for p in parts)


def configure(home,database,enabled,*,observe_only=False,executable=None):
    path=Path(home)/'hooks.json'
    original=path.read_bytes() if path.exists() else None
    document=json.loads(original.decode('utf-8-sig')) if original else {}
    hooks=document.setdefault('hooks',{})
    if not isinstance(hooks,dict):raise ValueError('Invalid hooks.json')
    normal,windows=command(database,observe_only,executable)
    for event in ('UserPromptSubmit','Stop'):
        entries=hooks.get(event,[])
        if not isinstance(entries,list):raise ValueError('Invalid hook entries')
        # Remove only entries we own; retain all other hooks and document fields.
        entries=[e for e in entries if e.get('description')!=MARKER]
        if enabled:entries.append(dict(description=MARKER,hooks=[dict(type='command',command=normal,
            commandWindows=windows,timeout=75 if event=='UserPromptSubmit' else 10)]))
        hooks[event]=entries
    path.parent.mkdir(parents=True,exist_ok=True)
    if original is not None:
        backup=path.with_name('hooks.before-codexon.json')
        if not backup.exists():backup.write_bytes(original)
    temporary=path.with_name('hooks.codexon.tmp')
    temporary.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # Do not overwrite a simultaneous editor change.
    if (path.read_bytes() if path.exists() else None)!=original:
        temporary.unlink();raise RuntimeError('Hook file changed; try again')
    os.replace(temporary,path)
    return path
