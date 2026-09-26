"""Run only an isolated (-Isolated) installer through install/update/removal."""
import argparse
import ctypes
from ctypes import wintypes as W
import json
import os
import base64
from pathlib import Path
import socket
import subprocess
import sys
import time
import winreg
import uuid
from contextlib import contextmanager

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_state import read_json
from tools.installer_identity import require_qa_installer

KEY=r'Software\Codexon-QA'


def registration():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,KEY) as key:
            return {name:winreg.QueryValueEx(key,name)[0] for name in ('InstallRoot','AppPath','RecoveryPath')}
    except FileNotFoundError:return {}


def run(command):
    if os.environ.get('CODEXON_QA_PACKAGE_FAMILY') and Path(command[0]).name=='Codexon-Setup.exe':
        require_qa_installer(Path(command[0]))
        proof=Path(os.environ['CODEXON_QA_PACKAGE_REPORTS'])/(uuid.uuid4().hex+'.json')
        request=dict(family=os.environ['CODEXON_QA_PACKAGE_FAMILY'],launcher=os.environ['CODEXON_QA_PACKAGE_LAUNCHER'],
            arguments=subprocess.list2cmdline([str(command[0]),subprocess.list2cmdline(list(map(str,command[1:]))),str(proof)]))
        encoded=base64.b64encode(json.dumps(request).encode()).decode()
        script="$p=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+encoded+"'))); Invoke-CommandInDesktopPackage -PackageFamilyName $p.family -AppId Launcher -Command $p.launcher -Args $p.arguments -PreventBreakaway -ErrorAction Stop"
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16-le')).decode()],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            value=read_json(proof)
            if value:
                assert value['packaged'] is True,'MSIX context was not established'
                return value['exit_code']
            time.sleep(.5)
        raise RuntimeError('Packaged installer did not complete; preserve its evidence')
    return subprocess.run(list(map(str,command)),timeout=180,creationflags=subprocess.CREATE_NO_WINDOW).returncode


@contextmanager
def prevent_receipt_replace(path):
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    api.CreateFileW.restype=W.HANDLE
    api.CloseHandle.argtypes=[W.HANDLE]
    handle=api.CreateFileW(str(path),0x80000000,1,None,3,0,None)
    if handle==W.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:yield
    finally:api.CloseHandle(handle)


def launch_snapshot(root):
    from cachemonitor.install_activation import shortcuts,snapshot_registry
    return dict(registry=snapshot_registry(True),receipt=(root/'installation.json').read_bytes(),
                links={str(p):p.read_bytes() if p.exists() else None for p in shortcuts(True)})


