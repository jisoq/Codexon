"""Start the replacement GUI; the existing instance exits only after its handshake."""
import os
from pathlib import Path
import subprocess
import sys


def launch_replacement(homes, *, index_path=None, handoff=None):
    command=[sys.executable]
    if not getattr(sys,'frozen',False):command.append(str(Path(__file__).resolve().parents[1]/'run.py'))
    command.append('--replace-gui')
    for home in homes:command.extend(('--codex-home',str(home)))
    if index_path:command.extend(('--index-path',str(index_path)))
    if handoff:command.extend(('--verify-handoff',str(handoff)))
    env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1')
    env.pop('CODEXON_LANGUAGE',None)
    return subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
