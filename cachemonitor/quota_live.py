"""Read-only account RPC. No model calls, login changes, or reset redemption."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from .quota import clean_limits
from .banked_resets import normalize_reset_credits


def locate_codex():
    candidates = []
    direct = shutil.which('codex.exe')
    if direct:
        candidates.append(Path(direct))
    root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'OpenAI' / 'Codex' / 'bin'
    candidates.extend(root.glob('*/codex.exe'))
    npm = Path(os.environ.get('APPDATA', Path.home())) / 'npm' / 'node_modules' / '@openai'
    candidates.extend(npm.glob('codex*/**/codex.exe'))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        raise RuntimeError('Codex 실행 파일을 찾을 수 없습니다. 로컬 기록으로 확인합니다.')
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def normalize_limits(result, observed, account):
    buckets = result.get('rateLimitsByLimitId')
    raw = buckets.get('codex') if isinstance(buckets, dict) else result.get('rateLimits')
    if not raw or raw.get('limitId') not in (None, 'codex'):
        raise ValueError('Codex 공용 한도 정보가 없습니다')
    converted = {'limit_id': 'codex', 'plan_type': raw.get('planType')}
    for name in ('primary', 'secondary'):
        window = raw.get(name)
        converted[name] = {'used_percent': window.get('usedPercent'),
                           'window_minutes': window.get('windowDurationMins'),
                           'resets_at': window.get('resetsAt')} if isinstance(window, dict) else None
    clean = clean_limits(converted)
    if clean is None:
        raise ValueError('사용 가능한 한도 창이 없습니다')
    separate = sorted({b['normalModelSlug'] for key, b in (buckets or {}).items()
                       if key != 'codex' and isinstance(b, dict) and b.get('normalModelSlug')})
    return {**clean, 'observed_at': observed, 'source': 'live', 'max_age': 90,
            'account': account, 'bucket': 'codex', 'separate_models': separate,
            'reset_credits':normalize_reset_credits(result.get('rateLimitResetCredits'))}


class AccountClient:
    ALLOWED = {'initialize', 'account/read', 'account/rateLimits/read'}
    _quota_cache = {}
    _quota_lock = threading.Lock()

    def __init__(self, home):
        self.home = str(home)
        self.process = None
        self.sequence = 0
        self.messages = queue.Queue()
        self.after = 0
        self.cache_seconds = 30

    def start(self):
        env = os.environ.copy()
        env['CODEX_HOME'] = self.home
        self.process = subprocess.Popen([locate_codex(), 'app-server'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        messages = self.messages = queue.Queue()
        stream = self.process.stdout
        def read():
            try:
                for line in stream:
                    try:
                        value = json.loads(line)
                        if 'id' in value:
                            messages.put(value)
                    except (ValueError, TypeError):
                        continue
            finally:
                messages.put({'disconnected': True})
        threading.Thread(target=read, daemon=True).start()
        self.rpc('initialize', {'clientInfo': {'name': 'cachemonitor', 'version': '1.0'}})
        self.process.stdin.write('{"method":"initialized"}\n')
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        if method not in self.ALLOWED:
            raise ValueError('Read-only RPC only')
        self.sequence += 1
        message = {'id': self.sequence, 'method': method}
        if params is not None:
            message['params'] = params
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()
        end = time.monotonic() + 12
        while time.monotonic() < end:
            try:
                value = self.messages.get(timeout=max(.01, end - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('한도 조회 시간 초과') from None
            if value.get('disconnected'):
                raise ConnectionError('Codex 한도 조회 연결이 종료됐습니다')
            if value.get('id') == self.sequence:
                if 'error' in value:
                    # Never store arbitrary server error payloads or auth data.
                    raise RuntimeError(f'Codex 한도 조회 실패 ({value["error"].get("code", "unknown")})')
                return value.get('result') or {}
        raise TimeoutError('한도 조회 시간 초과')

    def fetch(self):
        try:
            if self.process is None or self.process.poll() is not None:
                self.start()
            account = self.rpc('account/read', {'refreshToken': False}).get('account') or {}
            identity = account.get('email') or account.get('id')
            if not identity:
                raise RuntimeError('로그인된 Codex 계정 확인 필요')
            key = hashlib.sha256(str(identity).encode()).hexdigest()
            with self._quota_lock:
                cached = self._quota_cache.get(key)
                if cached and time.monotonic()-cached[0]<self.cache_seconds and cached[1]['requested_at']>=self.after:
                    return dict(cached[1])
                requested_at = time.time()
                requested_monotonic = time.monotonic()
                result = self.rpc('account/rateLimits/read')
                received_at = time.time()
                elapsed = time.monotonic() - requested_monotonic
                after = self.rpc('account/read', {'refreshToken': False}).get('account') or {}
                if (after.get('email') or after.get('id')) != identity:
                    raise RuntimeError('한도 조회 중 계정 변경: 다시 확인합니다')
                quota = {**normalize_limits(result, received_at, key),
                         'requested_at': requested_at, 'elapsed': elapsed}
                self._quota_cache[key] = (time.monotonic(), quota)
                return quota
        except Exception:
            self.close()
            raise

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                if stream: stream.close()
            self.process = None
