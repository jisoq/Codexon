import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import sys
import time

from . import chatgpt_startup as desktop
from .observer_state import ProcessLock

SAVE_WORK = '진행 중인 작업을 마치고 저장하세요. 업데이트하려면 Codex를 종료한 뒤 다시 실행해야 합니다.'
CLOSE_REQUIRED = 'Codex를 완전히 종료한 뒤 다시 시도하세요.'
RESTART_REQUIRED = 'Codex를 다시 실행하지 못했습니다. 다시 시도하세요.'
CONSENT_REQUIRED = '업데이트를 다시 시작하고 Codex 재시작을 확인하세요.'
TERMINAL = ('restarted', 'not_running', 'not_applicable')


def capture():
    targets = desktop.desktop_processes()
    return dict(phase='authorized', targets=targets, reopen_required=bool(targets))


def needs_reopening(record):
    client=record.get('client',{})
    if not (client.get('ready_at') or client.get('closed_at')) or client.get('phase') not in ('blocked','reopening'):
        return False
    current=desktop.desktop_processes()
    if not current:return True
    ready_at=client.get('ready_at')
    proxy=record.get('proxy',{})
    return bool(ready_at and (proxy.get('phase') in ('complete','off') or proxy.get('restored'))
                and installed_gui_ready(record) and (not record.get('scope') or restored_ready(record))
                and any(p['created']/10_000_000-11644473600>=ready_at for p in current))


def confirm_native(language):
    from .translation_catalog import translate
    api = C.WinDLL('user32', use_last_error=True)
    api.MessageBoxW.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT]
    MB_OKCANCEL, MB_ICONWARNING, MB_DEFBUTTON2, MB_SETFOREGROUND = 1, 0x30, 0x100, 0x10000
    return api.MessageBoxW(None, translate(SAVE_WORK, language), 'Codexon',
                          MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND) == 1


def close_desktop(targets, timeout=25):
    deadline = time.monotonic() + timeout
    cooperative_deadline = deadline - timeout / 5
    from .proxy_identity import process_identity
    kernel = desktop._process_api()
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    kernel.TerminateProcess.restype = W.BOOL
    user = C.WinDLL('user32', use_last_error=True)
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user.IsWindowVisible.argtypes = [W.HWND]
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    handles = {}
    try:
        session = W.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), C.byref(session)):raise OSError(CLOSE_REQUIRED)
        for target in targets:
            if target['session'] != session.value:raise RuntimeError(CONSENT_REQUIRED)
            handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x0001, False, target['pid'])
            if not handle:
                if process_identity(target['pid']) is None:continue
                raise OSError(CLOSE_REQUIRED)
            handles[target['pid']] = handle
            actual = process_identity(target['pid'])
            if actual != {k:target[k] for k in ('pid', 'created', 'executable')}:
                kernel.CloseHandle(handles.pop(target['pid']))
                continue
        visible = set()
        @callback_type
        def visible_window(hwnd, _):
            pid = W.DWORD()
            user.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value in handles and user.IsWindowVisible(hwnd):visible.add(hwnd)
            return True
        if not user.EnumWindows(visible_window, 0):raise OSError(CLOSE_REQUIRED)
        for hwnd in visible:user.PostMessageW(hwnd, 0x10, 0, 0)
        force_requested = False
        while True:
            pending = []
            for handle in handles.values():
                status = kernel.WaitForSingleObject(handle, 0)
                if status == 258:pending.append(handle)
                elif status != 0:raise OSError(CLOSE_REQUIRED)
            if not pending:return
            now = time.monotonic()
            if now >= deadline:raise RuntimeError(CLOSE_REQUIRED)
            if not force_requested and now >= cooperative_deadline:
                visible.clear()
                if not user.EnumWindows(visible_window, 0):raise OSError(CLOSE_REQUIRED)
                if not visible:
                    if time.monotonic() >= deadline:raise RuntimeError(CLOSE_REQUIRED)
                    for handle in pending:
                        status = kernel.WaitForSingleObject(handle, 0)
                        if status == 0:continue
                        if status != 258:raise OSError(CLOSE_REQUIRED)
                        if not kernel.TerminateProcess(handle, 1):
                            if kernel.WaitForSingleObject(handle, 0) != 0:raise OSError(CLOSE_REQUIRED)
                    force_requested = True
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    finally:
        for handle in handles.values():kernel.CloseHandle(handle)