def verify_cleanup(first,second,root):
    """Real GUI/collector handoff must precede deletion, including on QA installs."""
    import hashlib
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    from tools.verify_recovery import wait_report
    home=root/'cleanup home';home.mkdir();(home/'codexon-test-home').touch()
    index=root/'cleanup-data'/'index.sqlite'
    index.parent.mkdir()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    assert port!=8768
    (index.parent/'cache-route.json').write_text(json.dumps({'url':f'http://127.0.0.1:{port}'}))
    args=['--codex-home',str(home),'--index-path',str(index),'--evidence-path',str(index.parent/'model-evidence.sqlite'),
          '--cache-control','--verify-services','--hidden']
    processes=[]
    app=QCoreApplication.instance() or QCoreApplication([])
    try:
        for n,registration in enumerate((first,second)):
            report=root/f'cleanup-gui-{n}.json'
            command=[registration['AppPath'],*args,'--verify-handoff',str(report)]
            if n:command.append('--replace-gui')
            processes.append(subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW))
            wait_report(report,lambda d:d.get('ready'))
            if not n:
                from cachemonitor.usage_collection import CollectionChannel
                channel=CollectionChannel([str(home)],index,index.parent/'model-evidence.sqlite')
                try:
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        source=(channel.read() or {}).get('collection') or {}
                        if Path(source.get('executable',''))==Path(registration['AppPath']):break
                        time.sleep(.2)
                    else:raise RuntimeError('Previous QA collector did not become ready')
                finally:channel.close()
        assert processes[0].wait(timeout=30)==0
        journal=Path(second['InstallRoot'])/'cleanup.json'
        def cleaned(value):
            return (value.get('product')==str(Path(second['AppPath']).parent)
                    and any(i.get('status')=='removed' for i in value.get('items',[]))
                    and not Path(first['AppPath']).exists() and not Path(first['RecoveryPath']).exists())
        wait_report(journal,cleaned)
        assert Path(second['AppPath']).is_file() and Path(second['RecoveryPath']).is_file()
        (root/'cleanup-result.json').write_text(json.dumps(read_json(journal),indent=2))
    finally:
        client=QLocalSocket()
        client.connectToServer('CodexonQA-'+hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
        if client.waitForConnected(2000):
            client.write(b'verify-quit');client.waitForBytesWritten(2000);client.waitForReadyRead(3000)
        for process in processes:process.wait(timeout=45)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--broken-installer',type=Path)
    args=parser.parse_args()
    require_qa_installer(args.installer)
    if args.broken_installer:require_qa_installer(args.broken_installer)
    if registration():raise RuntimeError('An existing QA installation must be preserved; use a clean QA environment')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    # Keep installed Qt resource paths within Windows' installer path limit.
    # Evidence paths may be descriptive and much longer than the install root.
    install=Path(__file__).resolve().parents[1]/'artifacts'/('qa-'+uuid.uuid4().hex[:8])
    (root/'install-location.json').write_text(json.dumps({'root':str(install)}))
    if args.broken_installer:
        broken=install.with_name(install.name+'-f')
        assert run([args.broken_installer.resolve(),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',
                    f'/DIR={broken}',f'/LOG={root / "failed-first.log"}'])!=0
        assert not registration()
        assert read_json(broken/'install-result.json').get('error'), 'Runtime failure must reach activation'
        assert run([next(broken.glob('unins*.exe')),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'])==0
        assert not registration()
    command=[args.installer.resolve(),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',f'/DIR={install}']
    assert run([*command,f'/LOG={root / "install.log"}'])==0
    first=registration();assert Path(first['InstallRoot'])==install
    assert Path(first['AppPath']).is_file() and Path(first['RecoveryPath']).is_file()
    from cachemonitor.install_activation import shortcuts
    from cachemonitor.shell_shortcut import application_id
    recovery_link=next(p for p in shortcuts(True)[1:] if p.exists())
    assert application_id(recovery_link)=='Codexon-QA.Recovery'
    record=install/'preserved-record.txt';record.write_text('existing user record')
    before=launch_snapshot(install)
    if args.broken_installer:
        assert run([args.broken_installer.resolve(),*command[1:],f'/LOG={root / "failed-runtime.log"}'])!=0
        assert launch_snapshot(install)==before
    with prevent_receipt_replace(install/'installation.json'):
        assert run([*command,f'/LOG={root / "failed-receipt.log"}'])!=0
        assert launch_snapshot(install)==before
    assert not (install/'activation-pending.json').exists()
    assert run([*command,f'/LOG={root / "update.log"}'])==0
    second=registration()
    assert first['AppPath']!=second['AppPath'] and Path(first['AppPath']).is_file()
    assert read_json(install/'installation.json')['previous']==str(Path(first['AppPath']).parent)
    verify_cleanup(first,second,root)
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
    result=dict(passed=True,install=True,reinstall=True,old_payload_preserved_until_ready=True,old_payload_cleaned=True,
                busy_uninstall_deferred=True,uninstall=True,records_preserved=True,
                receipt_failure_restored=True,runtime_failure_restored=bool(args.broken_installer),
                first_failure_removable=bool(args.broken_installer))
    (root/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':main()
