"""Start the replacement GUI; the existing instance exits only after its handshake."""
import os
from pathlib import Path
import subprocess
import sys
import time

HANDOFF_TIMEOUT=30
REPLACEMENT_TIMEOUT=45


def handoff_ready(client, timeout=3000):
    """Read one bounded reply, including bytes received while flushing the request."""
    deadline=time.monotonic()+timeout/1000
    reply=b''
    while time.monotonic()<deadline:
        if not client.bytesAvailable() and not client.waitForReadyRead(max(1,int((deadline-time.monotonic())*1000))):
            return False
        reply+=bytes(client.read(14-len(reply)))
        if reply==b'ready-to-exit':return True
        if not b'ready-to-exit'.startswith(reply):return False
    return False


def launch_replacement(homes, *, index_path=None, handoff=None,evidence_path=None,quota_path=None,managed_services=False,executable=None,continue_update=None):
    if continue_update is not None:
        from .update_state import valid_id
        if not valid_id(continue_update):raise ValueError('Invalid update continuation operation')
    command=[str(Path(executable).resolve()) if executable is not None else sys.executable]
    if executable is None and not getattr(sys,'frozen',False):command.append(str(Path(__file__).resolve().parents[1]/'run.py'))
    command.append('--replace-gui')
    if continue_update:command.extend(('--continue-update',continue_update))
    if managed_services:command.append('--managed-services')
    for home in homes:command.extend(('--codex-home',str(home)))
    if index_path:command.extend(('--index-path',str(index_path)))
    if evidence_path:command.extend(('--evidence-path',str(evidence_path)))
    if quota_path:command.extend(('--quota-path',str(quota_path)))
    if handoff:command.extend(('--verify-handoff',str(handoff)))
    env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1')
    env.pop('CODEXON_LANGUAGE',None)
    return subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