def recovery_path(install=None):
    from .installation import installed
    from .proxy_identity import digest
    from .observer_state import read_json
    install = install or installed()
    path = Path(install['RecoveryPath']).resolve()
    manifest = read_json(Path(install['AppPath']).parent / 'build-manifest.json')
    if digest(path) != manifest.get('recovery_sha256'):raise RuntimeError('복구 도구 검증에 실패했습니다.')
    return path


def start(journal, operation_id, recovery):
    from .observer_task import ObserverTask
    client = journal.read().get('client', {})
    if client.get('phase') in TERMINAL:return
    command = [str(recovery), '--restart-client', '--install-root', str(journal.root), '--update-id', operation_id]
    if not getattr(sys, 'frozen', False):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'recovery_main.py'), *command[1:]]
    task = ObserverTask(str(journal.root) + ':' + operation_id, role='ClientRestart')
    if not task.inspect().get('running'):
        journal.client_result(operation_id, recovery=str(recovery))
        task.start(command)


def require_stopped(journal, operation_id):
    record = journal.read()
    if record.get('operation_id') != operation_id:raise RuntimeError(CONSENT_REQUIRED)
    client = record.get('client', {})
    closed_retry=bool(client.get('closed_at')) and client.get('phase') in ('blocked','reopening')
    if client.get('phase') not in ('closed', 'not_running', 'not_applicable') and not closed_retry:
        raise RuntimeError(client.get('message') or CONSENT_REQUIRED)
    if client['phase'] != 'not_applicable' and desktop.desktop_processes():
        journal.client_result(operation_id, phase='blocked', message=CONSENT_REQUIRED)
        raise RuntimeError(CONSENT_REQUIRED)


def prepare_restart(journal, operation_id, recovery):
    client = journal.read().get('client', {})
    if client.get('phase') in ('not_running', 'not_applicable', 'closed'):
        require_stopped(journal, operation_id)
        return
    if client.get('phase') != 'authorized':raise RuntimeError(client.get('message') or CONSENT_REQUIRED)
    start(journal, operation_id, recovery)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        record = journal.read(); client = record.get('client', {})
        if client.get('phase') in ('closed', 'not_running'):
            require_stopped(journal, operation_id)
            return
        if client.get('phase') == 'blocked':raise RuntimeError(client['message'])
        if record.get('cancel_requested'):
            from .app_update import UpdateCancelled
            raise UpdateCancelled()
        time.sleep(.1)
    raise RuntimeError('Codex 종료 확인을 완료하지 못했습니다. 다시 시도하세요.')


def restored_ready(record):
    from .connection_recovery import target
    from .app_services import route_readiness
    scope=record.get('scope')
    if not scope:return False
    manager=target(Path(scope['home']),Path(scope['evidence']).parent,scope['url'])
    manager.evidence=Path(scope['evidence'])
    return route_readiness(manager,record)['phase'] in ('complete','off')


def installed_gui_ready(record):
    if record.get('kind')!='app' or record.get('app',{}).get('state')!='verified':return True
    app=record['app'];startup=app.get('startup') or {}
    if startup.get('state')=='skipped' and startup.get('policy')=='no_launch':return True
    if startup.get('state')!='ready':return False
    from .proxy_identity import same_process,digest
    owner=startup.get('owner') or {}
    return (owner.get('executable')==app.get('executable') and same_process(owner)
            and digest(app['executable'])==app.get('sha256'))


