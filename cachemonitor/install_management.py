"""Transactional per-user installation activation and non-destructive removal."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .observer_control import atomic_write
from .observer_state import ProcessLock, read_json
from .installation import KEY

STARTUP_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def startup_replacement(command, executable):
    match=re.fullmatch(r'\s*(?:"([^"]+)"|(\S+))(.*)',command,flags=re.DOTALL)
    if not match:return command
    old=match[1] or match[2]
    if Path(old).name.lower() not in ('cachemonitor.exe','codexon.exe'):return command
    return subprocess.list2cmdline([str(executable)])+match[3]


def migrate_startup(executable):
    """Retarget an enabled packaged app without changing its login preferences.

    Keep the value name (and Windows StartupApproved state) and arguments intact.
    A failed read-back must reach the activation transaction so it can roll back.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,STARTUP_KEY,
                             0,winreg.KEY_READ|winreg.KEY_SET_VALUE)
    except FileNotFoundError:return
    with key:
        try:command,kind=winreg.QueryValueEx(key,'CacheMonitor')
        except FileNotFoundError:return
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):return
        updated=startup_replacement(command,executable)
        if updated==command:return
        winreg.SetValueEx(key,'CacheMonitor',0,kind,updated)
        try:actual=winreg.QueryValueEx(key,'CacheMonitor')
        except FileNotFoundError:actual=None
        if actual!=(updated,kind):
            raise OSError('Windows 로그인 시 시작 경로를 갱신하지 못했습니다. 이전 설치 상태를 유지합니다.')


def contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('Installation component is outside its installation root')
    return path


def register(root, product, recovery, *, isolated=False):
    import winreg
    key_name = KEY+'-QA' if isolated else KEY
    values = dict(InstallRoot=str(root), AppPath=str(product/'Codexon.exe'), RecoveryPath=str(recovery))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_name) as key:
        previous={}
        for name in values:
            try:previous[name]=winreg.QueryValueEx(key,name)
            except FileNotFoundError:previous[name]=None
        try:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        except OSError:
            for name,value in previous.items():
                if value is None:
                    try:winreg.DeleteValue(key,name)
                    except FileNotFoundError:pass
                else:winreg.SetValueEx(key,name,0,value[1],value[0])
            raise


def activate_proxy(manager):
    status = manager.status()
    if not status.get('configured'):
        return dict(phase='off', message='프록시 사용 꺼짐')
    if getattr(manager,'shared_cache_worker',False):return manager.update_proxy()['update']
    manager.configure_check()
    health = status.get('health') or {}
    if health:
        from .version import PROXY_VERSION
        from .proxy_update import process_executable
        command=manager.supervisor_command(manager.state().get('upstream','chatgpt'))
        active=process_executable(manager.runtime()['pid'])
        if (health.get('version')!=PROXY_VERSION or health.get('lifecycle')!='managed'
                or Path(active).resolve()!=Path(command[0]).resolve()):
            return manager.update_proxy().get('update') or {}
        manager.task.configure(command,autostart=True)
        return dict(phase='complete', message='연결 구성요소 업데이트 완료')
    if status.get('probe_state')=='refused' and manager.state().get('enabled'):
        manager.attach_supervisor()
        return dict(phase='complete', message='연결 구성요소 시작 완료')
    return dict(phase='recovery_required', message='시작 메뉴의 Codexon 연결 복구를 실행해 주세요.')


