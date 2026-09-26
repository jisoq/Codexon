"""Verify a live login job hands off to a reinstalled app via LaunchServices."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor import macos_installation as mac
from cachemonitor.launch_context import save_homes,save_cache_paths
from cachemonitor.macos_startup import MacStartup
from cachemonitor.macos_process import process_identity
from cachemonitor.observer_state import read_json
from cachemonitor.proxy_identity import process_exited
from tools.verify_proxy_update import isolated_environment,stop_task


def wait_for(read,predicate,timeout=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=read()
        if predicate(value):return value
        time.sleep(.1)
    raise RuntimeError('The isolated installed GUI did not reach the expected state.')


def close_gui(index):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    app=QCoreApplication.instance() or QCoreApplication([])
    client=QLocalSocket()
    client.connectToServer('CodexonQA-'+hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
    if client.waitForConnected(3000):
        client.write(b'update-exit');client.waitForBytesWritten(3000);client.waitForReadyRead(5000)
        client.disconnectFromServer()


def verify(source,root,allow_ad_hoc):
    source=source.resolve()
    if (source/'Install Codexon.app').is_dir():source=source/'Install Codexon.app/Contents/Resources/payload'
    homes=[root/'Work Home',root/'Second Home']
    for home in homes:home.mkdir();(home/'codexon-test-home').touch()
    data=root/'cache';data.mkdir();index=data/'index.sqlite';evidence=data/'evidence.sqlite'
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port not in (8768,8771)
    (data/'cache-route.json').write_text(json.dumps(dict(url=f'http://127.0.0.1:{port}')))
    save_homes(homes);save_cache_paths(index,evidence,data/'quota.sqlite')
    options=dict(root=mac.install_root(),applications=root/'Applications',allow_ad_hoc=allow_ad_hoc)
    first=mac.install_source(source,**options,isolated=True,launch=False)
    report=root/'gui.json'
    arguments=['--index-path',str(index),'--evidence-path',str(evidence),'--verify-handoff',str(report),'--hidden']
    for home in homes:arguments+=['--codex-home',str(home)]
    task=MacStartup().task;identities=[]
    try:
        with patch.object(sys,'frozen',True,create=True):task.start([first['AppPath'],*arguments],autostart=True)
        before=wait_for(lambda:read_json(report),lambda value:value.get('ready'))
        previous=process_identity(before['pid']);identities.append(previous)
        assert previous['executable']==first['AppPath'] and task.inspect()['running']==1
        with patch.object(sys,'frozen',True,create=True):second=mac.install_source(source,**options,isolated=False,launch=True)
        after=wait_for(lambda:read_json(report),lambda value:value.get('ready') and value.get('pid')!=before['pid'])
        current=process_identity(after['pid']);identities.append(current)
        assert current['executable']==second['AppPath'] and before['homes']==after['homes']==list(map(str,homes))
        wait_for(lambda:process_exited(previous),bool)
        login=task.inspect()
        assert login['command']==[second['AppPath'],*arguments] and login['autostart']
        close_gui(index);wait_for(lambda:process_exited(current),bool)
        removed=mac.prepare_uninstall(options['root'])
        assert removed['ready'] and not task.inspect()['registered']
        return dict(passed=True,previous_gui_exited=True,actual_installed_paths=True,
                    launchservices_handoff=True,multiple_custom_homes_preserved=True,
                    login_preferences_preserved=True,owned_services_removed=True,
                    model_requests=0,live_quota_requests=0,production_services_changed=False)
    finally:
        close_gui(index)
        for identity in identities:
            if identity and not process_exited(identity):
                # Only the exact test GUI captured above may be terminated.
                if process_identity(identity['pid'])==identity:os.kill(identity['pid'],signal.SIGTERM)
                wait_for(lambda:process_exited(identity),bool)
        if task.inspect().get('registered'):stop_task(task)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-ad-hoc',action='store_true')
    args=parser.parse_args()
    if sys.platform!='darwin':parser.error('Run on macOS')
    with tempfile.TemporaryDirectory(prefix='codexon-installed-handoff-') as temp:
        root=Path(temp)
        with isolated_environment(root):result=verify(args.product,root,args.allow_ad_hoc)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
