"""Run only an isolated (-Isolated) installer through install/update/removal."""
import argparse
import ctypes
from ctypes import wintypes as W
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import winreg

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_state import read_json

KEY=r'Software\Codexon-QA'


def registration():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,KEY) as key:
            return {name:winreg.QueryValueEx(key,name)[0] for name in ('InstallRoot','AppPath','RecoveryPath')}
    except FileNotFoundError:return {}


def run(command):
    return subprocess.run(list(map(str,command)),timeout=180,creationflags=subprocess.CREATE_NO_WINDOW).returncode


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if registration():raise RuntimeError('An existing QA installation must be preserved; use a clean QA environment')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    install=root/'installed'
    command=[args.installer.resolve(),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',f'/DIR={install}']
    assert run([*command,f'/LOG={root / "install.log"}'])==0
    first=registration();assert Path(first['InstallRoot'])==install
    assert Path(first['AppPath']).is_file() and Path(first['RecoveryPath']).is_file()
    record=install/'preserved-record.txt';record.write_text('existing user record')
    assert run([*command,f'/LOG={root / "update.log"}'])==0
    second=registration()
    assert first['AppPath']!=second['AppPath'] and Path(first['AppPath']).is_file()
    assert read_json(install/'installation.json')['previous']==str(Path(first['AppPath']).parent)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Classes\codexon-recovery-qa\shell\open\command') as key:
        assert second['RecoveryPath'] in winreg.QueryValueEx(key,'')[0]
    # Keep a real independent recovery window open to test removal deferral.
    home=root/'home';home.mkdir();(home/'codexon-test-home').touch()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    report=root/'ui.json'
    process=subprocess.Popen([second['RecoveryPath'],'--codex-home',str(home),'--data-dir',str(root/'data'),
                              '--proxy-url',f'http://127.0.0.1:{port}','--ui-smoke',str(report)],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    uninstaller=next(install.glob('unins*.exe'))
    uninstall=[uninstaller,'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART']
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not read_json(report).get('hwnd'):time.sleep(.2)
        assert read_json(report).get('hwnd')
        blocked=run([*uninstall,f'/LOG={root / "blocked-uninstall.log"}'])
        assert blocked!=0 and registration()==second and Path(second['AppPath']).exists()
    finally:
        user=ctypes.WinDLL('user32');user.GetAncestor.argtypes=[W.HWND,W.UINT];user.GetAncestor.restype=W.HWND
        user.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]
        hwnd=read_json(report).get('hwnd')
        if hwnd:user.PostMessageW(user.GetAncestor(hwnd,2),0x0010,0,0)
        process.wait(timeout=20)
    assert run([*uninstall,f'/LOG={root / "uninstall.log"}'])==0
    assert not registration() and not Path(second['AppPath']).exists()
    assert record.read_text()=='existing user record'
    result=dict(passed=True,install=True,reinstall=True,old_payload_preserved=True,
                busy_uninstall_deferred=True,uninstall=True,records_preserved=True)
    (root/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':main()