def finish(root, product, recovery, *, isolated=False, launch=True, language='ko'):
    root = Path(root).resolve()
    product = contained(product, root/'versions')
    recovery = contained(recovery, root/'maintenance')
    manifest = read_json(product/'build-manifest.json')
    exe = product/'Codexon.exe'
    if manifest.get('product')!='Codexon' or hashlib.sha256(exe.read_bytes()).hexdigest()!=manifest.get('sha256'):
        raise RuntimeError('설치 파일 검증에 실패했습니다. 이전 버전을 유지합니다.')
    if hashlib.sha256(recovery.read_bytes()).hexdigest()!=manifest.get('recovery_sha256'):
        raise RuntimeError('복구 도구 검증에 실패했습니다. 이전 버전을 유지합니다.')
    with ProcessLock(root/'install.lock',timeout=10):
        report = root/('runtime-'+uuid.uuid4().hex+'.json')
        check = subprocess.run([str(exe),'--verify-runtime',str(report)],timeout=45,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        value = read_json(report)
        if check.returncode or value.get('errors')!=[] or value.get('version')!=manifest['version']:
            raise RuntimeError('새 버전 실행 검사에 실패했습니다. 이전 버전을 유지합니다.')
        from .install_activation import Activation, publish_shell
        activation = Activation(root, isolated)
        previous = read_json(root/'installation.json')
        receipt = dict(product=str(product), recovery=str(recovery), version=manifest['version'],
                       commit=manifest['commit'], previous=previous.get('product'), installed_at=time.time())
        # Keep the previous payload and receipt; only the launch pointers change.
        try:
            if previous:
                atomic_write(root/('installation-'+uuid.uuid4().hex+'.json'),json.dumps(previous).encode())
            if not isolated:
                from .launch_context import running_homes, save_homes
                homes = running_homes(root)
                if homes:save_homes(homes)
            register(root, product, recovery, isolated=isolated)
            publish_shell(product, recovery, isolated=isolated, language=language)
            if not isolated:migrate_startup(exe)
            atomic_write(root/'installation.json',json.dumps(receipt,indent=2).encode())
            activation.commit()
        except Exception:
            activation.rollback()
            raise
        if not isolated:
            from .observer_task import retire_desktop_startups
            try:retire_desktop_startups(root)
            except RuntimeError as exc:receipt['startup_warning']=str(exc)
            result_path = root/'connection-update.json'
            try:
                result = subprocess.run([str(exe),'--complete-install','--control-report',str(result_path)],
                                        timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
                receipt['connection'] = read_json(result_path)
                if result.returncode:raise RuntimeError('Connection update did not complete')
            except (OSError,RuntimeError,subprocess.TimeoutExpired):
                receipt['connection'] = dict(phase='recovery_required',message='연결 복구를 실행해 주세요.')
            if launch:
                from .observer_task import ObserverTask
                # A new task identity must be able to run while the old GUI task
                # is still alive; that new process performs the graceful handoff.
                try:ObserverTask(str(exe),role='Desktop').start([str(exe),'--replace-gui'])
                except RuntimeError:receipt['launch']='시작 메뉴에서 Codexon을 열어 주세요.'
        return receipt


def processes_under(root):
    import base64
    encoded = base64.b64encode(str(Path(root).resolve()).encode()).decode()
    script = r"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__ROOT__')).TrimEnd('\')+'\'
@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) } | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress
""".replace('__ROOT__',encoded)
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
                           base64.b64encode(script.encode('utf-16-le')).decode()],
                          capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('실행 중인 구성요소를 확인하지 못했습니다.')
    text=result.stdout.decode('utf-8-sig').strip()
    values=json.loads(text) if text else []
    return values if isinstance(values,list) else [values]


def connection_manager():
    from .launch_context import cache_paths,resolve_homes
    paths=cache_paths()
    if paths.get('index_path') and paths.get('evidence_path'):
        from .cache_worker_control import CacheWorkerManager
        return CacheWorkerManager(resolve_homes()[0],paths['index_path'],paths['evidence_path'])
    from .connection_recovery import target
    return target()


def prepare_uninstall(root, *, isolated=False):
    root=Path(root).resolve()
    with ProcessLock(root/'install.lock',timeout=5):
        from .installation import installed
        registration = installed() if not isolated else {}
        if not isolated and registration.get('InstallRoot') and Path(registration['InstallRoot']).resolve()==root:
            from .connection_recovery import restore
            from .launch_context import resolve_homes
            from .cache_hooks import remove_installation
            manager=connection_manager()
            for home in set([str(manager.home),*resolve_homes()]):remove_installation(home,root)
            restore(manager)
            # A newly drained worker exits cooperatively; busy streams remain
            # alive and the existing process check defers payload deletion.
            if getattr(manager,'shared_cache_worker',False):
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    health=manager.health(timeout=1)
                    if not health or health.get('active_connections'):break
                    time.sleep(.2)
            # Existing sockets are allowed to finish; a busy app/relay defers removal.
        for process in processes_under(root):
            name=Path(process['ExecutablePath']).name.lower()
            if name.startswith('unins'):continue
            if process['ProcessId'] in (os.getpid(),os.getppid()) and Path(process['ExecutablePath']).resolve()==Path(sys.executable).resolve():continue
            raise RuntimeError('Codexon을 종료하고 진행 중인 연결이 끝난 뒤 제거를 다시 실행하세요. 연결 설정과 기록은 보존됩니다.')
        if not isolated:
            import winreg
            from .observer_task import ObserverTask
            for exe in (root/'versions').glob('*/Codexon/Codexon.exe'):
                ObserverTask(str(exe),role='Desktop').remove()
            ObserverTask(str(root),role='Installer').remove()
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run',0,
                                    winreg.KEY_READ|winreg.KEY_SET_VALUE) as key:
                    command=winreg.QueryValueEx(key,'CacheMonitor')[0]
                    match=re.match(r'\s*(?:"([^"]+)"|(\S+))',command)
                    if match and Path(match[1] or match[2]).resolve().is_relative_to(root):
                        winreg.DeleteValue(key,'CacheMonitor')
            except FileNotFoundError:pass
        from .install_activation import remove_shortcuts
        remove_shortcuts(isolated)
        return dict(ready=True,records_preserved=True)


def complete_main():
    import argparse
    from .connection_recovery import target
    parser=argparse.ArgumentParser()
    parser.add_argument('--control-report',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=activate_proxy(connection_manager())
    except Exception as exc:result=dict(phase='recovery_required',error=str(exc))
    atomic_write(args.control_report,json.dumps(result,ensure_ascii=False).encode())
    return 1 if result.get('error') else 0
