"""Offline recovery and bounded scheduled checks. No Qt, login or model calls."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from .model_evidence import default_path, home_key
from .observer_control import ObserverManager, URL, atomic_write
from .observer_state import ProcessLock, read_json
from .translation_catalog import translate as tr


def target(home=None, directory=None, url=None):
    directory = Path(directory) if directory else default_path().parent
    state = read_json(directory / 'model-observer.json')
    home = Path(home or state.get('home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    url = url or (state.get('url') if state.get('home') == home_key(home) else None) or URL
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('Codexon 로컬 프록시 주소가 아닙니다. 설정을 변경하지 않았습니다.')
    return ObserverManager(home, directory, url)


def inspect(manager):
    try:
        status = manager.status()
    except (OSError, ValueError, RuntimeError) as exc:
        return dict(code='unreadable', confirmed=False, can_recover=True,
                    title='연결 설정을 확인하지 못했습니다',
                    detail='설정을 읽을 수 없습니다. 기존 파일은 유지됩니다. 복구를 시도하면 결과를 확인할 수 있습니다.',
                    error=str(exc))
    return assess(status)


def assess(status):
    """Use the same interpretation in the GUI, scheduled checks and recovery window."""
    if not status['configured']:
        restart = status.get('restart_required', False)
        return dict(code='direct', confirmed=False, can_recover=True, status=status,
                    title='Codexon 프록시 연결이 해제되어 있습니다',
                    detail='Codex가 이전 연결을 사용 중이라면 작업을 마친 뒤 완전히 종료하고 다시 여세요.' if restart
                    else '다른 연결·로그인·서버 문제는 이 도구의 확인 범위에 포함되지 않습니다.')
    update = status.get('update') or {}
    from .proxy_update import BUSY
    if update.get('phase') in BUSY:
        return dict(code='updating', confirmed=False, can_recover=True, status=status,
                    title='연결 구성요소를 전환하고 있습니다', detail=update.get('message') or '교체 상태를 확인하고 있습니다.')
    probe = status.get('probe_state')
    health = status.get('health') or {}
    if probe in ('refused', 'identity_mismatch'):
        return dict(code=probe, confirmed=True, can_recover=True, status=status,
                    title='Codexon 프록시 연결을 사용할 수 없습니다',
                    detail='직접 연결로 복원한 뒤 Codex를 다시 열어 주세요.')
    if health.get('internal_failure_streak', 0) >= 3:
        return dict(code='relay_failure', confirmed=True, can_recover=True, status=status,
                    title='프록시 내부 오류가 반복되었습니다',
                    detail='직접 연결로 복원할 수 있습니다. 이미 실패한 요청은 자동으로 재전송하지 않습니다.')
    if health.get('storage_failure_streak', 0) >= 3 or health.get('observation_enabled') is False:
        return dict(code='observation_failure', confirmed=True, can_recover=True, status=status,
                    title='모델 관측 기록을 저장하지 못했습니다',
                    detail='통신 장애로 단정하지 않습니다. 관측을 중지하고 직접 연결로 복원할 수 있습니다.')
    if probe == 'healthy':
        return dict(code='responding', confirmed=False, can_recover=True, status=status,
                    title='로컬 프록시가 응답합니다',
                    detail='실제 AI 요청 성공 여부는 확인하지 않았습니다. Codex 통신이 안 되면 직접 연결로 복원할 수 있습니다.')
    return dict(code='unknown', confirmed=False, can_recover=True, status=status,
                title='프록시 응답을 확인하지 못했습니다',
                detail='지연만으로 장애를 확정하지 않습니다. 필요하면 직접 연결로 복원할 수 있습니다.')


def restore(manager):
    with ProcessLock(manager.control_lock, timeout=5):
        status=manager.recover_direct()
        if manager.config()[1].get('openai_base_url') == manager.url:
            raise RuntimeError('프록시 연결 설정이 남아 있습니다. 복원이 완료되지 않았습니다.')
    detail='진행 중인 작업을 확인한 뒤 Codex를 완전히 종료하고 다시 여세요. 통신 성공 여부는 재시작 후 확인됩니다.'
    if status.get('cleanup_warning'):
        detail+=' 예약 작업 해제가 완료되지 않았습니다. 이 도구에서 복원을 다시 실행해 주세요.'
    return dict(code='restored', title='연결 설정 복원 완료',detail=detail)


def notification_uri(manager):
    data = json.dumps(dict(home=str(manager.home), directory=str(manager.directory), url=manager.url)).encode()
    return 'codexon-recovery:' + base64.urlsafe_b64encode(data).decode()


def notify(manager, result):
    if sys.platform == 'darwin':
        from .macos_notifications import notify_recovery
        return notify_recovery(manager, result)
    if os.name != 'nt':
        return False
    from .installation import installed
    if not installed():
        return False  # Portable users retain the standalone manual tool.
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\CacheMonitor\CacheMonitor\notifications') as key:
            for name in ('enabled','proxy_failure'):
                try:value=winreg.QueryValueEx(key,name)[0]
                except FileNotFoundError:continue
                if str(value).lower() in ('false','0'):return False
    except FileNotFoundError:pass
    xml = ('<toast activationType="protocol" launch="'+escape(notification_uri(manager), {'"':'&quot;'})+'">'
           '<visual><binding template="ToastGeneric"><text>'+escape(tr('Codexon 연결 확인'))+'</text><text>'
           +escape(tr(result['title']))+'</text><text>'+escape(tr('눌러서 연결 복구 열기'))+'</text></binding></visual></toast>')
    encoded = base64.b64encode(xml.encode()).decode()
    script = """
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null
$xml=New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__XML__')))
$toast=[Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag='connection'
$toast.Group='Codexon'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Codexon.Recovery').Show($toast)
""".replace('__XML__', encoded)
    code = base64.b64encode(script.encode('utf-16-le')).decode()
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', code],
                            capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode == 0


def check_once(manager, *, notification=notify, now=None):
    now = time.time() if now is None else now
    # A second scheduled invocation exits; it never competes for configuration locks.
    try:
        with ProcessLock(manager.directory/'connection-check.lock'):
            result = inspect(manager)
            path = manager.directory/'connection-check.json'
            prior = read_json(path)
            same = prior.get('code') == result['code'] and 5 <= now-prior.get('at', 0) <= 180
            count = prior.get('count', 0)+1 if same else 1
            notified = prior.get('notified_code') == result['code']
            # Two separate confirmed observations; unknowns never produce alerts.
            if result['confirmed'] and count >= 2 and not notified:
                notified = notification(manager, result)
            notified_code=result['code'] if notified else prior.get('notified_code')
            if result['code'] in ('direct','responding'):notified_code=None
            atomic_write(path, json.dumps(dict(code=result['code'], at=now, count=count,
                                               notified=notified,notified_code=notified_code,
                                               title=result['title']), ensure_ascii=False).encode())
            return result
    except RuntimeError:
        return dict(code='busy', confirmed=False)


class RecoveryWindow:
    def __init__(self, manager):
        import tkinter as tk
        from tkinter import ttk
        self.manager = manager
        self.root = tk.Tk()
        self.root.title(tr('Codexon 연결 복구'))
        self.root.geometry('580x310')
        self.root.minsize(480, 280)
        self.root.configure(bg='#f5f6f8')
        self.messages = queue.Queue()
        self.busy = False
        self.result = None
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():style.theme_use('vista')
        style.configure('TButton', font=('맑은 고딕', 11), padding=(12, 8))
        frame = tk.Frame(self.root, bg='#f5f6f8', padx=24, pady=24)
        frame.pack(fill='both', expand=True)
        self.title = tk.Label(frame, text=tr('연결 상태 확인 중…'), bg='#f5f6f8', fg='#18212c',
                              font=('맑은 고딕', 16, 'bold'), anchor='w', justify='left', wraplength=510)
        self.title.pack(fill='x')
        self.detail = tk.Label(frame, text=tr('인터넷이나 Codexon 본체 없이 복구할 수 있습니다.'), bg='#f5f6f8',
                               fg='#475365', font=('맑은 고딕', 11), anchor='nw', justify='left', wraplength=510)
        self.detail.pack(fill='both', expand=True, pady=(14, 16))
        self.button = ttk.Button(frame, text=tr('직접 연결로 복원'), command=self.recover)
        self.button.pack(anchor='w')
        self.root.bind('<Configure>', self.resize)
        self.root.after(100, self.poll)
        self.submit(inspect)

    def resize(self, event):
        if event.widget == self.root:
            self.title.configure(wraplength=max(350, event.width-48))
            self.detail.configure(wraplength=max(350, event.width-48))

    def submit(self, function):
        self.busy = True
        self.button.state(['disabled'])
        def work():
            try:result = function(self.manager)
            except Exception as exc:
                result = dict(code='error', title='설정을 복원하지 못했습니다',
                              detail='설정 파일을 읽거나 변경할 수 없습니다. 기존 설정과 백업은 보존됩니다. 파일 접근 권한을 확인한 뒤 다시 시도하세요.')
                if isinstance(exc,RuntimeError) and '다른 프록시 설정 작업' in str(exc):
                    result['detail']='다른 연결 설정 작업이 진행 중입니다. 잠시 기다린 뒤 복원을 다시 실행해 주세요.'
            self.messages.put(result)
        # Keep an already-started recovery transaction alive if the window closes.
        threading.Thread(target=work, daemon=False).start()

    def recover(self):
        if not self.busy:
            self.title.configure(text=tr('직접 연결로 복원 중…'))
            self.submit(restore)

    def poll(self):
        try:
            self.result = self.messages.get_nowait()
            self.busy = False
            self.title.configure(text=tr(self.result['title']))
            self.detail.configure(text=tr(self.result['detail']))
            self.button.state(['!disabled'])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Codexon offline connection recovery')
    parser.add_argument('uri', nargs='?')
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--proxy-url')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--install-root', type=Path)
    parser.add_argument('--product-dir', type=Path)
    parser.add_argument('--prepare-uninstall', action='store_true')
    parser.add_argument('--isolated-install', action='store_true')
    parser.add_argument('--no-launch', action='store_true')
    parser.add_argument('--language', choices=('en','ko'))
    parser.add_argument('--ui-smoke', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.language:os.environ['CODEXON_LANGUAGE']=args.language
    if args.install_root:
        from .install_management import finish, prepare_uninstall
        try:
            if args.prepare_uninstall:
                result=prepare_uninstall(args.install_root,isolated=args.isolated_install)
            else:
                result=finish(args.install_root,args.product_dir,Path(sys.executable),
                              isolated=args.isolated_install,launch=not args.no_launch,
                              language=args.language or 'ko')
        except Exception as exc:result=dict(error=str(exc))
        if args.report:atomic_write(args.report,json.dumps(result,ensure_ascii=False,indent=2).encode())
        return 1 if result.get('error') else 0
    if args.uri:
        if args.uri.split(':',1)[0] not in ('codexon-recovery','codexon-recovery-qa') or len(args.uri)>16384:
            parser.error('Unknown recovery link')
        try:
            value = json.loads(base64.urlsafe_b64decode(args.uri.split(':',1)[1]).decode())
            args.codex_home, args.data_dir, args.proxy_url = value['home'], value['directory'], value['url']
        except (ValueError, KeyError, TypeError):parser.error('Invalid recovery link')
    try:
        manager = target(args.codex_home, args.data_dir, args.proxy_url)
        if args.check:result = check_once(manager)
        elif args.restore:result = restore(manager)
        elif args.status:result = inspect(manager)
        else:
            if args.ui_smoke and (not args.codex_home or not args.data_dir or manager.url==URL
                    or not (manager.home/'codexon-test-home').exists()):
                raise ValueError('UI checks require an explicitly isolated test home and port')
            if sys.platform == 'darwin':
                from .macos_recovery import RecoveryWindow as MacRecoveryWindow
                window=MacRecoveryWindow(manager,smoke=args.ui_smoke)
            else:window=RecoveryWindow(manager)
            if args.ui_smoke and sys.platform != 'darwin':
                window.smoke_activations=0
                window.smoke_reports=0
                def callback_error(kind,value,trace):
                    import traceback
                    args.ui_smoke.with_suffix('.error.txt').write_text(
                        ''.join(traceback.format_exception(kind,value,trace)),encoding='utf-8')
                window.root.report_callback_exception=callback_error
                def report_ui():
                    window.root.after(250,report_ui)
                    window.smoke_reports+=1
                    action=args.ui_smoke.with_suffix('.click')
                    button=window.button
                    ready=bool(button.winfo_ismapped() and button.instate(['!disabled']))
                    if action.exists() and ready:
                        action.unlink()
                        # Use Tk's normal button activation binding. A synthetic
                        # pointer press can lose its pressed state when Windows
                        # delivers a native Leave event on an unshown desktop.
                        button.event_generate('<<Invoke>>')
                        window.smoke_activations+=1
                    atomic_write(args.ui_smoke,json.dumps(dict(busy=window.busy,result=window.result,
                        hwnd=window.root.winfo_id(),button=window.button.winfo_id(),
                        width=button.winfo_width(),height=button.winfo_height(),
                        ready=ready,activations=window.smoke_activations,reports=window.smoke_reports)).encode())
                window.root.after(250,report_ui)
            window.run()
            return 0
    except Exception as exc:
        result = dict(code='error', error=str(exc))
    if args.report:
        atomic_write(args.report, json.dumps(result, ensure_ascii=False, indent=2).encode())
    return 1 if result['code']=='error' else 0