def run_restart(journal, operation_id):
    from .proxy_identity import process_identity, same_process
    from .update_state import installed_gui_required
    with ProcessLock(journal.root / 'client-restart.lock'):
        record = journal.read()
        if record.get('operation_id') != operation_id:return
        client = record.get('client', {})
        phase = client.get('phase')
        if phase in TERMINAL:return
        try:
            targets = client.get('targets', [])
            if not isinstance(targets, list) or any(
                    not isinstance(t, dict) or t.get('app_id') != desktop.CODEX_APP_ID
                    or type(t.get('pid')) is not int or type(t.get('created')) is not int
                    or type(t.get('session')) is not int or not isinstance(t.get('executable'), str)
                    for t in targets):raise RuntimeError(CONSENT_REQUIRED)
            journal.client_result(operation_id, owner=process_identity(os.getpid()))
            if phase == 'authorized':
                current = desktop.desktop_processes()
                if current and not any(t in targets for t in current):raise RuntimeError(CONSENT_REQUIRED)
                if record.get('cancel_requested'):return
                if not client['reopen_required']:
                    journal.client_result(operation_id, phase='not_running')
                    return
                journal.client_result(operation_id, phase='closing',targets=current)
                close_desktop(current)
                if desktop.desktop_processes():raise RuntimeError(CLOSE_REQUIRED)
                journal.client_result(operation_id, phase='closed',closed_at=time.time())
            elif phase == 'closing':
                if desktop.desktop_processes():raise RuntimeError(CONSENT_REQUIRED)
                journal.client_result(operation_id, phase='closed',closed_at=time.time())
            elif phase not in ('closed', 'reopening'):
                raise RuntimeError(client.get('message') or CONSENT_REQUIRED)
            while True:
                record = journal.read()
                if record.get('operation_id') != operation_id:return
                client = record['client']; proxy = record.get('proxy', {})
                gui_ready=installed_gui_ready(record)
                ready = (record.get('app', {}).get('state') in ('verified', 'current')
                         and proxy.get('phase') in ('complete', 'off') and gui_ready
                         and (not record.get('scope') or restored_ready(record)))
                failed = record.get('component_phase', record['phase']) in ('failed', 'partial', 'cancelled', 'deferred')
                cancelled = record.get('cancel_requested') and not record.get('activation_started') and not proxy.get('cutover_started')
                if ready or (failed or cancelled) and gui_ready and restored_ready(record):
                    break
                startup=record.get('app',{}).get('startup') or {}
                pending_gui=installed_gui_required(record) and startup.get('state') in ('waiting','starting')
                if pending_gui and startup.get('owner') and same_process(startup['owner']) is False:
                    journal.startup_result(operation_id,dict(state='blocked',message='Codexon 시작이 중단되었습니다. 업데이트를 다시 시도하세요.'))
                    raise RuntimeError('연결 복구 후 Codex를 다시 실행하세요.')
                if failed and not pending_gui or startup.get('state')=='ready' and not gui_ready or startup.get('state')=='blocked' or (installed_gui_required(record) and not startup):
                    raise RuntimeError('연결 복구 후 Codex를 다시 실행하세요.')
                time.sleep(.5)
            ready_at = client.get('ready_at') or time.time()
            journal.client_result(operation_id, phase='reopening', ready_at=ready_at)
            current = desktop.desktop_processes()
            fresh = [p for p in current if p['created'] / 10_000_000 - 11644473600 >= ready_at]
            if current and not fresh:raise RuntimeError(CONSENT_REQUIRED)
            if not fresh:
                latest=journal.read()
                if latest.get('operation_id')!=operation_id or not installed_gui_ready(latest) or latest.get('scope') and not restored_ready(latest):
                    raise RuntimeError('Codexon과 연결 준비 상태가 변경되어 Codex 재시작을 보류했습니다.')
                desktop._activate(desktop.CODEX_APP_ID)
            deadline = time.monotonic() + 25
            while not fresh and time.monotonic() < deadline:
                fresh = [p for p in desktop.desktop_processes()
                         if p['created'] / 10_000_000 - 11644473600 >= ready_at]
                if not fresh:time.sleep(.1)
            if not fresh:raise RuntimeError(RESTART_REQUIRED)
            journal.client_result(operation_id, phase='restarted', result=fresh, message='')
            if cancelled:journal.change(operation_id, phase='cancelled', cancel_requested=False)
        except Exception as exc:
            journal.client_result(operation_id, phase='blocked', message=str(exc))


def retry(journal, operation_id, recovery):
    if not needs_reopening(journal.read()):raise RuntimeError(CONSENT_REQUIRED)
    journal.client_result(operation_id, phase='reopening', message='')
    start(journal, operation_id, recovery)
