"""Explicit opt-in hook registration; never changes model or permission settings."""
import json
import os
from pathlib import Path
import subprocess
import sys

MARKER='codexon-cache-control'


def command(database):
    if getattr(sys,'frozen',False):
        parts=[str(Path(sys.executable).with_name('CodexonHook.exe')),'--cache-hook']
    else:parts=[sys.executable,str(Path(__file__).resolve().parents[1]/'run.py'),'--cache-hook']
    parts+=['--database',str(Path(database).resolve())]
    return subprocess.list2cmdline(parts), '& '+' '.join("'"+p.replace("'","''")+"'" for p in parts)


def configure(home,database,enabled):
    path=Path(home)/'hooks.json'
    original=path.read_bytes() if path.exists() else None
    document=json.loads(original.decode('utf-8-sig')) if original else {}
    hooks=document.setdefault('hooks',{})
    if not isinstance(hooks,dict):raise ValueError('Invalid hooks.json')
    normal,windows=command(database)
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
