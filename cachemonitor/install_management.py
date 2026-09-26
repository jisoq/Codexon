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
            for name, value in values.items():
                if winreg.QueryValueEx(key, name) != (value, winreg.REG_SZ):
                    raise OSError('Installation registry read-back did not match')
        except OSError:
            for name,value in previous.items():
                if value is None:
                    try:winreg.DeleteValue(key,name)
                    except FileNotFoundError:pass
                else:winreg.SetValueEx(key,name,0,value[1],value[0])
            raise


def activate_proxy(manager):
    manager.cleanup_legacy_check()
    manager.adopt_registrations()
    status = manager.status()
    if not status.get('configured'):
        return dict(phase='off', message='프록시 사용 꺼짐')
    if getattr(manager,'shared_cache_worker',False):
        if not status.get('health'):
            manager.resume()
        return manager.update_proxy().get('update') or {}
    health = status.get('health') or {}
    if health:
        return manager.update_proxy().get('update') or {}
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
                       commit=manifest['commit'], previous=previous.get('product'), installed_at=time.time(),
                       isolated=isolated)
        # Keep rollback payloads until the new GUI verifies all service handoffs.
        try:
            if previous:
                atomic_write(root/('installation-'+uuid.uuid4().hex+'.json'),json.dumps(previous).encode())
            if not isolated:
                from .launch_context import running_homes, save_homes, resolve_homes
                homes = running_homes(root)
                if homes:save_homes(homes)
                receipt['homes']=resolve_homes(homes)
                from .cache_hooks import migrate_installation
                hook_homes=set(receipt['homes'])
                for record in [previous, *[read_json(p) for p in root.glob('installation-*.json')]]:
                    hook_homes.update(h for h in record.get('homes',[]) if isinstance(h,str) and Path(h).is_absolute())
                for home in hook_homes:
                    migrate_installation(home,root,product/'CodexonHook.exe',before_write=activation.track_file)
            register(root, product, recovery, isolated=isolated)
            publish_shell(product, recovery, isolated=isolated, language=language)
            if not isolated:migrate_startup(exe)
            atomic_write(root/'installation.json',json.dumps(receipt,indent=2).encode())
            if not isolated:
                from .installation import pointer_path
                pointer=dict(InstallRoot=str(root),AppPath=str(exe),RecoveryPath=str(recovery))
                atomic_write(pointer_path(),json.dumps(pointer).encode())
                if read_json(pointer_path())!=pointer:raise OSError('Installation pointer verification failed')
            if read_json(root/'installation.json')!=receipt:raise OSError('Installation receipt verification failed')
            activation.commit()
        except Exception:
            activation.rollback()
            raise
        from .install_cleanup import queue
        try:queue(root)
        except (OSError,ValueError,RuntimeError) as exc:receipt['cleanup_warning']=str(exc)
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
                from .app_services import suspended
                if suspended(connection_manager()):
                    launch=False
                    receipt['launch']='앱 종료 요청을 유지했습니다.'
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
$ErrorActionPreference='Stop'
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
    from .connection_recovery import target
    return target()


def prepare_uninstall(root, *, isolated=False, caller_pid=None):
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
        collectors=[]
        for process in processes_under(root):
            name=Path(process['ExecutablePath']).name.lower()
            if name.startswith('unins'):continue
            if process['ProcessId'] in (os.getpid(),os.getppid()) and Path(process['ExecutablePath']).resolve()==Path(sys.executable).resolve():continue
            if process['ProcessId']==caller_pid and Path(process['ExecutablePath']).resolve()==Path(sys.executable).resolve():
                from .launch_context import command_arguments
                from .proxy_target import option
                arguments=command_arguments(process.get('CommandLine') or '')
                if '--prepare-uninstall' in arguments and Path(option(arguments,'--install-root','')).resolve()==root:continue
            if '--usage-collector' in (process.get('CommandLine') or ''):
                collectors.append(process);continue
            raise RuntimeError('Codexon을 종료하고 진행 중인 연결이 끝난 뒤 제거를 다시 실행하세요. 연결 설정과 기록은 보존됩니다.')
        if collectors:
            from .observer_task import ObserverTask
            from .launch_context import command_arguments
            for process in collectors:
                args=command_arguments(process['CommandLine'])
                if '--index-path' not in args:raise RuntimeError('수집기 경로를 확인할 수 없습니다')
                index=args[args.index('--index-path')+1]
                task=ObserverTask(str(Path(index).resolve()),role='UsageCollector')
                task.stop();task.remove()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if not any('--usage-collector' in (p.get('CommandLine') or '') for p in processes_under(root)):break
                time.sleep(.1)
            else:raise RuntimeError('백그라운드 수집기 종료를 기다린 뒤 제거를 다시 실행하세요')
        from .observer_task import remove_installation_collectors
        remove_installation_collectors(root)
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
        if not isolated:
            from .installation import pointer_path
            path=pointer_path();value=read_json(path)
            if value.get('InstallRoot')==str(root):path.unlink(missing_ok=True)
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
