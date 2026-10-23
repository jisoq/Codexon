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
                    'switching', 'stopping', 'starting', 'verifying', 'rollback', 'closing_client', 'restarting_client'))
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

    def begin(self, kind, version, *, scope=None, release=None, replace=False, client=None):
        self.root.mkdir(parents=True, exist_ok=True)
        with ProcessLock(self.lock, timeout=5):
            previous = self.read()
            if previous.get('phase') in ACTIVE and not replace:
                raise RuntimeError('업데이트가 이미 진행 중입니다.')
            client = dict(client or dict(phase='awaiting_consent'))
            client.setdefault('reopen_required', bool(client.get('targets')))
            if (previous.get('kind') == kind and previous.get('target_version') == version
                    and previous.get('scope') == scope and previous.get('phase') not in ('complete', 'cancelled')):
                client['reopen_required'] |= previous.get('client', {}).get('reopen_required', False)
            value = dict(operation_id=uuid.uuid4().hex, kind=kind, target_version=version,
                         phase='preparing', created_at=time.time(), scope=scope,
                         app={'state': 'current' if kind == 'proxy' else 'pending'},
                         proxy={'state': 'pending'}, release=release,
                         client=client,
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
            if 'phase' in changes:
                value['component_phase'] = changes.get('component_phase', changes['phase'])
                value['phase'] = overall_phase(value, value['component_phase'])
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

    def client_result(self, operation_id, **changes):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id:return False
            value.setdefault('component_phase', value['phase'])
            if 'reopen_required' in changes:
                changes['reopen_required'] |= value.get('client', {}).get('reopen_required', False)
            value['client'] = {**value.get('client', {}), **changes}
            value['phase'] = overall_phase(value, value.get('component_phase', value['phase']))
            self.write(value)
            return True

    def begin_activation(self, operation_id):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id or value.get('cancel_requested'):return False
            value.update(phase='installing', component_phase='installing', activation_started=True)
            self.write(value)
            return True

    def prepare_proxy_retry(self, operation_id, consent):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('operation_id') != operation_id:return False
            client = value.get('client', {})
            reopen_required = client['reopen_required']
            client = dict(consent if consent is not None else client)
            client['reopen_required'] = reopen_required or client.get('reopen_required', False)
            for key in ('ready_at', 'result', 'owner', 'message'):
                client.pop(key, None)
            value.update(phase='waiting', component_phase='waiting', client=client,
                         proxy=dict(state='pending',updated_at=time.time()), cancel_requested=False, defer_requested=False,
                         activation_started=False, required_action=None, message='')
            self.write(value)
            return True

    def cancel(self, *, later=False):
        with ProcessLock(self.lock, timeout=5):
            value = self.read()
            if value.get('activation_started') or value.get('proxy',{}).get('cutover_started') or value.get('phase') not in ('preparing', 'downloading', 'installing', 'waiting', 'needs_exit', 'closing_client', 'restart_required'):
                return False
            value.update(cancel_requested=True, defer_requested=bool(later))
            self.write(value)
            return True

    def installed(self, operation_id, product, manifest, *, scope=None, recovered=False, startup=None):
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
            if startup is not None:app['startup']=startup
            if recovered:
                if scope != value.get('scope'):
                    return None
                existing=value.get('app',{})
                if {key:item for key,item in existing.items() if key!='startup'}=={key:item for key,item in app.items() if key!='startup'}:
                    return operation_id
                if value.get('phase') != 'installing' or value.get('app',{}).get('state') == 'verified':
                    return None
            value.update(phase='waiting', component_phase='waiting', activation_started=False, scope=scope or value.get('scope'), cancel_requested=False,
                         app=app)
            self.write(value)
            return operation_id

    def bind_installed(self, operation_id, *, executable=None):
        from .installation import pointer_path
        from .proxy_identity import digest
        record=self.read();app=record.get('app',{})
        if record.get('operation_id')!=operation_id or record.get('kind')!='app' or app.get('state')!='verified':
            raise RuntimeError('설치된 업데이트 요청을 확인하지 못했습니다.')
        target=Path(app.get('executable','')).resolve()
        receipt=read_json(self.root/'installation.json')
        manifest=read_json(target.parent/'build-manifest.json')
        pointer=read_json(pointer_path())
        if (not target.is_relative_to(self.root/'versions') or (self.root/'activation-pending.json').exists()
                or executable is not None and Path(executable).resolve()!=target
                or receipt.get('operation_id')!=operation_id or receipt.get('isolated') is not False
                or receipt.get('scope')!=record.get('scope') or Path(receipt.get('product','')).resolve()!=target.parent
                or Path(pointer.get('InstallRoot','')).resolve()!=self.root
                or Path(pointer.get('AppPath','')).resolve()!=target
                or manifest.get('product')!='Codexon' or manifest.get('version')!=record.get('target_version')
                or any(not app.get(key) or manifest.get(key)!=app[key] or receipt.get(key)!=app[key]
                       for key in ('version','commit','sha256')) or digest(target)!=app['sha256']):
            raise RuntimeError('설치된 업데이트의 실행 파일을 확인하지 못했습니다.')
        return record

    def startup_result(self, operation_id, result):
        with ProcessLock(self.lock,timeout=5):
            value=self.read()
            if value.get('operation_id')!=operation_id or value.get('app',{}).get('state')!='verified':return False
            startup=value['app'].get('startup',dict(state='waiting',policy='requested'))
            if result.get('state')=='ready':
                owner=result.get('owner') or {}
                if startup.get('policy')!='requested' or owner.get('executable')!=value['app'].get('executable'):return False
            value['app']={**value['app'],'startup':{**startup,**result}}
            if result.get('state') in ('waiting','starting') and value.get('component_phase') in ('failed','partial','cancelled','deferred'):
                value['component_phase']='waiting'
            value['phase']=overall_phase(value,value.get('component_phase',value['phase']))
            self.write(value)
            return True

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
            if (previous.get('cutover_started') and not result.get('cutover_started')
                    and phase in ('queued','waiting','cancelled')):
                return False
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
            if (value.get('component_phase',value.get('phase')) == mapped and previous == result
                    and not value.get('cancel_requested')):
                return True
            value.update(component_phase=mapped, proxy=dict(result), required_action=result.get('required_action'),
                         message=result.get('message', ''))
            value['phase'] = overall_phase(value, mapped)
            if phase in ('complete','off','cancelled','failed'):value['cancel_requested']=False
            self.write(value)
            return True

    def fail(self, operation_id, message):
        value = self.read()
        phase = 'partial' if value.get('app', {}).get('state') == 'verified' else 'failed'
        return self.change(operation_id, phase=phase, component_phase=phase, message=str(message))


def installed_gui_required(record):
    return (record.get('kind')=='app' and record.get('app',{}).get('state')=='verified'
            and record.get('app',{}).get('startup',{}).get('policy','requested')!='no_launch')


def overall_phase(record, component_phase):
    client = record.get('client', {})
    phase = client.get('phase')
    if phase == 'blocked':return 'restart_required'
    if phase == 'closing':return 'closing_client'
    startup=record.get('app',{}).get('startup') or {}
    if installed_gui_required(record) and startup.get('state')!='ready':
        if startup.get('state')=='blocked':return 'restart_required' if client.get('reopen_required') else 'partial'
        if component_phase=='complete':return 'waiting'
    if phase in ('closed', 'reopening') and component_phase in ('complete', 'partial', 'failed', 'cancelled', 'deferred'):
        return 'restarting_client'
    if component_phase == 'complete' and phase not in ('restarted', 'not_running', 'not_applicable'):
        return 'restart_required'
    return component_phase


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
