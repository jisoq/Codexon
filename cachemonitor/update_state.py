"""Durable identity and component results for one user-requested update."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

from .observer_state import ProcessLock, read_json

ACTIVE = frozenset(('preparing', 'downloading', 'installing', 'waiting', 'needs_exit',
                    'switching', 'stopping', 'starting', 'verifying', 'rollback'))
CRITICAL = frozenset(('switching', 'stopping', 'starting', 'verifying', 'rollback'))


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


class UpdateState:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'app-update.json'
        self.lock = self.root / 'app-update-journal.lock'

    def read(self):
        value = read_json(self.path)
        return value if valid_id(value.get('operation_id')) else {}

    def write(self, value):
        from .observer_control import atomic_write
        value['updated_at'] = time.time()
        atomic_write(self.path, json.dumps(value, ensure_ascii=False).encode())

    def begin(self, kind, version, *, scope=None, release=None, replace=False):
        self.root.mkdir(parents=True, exist_ok=True)
        with ProcessLock(self.lock, timeout=5):
            previous = self.read()
            if previous.get('phase') in ACTIVE and not replace:
                raise RuntimeError('업데이트가 이미 진행 중입니다.')
            value = dict(operation_id=uuid.uuid4().hex, kind=kind, target_version=version,
                         phase='preparing', created_at=time.time(), scope=scope,
                         app={'state': 'current' if kind == 'proxy' else 'pending'},
                         proxy={'state': 'pending'}, release=release,
                         cancel_requested=False, defer_requested=False)
            if os.name=='nt':
                from .proxy_identity import process_identity
                value['owner']=process_identity(os.getpid())
            self.write(value)
            return value

    def change(self, operation_id, **changes):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id:
                return False
            value.update(changes)
            self.write(value)
            return True

    def cancelled(self, operation_id):
        value = self.read()
        return value.get('operation_id') != operation_id or value.get('cancel_requested', False)

    def install_ready(self, operation_id, output, digest):
        with ProcessLock(self.lock,timeout=5):
            value=self.read()
            if value.get('operation_id')!=operation_id or value.get('cancel_requested'):return False
            value.update(phase='installing',installer=str(output),installer_sha256=digest)
            self.write(value)
            return True

    def begin_switch(self,operation_id):
        with ProcessLock(self.lock,timeout=5):
            value=self.read()
            if value.get('operation_id')!=operation_id or value.get('cancel_requested'):return False
            value.update(phase='switching',proxy={**value.get('proxy',{}),'cutover_started':True})
            self.write(value)
            return True

    def cancel(self, *, later=False):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('proxy',{}).get('cutover_started') or value.get('phase') not in ('preparing', 'downloading', 'waiting', 'needs_exit'):
                return False
            value.update(cancel_requested=True, defer_requested=bool(later))
            self.write(value)
            return True

    def installed(self, operation_id, product, manifest, *, scope=None, recovered=False):
        """Called only after installer runtime/hash/activation checks have passed."""
        product = Path(product).resolve()
        if not product.is_relative_to(self.root / 'versions'):
            raise ValueError('설치 결과의 실행 경로가 올바르지 않습니다.')
        if operation_id is None:
            operation_id = self.begin('app', manifest['version'], scope=scope, replace=True)['operation_id']
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id:
                return None
            if value['target_version'] != manifest['version']:
                raise ValueError('요청한 버전과 설치 결과가 다릅니다.')
            app=dict(state='verified', version=manifest['version'],
                     executable=str(product / 'Codexon.exe'), sha256=manifest['sha256'],
                     commit=manifest['commit'])
            if recovered:
                if scope != value.get('scope'):
                    return None
                if value.get('app') == app:
                    return operation_id
                if value.get('phase') != 'installing' or value.get('app',{}).get('state') == 'verified':
                    return None
            value.update(phase='waiting', scope=scope or value.get('scope'), cancel_requested=False,
                         app=app)
            self.write(value)
            return operation_id

    def proxy_result(self, operation_id, result, *, replay=False):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id:
                return False
            if result.get('operation_id', operation_id) != operation_id:
                return False
            if result.get('target_app_version') and result['target_app_version'] != value.get('target_version'):
                return False
            if result.get('scope') and value.get('scope') and result['scope'] != value['scope']:
                return False
            if replay and not matches_proxy_target(value, result):
                return False
            previous = value.get('proxy', {})
            if (result.get('updated_at') and previous.get('updated_at')
                    and result['updated_at'] < previous['updated_at']):
                return False
            phase = result.get('phase', '')
            app_done = value.get('app', {}).get('state') in ('verified', 'current')
            mapped = phase
            if phase == 'queued':
                mapped = 'waiting'
            elif phase == 'waiting' and result.get('required_action') == 'close_client':
                mapped = 'needs_exit'
            elif phase in ('complete', 'off'):
                mapped = 'complete' if app_done else 'installing'
            elif phase == 'failed':
                mapped = 'partial' if value.get('app', {}).get('state') == 'verified' else 'failed'
            elif phase == 'cancelled':
                mapped = 'deferred' if app_done or value.get('defer_requested') else 'cancelled'
            if phase=='recovery_required':mapped='partial' if app_done else 'failed'
            if (value.get('phase') == mapped and previous == result
                    and not value.get('cancel_requested')):
                return True
            value.update(phase=mapped, proxy=dict(result), required_action=result.get('required_action'),
                         message=result.get('message', ''))
            if phase in ('complete','off','cancelled','failed'):value['cancel_requested']=False
            self.write(value)
            return True

    def fail(self, operation_id, message):
        value = self.read()
        phase = 'partial' if value.get('app', {}).get('state') == 'verified' else 'failed'
        return self.change(operation_id, phase=phase, message=str(message))


def matches_proxy_target(record, result):
    """Only replay evidence bound to the requested operation, connection and build."""
    if (not record.get('scope') or result.get('scope') != record['scope']
            or result.get('operation_id') != record.get('operation_id')):
        return False
    app = record.get('app', {})
    previous = record.get('proxy', {})
    expected = (dict(executable=app.get('executable'), sha256=app.get('sha256'),
                     commit=app.get('commit')) if app.get('state') == 'verified'
                else previous.get('distribution'))
    actual = result.get('distribution')
    if actual and expected:
        if any(not expected.get(key) or actual.get(key) != expected[key]
               for key in ('executable', 'sha256', 'commit')):
            return False
    elif result.get('phase') == 'complete':
        return False
    target_app = result.get('target_app_version')
    if target_app:
        return target_app == record.get('target_version')
    # Older workers did not publish the app version. Their independently
    # verified deployment is sufficient only when the requested build matches.
    return bool(actual and expected and result.get('target_version')
                and result.get('target_version') == previous.get('target_version'))


def scope_for(manager):
    from .proxy_target import ProxyTarget
    return ProxyTarget(manager).scope


def state_for(manager=None, install=None):
    """Resolve persisted state without creating files during a settings read."""
    if install and install.get('InstallRoot'):
        return UpdateState(install['InstallRoot'])
    if manager is not None and getattr(manager, 'directory', None):
        return UpdateState(manager.directory)
    return None
