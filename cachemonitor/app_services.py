"""Application-owned service lifetime; collection remains in CollectorService."""
import json
import os
import time
import sys
import threading
import uuid
from pathlib import Path
from .observer_state import ProcessLock, read_json
from .observer_control import atomic_write
from .proxy_target import ProxyTarget, option
from .shutdown_state import ExitCancelled, ExitControl, ExitProgress, ExitResult
from . import proxy_identity as identity

def session_path(manager):
    return manager.directory / 'app-services.json'

def suspended(manager):
    value = read_json(session_path(manager))
    return value.get('scope') == ProxyTarget(manager).scope and value.get('phase') in ('preparing', 'closing', 'stopped', 'failed', 'resuming')

def require_running(manager):
    if suspended(manager):
        raise RuntimeError('Codexon 종료 중이거나 종료된 상태입니다. 앱을 다시 열어 주세요.')

def disable_resume(manager):
    path = session_path(manager)
    value = read_json(path)
    if value.get('scope') == ProxyTarget(manager).scope:
        value['resume'] = False
        atomic_write(path, json.dumps(value).encode())


class ExitRecoveryBlocked(RuntimeError):
    pass


def exit_recovery_owner(manager):
    """Read scoped exit-owner proof without changing services or route intent."""
    value = read_json(session_path(manager))
    if value.get('scope') != ProxyTarget(manager).scope or value.get('phase') not in ('preparing', 'closing'):
        return None
    owner = value.get('preparation_owner')
    proof = dict(scope=value['scope'], phase=value['phase'], operation_id=value.get('operation_id'),
                 preparation_owner=owner, owner_state='unknown')
    if (not isinstance(proof['operation_id'], str) or not proof['operation_id']
            or not isinstance(owner, dict) or set(owner) != {'pid', 'created', 'executable'}
            or type(owner['pid']) is not int or owner['pid'] <= 0
            or type(owner['created']) is not int or owner['created'] <= 0
            or not isinstance(owner['executable'], str) or not owner['executable']):
        return proof
    try:
        alive = identity.same_process(owner)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return proof
    proof['owner_state'] = 'live' if alive is True else 'dead' if alive is False else 'unknown'
    return proof


def recover_after_exit(manager, expected):
    """Honor explicit Off after a proven owner exit, without touching its proxy."""
    with ProcessLock(manager.control_lock, timeout=5):
        current = exit_recovery_owner(manager)
        if current != expected:
            raise ExitRecoveryBlocked('종료 작업이 변경되어 직접 연결 복원을 보류했습니다. 상태를 다시 확인하세요.')
        if not current or current['owner_state'] != 'dead':
            raise ExitRecoveryBlocked('종료 작업이 진행 중이거나 소유권을 확인하지 못해 직접 연결 복원을 보류했습니다.')
        status = manager.recover_direct()
        if manager.config()[1].get('openai_base_url') == manager.url:
            raise RuntimeError('프록시 연결 설정이 남아 있습니다. 복원이 완료되지 않았습니다.')
        # Restoring direct access settles route intent, not proxy exit or storage.
        AppServices(manager, [], collection=False).save(phase='active', resume=False)
        return status


class LoginLaunch:
    """A login request can be cancelled on the GUI thread during route recovery."""
    def __init__(self, launch):
        self.launch = launch
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def __call__(self):
        return 'cancelled' if self.cancelled.is_set() else self.launch()


class AppServices:

    def __init__(self, manager, homes, index=None, evidence=None, *, progress=lambda text: None, clock=time.monotonic, sleep=time.sleep, collection=True, login_launch=None):
        self.manager, self.homes = (manager, homes)
        self.index, self.evidence = (index, evidence)
        self.target = ProxyTarget(manager) if manager else None
        self.progress, self.clock, self.sleep = (progress, clock, sleep)
        self.collection = collection
        self.active = False
        self.recovery_deferred = False
        self.closing = threading.Event()
        self.operation_lock = threading.RLock()
        self.retries = {}
        self.proxy_source = None
        self.collector_source = None
        self.collector_pending = None
        self.collection_fault = {}
        self.collection_healthy = False
        self.collection_snapshot_at = None
        self.login_launch = login_launch

    def deactivate(self):
        self.closing.set()
        self.active = False

    def release_exit(self, *, defer_recovery=True):
        """Reopen controls after finalization failure without changing services."""
        self.recovery_deferred = defer_recovery
        self.closing.clear()
        self.active = True

    def execute(self, operation):
        """Serialize UI actions and the app's single periodic operation."""
        with self.operation_lock:
            if self.closing.is_set():
                return {}
            if operation == 'login':
                return self.launch_login()
            if operation in ('start', 'ensure'):
                result = self.start() if operation == 'start' else self.poll()
                if not result.get('collection_issue') and (not result.get('error')):
                    from .install_cleanup import after_services
                    try:
                        after_services(self)
                    except (OSError, ValueError, RuntimeError) as exc:
                        result['cleanup_warning'] = str(exc)
                return result
            if operation in ('turn_on', 'resume'):
                self.retries.pop('proxy', None)
                if suspended(self.manager):
                    resume_proxy(self.manager)
            result = getattr(self.manager, operation)()
            self.remember_proxy(result)
            if operation in ('turn_on', 'resume'):
                self.recovery_deferred = False
            return result

    def status(self):
        return self.manager.status() if self.manager else {}

    def remember_proxy(self, result):
        health = result.get('health') or {}
        if health.get('instance') and health['instance'] != (self.proxy_source or {}).get('instance'):
            self.proxy_source = self.target.capture(health)

    def start(self):
        from .usage_collection import CollectionPending
        from .app_errors import exception_facts
        self.active = True
        errors = []
        login_pending = self.login_launch is not None
        result = self.launch_login() if login_pending else {}
        if self.collection and not self.closing.is_set():
            try:
                self.start_collection()
                self.collection_fault=dict(starting=bool(self.collector_pending))
                self.collection_healthy=bool(self.collector_source and not self.collector_pending)
                if self.collector_pending:
                    errors.append('수집 시작 중')
            except CollectionPending as exc:
                self.collection_fault=dict(status_pending=True)
                errors.append(str(exc))
            except (OSError, ValueError, RuntimeError) as exc:
                self.collection_fault=dict(start_failed=True,**exception_facts(exc))
                errors.append(str(exc))
        if not login_pending:
            result.update(self.start_proxy())
        elif not result.get('health') and not result.get('error'):
            # Direct mode launches before any optional service probes.
            try:
                result.update(self.start_proxy())
            except (OSError, ValueError, RuntimeError) as exc:
                result['error'] = str(exc)
        if errors:
            result['collection_issue'] = '\n'.join(errors)
        elif self.collection:
            result['collection_issue'] = ''
        if self.collection:
            result.update(collection_fault=dict(self.collection_fault),collection_healthy=self.collection_healthy,
                          collection_snapshot_at=self.collection_snapshot_at)
        return result

    def launch_login(self):
        result = {}
        launch, self.login_launch = self.login_launch, None
        if launch and not self.closing.is_set():
            # The login app needs the route, not the collector or installation cleanup.
            route_ready = False
            try:
                if not self.direct_login_ready():
                    result = self.start_proxy()
                    if not self.login_ready(result):
                        raise RuntimeError('Codexon 연결 준비를 확인하지 못해 ChatGPT 자동 실행을 보류했습니다.')
                route_ready = True
                if not self.closing.is_set():
                    result['chatgpt_startup'] = launch()
            except (OSError, ValueError, RuntimeError) as exc:
                if not (isinstance(launch, LoginLaunch) and launch.cancelled.is_set()):
                    result['chatgpt_startup_error'] = str(exc)
                if not route_ready:
                    result['error'] = str(exc)
        return result

    def direct_login_ready(self):
        m = self.manager
        if not m or suspended(m):
            return False
        state = m.state()
        from .proxy_update import BUSY
        return (m.config()[1].get('openai_base_url') != m.url
                and not state.get('enabled') and not state.get('pending')
                and state.get('phase') not in ('faulted', 'failed', 'recovery_failed')
                and read_json(m.directory/'proxy-update.json').get('phase') not in BUSY)

    def login_ready(self, result):
        m = self.manager
        if not m or self.closing.is_set():
            return False
        if self.direct_login_ready():
            return True
        from .proxy_update import BUSY
        return (not suspended(m) and m.config()[1].get('openai_base_url') == m.url
                and m.state().get('enabled') and not m.state().get('pending')
                and result.get('phase') == 'active' and not result.get('service_issue')
                and (result.get('update') or {}).get('phase') not in BUSY
                and m.local_ready(result.get('health')))

    def start_proxy(self):
        result = {}
        if self.manager and (not self.closing.is_set()):
            from .retired_cache import retire
            retire(self.manager)
            self.manager.cleanup_legacy_check()
            self.manager.adopt_registrations()
            if not self.closing.is_set():
                result = self.manager.resume()
                self.remember_proxy(result)
        return result

    def collector_identity(self, channel, snapshot):
        from .usage_collection import locked
        lock = channel.companion('.collector.lock')
        if not locked(lock):
            self.collector_source = None
            return None
        try:
            source = self._collector_identity(channel, snapshot)
            if not locked(lock):
                self.collector_source = None
                return None
            return source
        except (OSError, ValueError, RuntimeError):
            # The owner may have exited during process inspection. A failed
            # lock probe must propagate rather than authorize a replacement.
            if not locked(lock):
                self.collector_source = None
                return None
            raise

    def _collector_identity(self, channel, snapshot):
        from .usage_collection import CollectionPending
        source = (snapshot or {}).get('collection') or {}
        if not source.get('pid'):
            raise CollectionPending('수집기 실행 정보 확인 대기')
        process = identity.process_identity(source['pid'])
        if not process:
            raise CollectionPending('수집기 실행 정보가 변경되었습니다.')
        saved = self.collector_source
        if saved and saved['process'] == process and (saved['instance'] == source.get('instance')):
            return saved
        command = identity.process_command(process['pid'])
        homes = [str(Path(command[n + 1]).resolve()) for n, arg in enumerate(command[:-1]) if arg == '--codex-home']
        if '--usage-collector' not in command or not homes or (not set(homes).issubset(set(channel.homes))) or (Path(option(command, '--index-path', '')).resolve() != channel.path) or (Path(option(command, '--evidence-path', str(channel.default_evidence))).resolve() != Path(channel.scope)) or (Path(process['executable']).resolve() != Path(source.get('executable', '')).resolve()):
            raise CollectionPending('다른 수집기의 실행 정보를 보존합니다.')
        if not identity.same_process(process):
            raise CollectionPending('수집기 실행 정보가 변경되었습니다.')
        self.collector_source = dict(process=process, instance=source['instance'])
        return self.collector_source

    def launch_collector(self, task, command, snapshot):
        task.start(command, autostart=False)
        self.collector_pending = ((snapshot or {}).get('collection', {}).get('instance'), self.clock())

    def start_collection(self):
        """Startup/adoption is the only collection migration entry point."""
        from .usage_collection import CollectionChannel, collector_command, locked
        from .collection_lifecycle import retire_legacy, startup_snapshot
        from .observer_task import ObserverTask
        from .version import VERSION
        channel = CollectionChannel(self.homes, self.index, self.evidence)
        try:
            with ProcessLock(channel.companion('.collector-update.lock'), timeout=30):
                task = ObserverTask(str(channel.path), role='UsageCollector')
                command = collector_command(channel)
                retire_legacy(channel)
                snapshot, broken_default = startup_snapshot(channel)
                source = self.collector_identity(channel, snapshot)
                version = ((snapshot or {}).get('collection') or {}).get('version', '')
                newer = tuple((int(p) for p in version.split('.') if p.isdigit())) > tuple((int(p) for p in VERSION.split('.') if p.isdigit()))
                if newer and source:
                    raise RuntimeError('새 버전 수집기를 이전 앱으로 교체하지 않습니다.')
                replace = source and (broken_default or version != VERSION or (getattr(sys, 'frozen', False) and Path(source['process']['executable']).resolve() != Path(sys.executable).resolve()))
                if replace:
                    task.configure(command, autostart=False)
                    channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)', (source['instance'], 'stop'))
                    channel.db.commit()
                    self.wait(lambda: not identity.same_process(source['process']) and (not locked(channel.companion('.collector.lock'))), '수집 기록 저장과 이전 수집기 종료 대기')
                    source = None
                if self.closing.is_set():
                    return
                atomic_write(channel.companion('.session.json'), json.dumps(dict(scope=channel.scope, stopped=False)).encode())
                if source:
                    task.configure(command, autostart=False)
                elif not locked(channel.companion('.collector.lock')) and (not task.inspect().get('running')):
                    self.launch_collector(task, command, snapshot)
        finally:
            channel.close()

    def retry(self, name, dead, start):
        """A successful restart never replenishes this app session's budget."""
        if self.recovery_deferred:
            return
        state = self.retries.setdefault(name, dict(at=None, count=0))
        if not dead:
            state['at'] = None
            return
        now = self.clock()
        if state['at'] is None:
            state['at'] = now + 60
        if now < state['at'] or state['count'] >= 3 or self.closing.is_set():
            return
        state['count'] += 1
        state['at'] = now + 60
        start()

    def poll_collection(self):
        from hashlib import sha256
        from .usage_collection import CollectionChannel, collector_command, locked
        from .observer_task import ObserverTask
        channel = CollectionChannel(self.homes, self.index, self.evidence)
        self.collection_fault={};self.collection_healthy=False
        try:
            with ProcessLock(channel.companion('.collector-update.lock')):
                snapshot = channel.read_header()
                identity_snapshot = snapshot
                marker=read_json(channel.companion('.collector-error.json'))
                marker_facts=(marker['facts'] if marker.get('scope')==sha256(channel.scope.encode('utf-8')).hexdigest()
                    and isinstance(marker.get('at'),(int,float)) and marker['at']>(snapshot or {}).get('ts',0)
                    and isinstance(marker.get('facts'),dict) else {})
                published = False
                if self.collector_pending:
                    previous, requested = self.collector_pending
                    current = (snapshot or {}).get('collection', {}).get('instance')
                    if not current or current == previous:
                        if self.clock() - requested < 30:
                            self.collection_fault=dict(starting=True)|marker_facts
                            return '수집 시작 중'
                        # Continue the normal liveness checks after the grace
                        # period, but never inspect the previous owner's PID.
                        identity_snapshot = None
                    elif time.time() - snapshot['ts'] <= 30:
                        published = True
                source = self.collector_identity(channel, identity_snapshot)
                if source and snapshot and not self.collector_pending:
                    self.collection_snapshot_at=snapshot['ts']
                if published and source:
                    self.collector_pending = None
                stopped = read_json(channel.companion('.session.json'))
                task = ObserverTask(str(channel.path), role='UsageCollector')
                dead = not source and (not locked(channel.companion('.collector.lock'))) and (not task.inspect().get('running')) and (not (stopped.get('scope') == channel.scope and stopped.get('stopped')))
                try:self.retry('collection', dead, lambda: self.launch_collector(task, collector_command(channel), snapshot))
                except (OSError,ValueError,RuntimeError):
                    self.collection_fault=dict(start_failed=True,
                        retry_exhausted=self.retries.get('collection',{}).get('count',0)>=3)
                    raise
                self.collection_healthy=bool(source and snapshot and not self.collector_pending and time.time()-snapshot['ts']<=30)
                if dead:
                    grace=self.collector_pending and self.clock()-self.collector_pending[1]<30
                    self.collection_fault=dict(runtime_failed=True,
                        retry_exhausted=self.retries.get('collection',{}).get('count',0)>=3 and not grace)
                self.collection_fault.update(marker_facts)
                if stopped.get('scope')==channel.scope and stopped.get('stopped'):
                    self.collection_fault=dict(expected_stop=True)
                if self.collector_pending:self.collection_fault['handoff_pending']=True
                if not source:self.collection_fault['owner_unknown']=True
                if not snapshot:self.collection_fault['snapshot_missing']=True
                elif time.time()-snapshot['ts']>30:self.collection_fault['snapshot_stale']=True
                return '수집 결과 갱신 지연' if self.collector_pending or not source or not snapshot or time.time() - snapshot['ts'] > 30 else ''
        finally:
            channel.close()

    def poll(self):
        if not self.active or self.closing.is_set():
            return {}
        from .proxy_update import BUSY
        result = self.status()
        if self.collection:
            from .usage_collection import CollectionPending
            from .app_errors import exception_facts
            try:
                result['collection_issue'] = self.poll_collection()
            except CollectionPending as exc:
                self.collection_fault=dict(status_pending=True)
                self.collection_healthy=False
                result['collection_issue']=str(exc)
            except (OSError, ValueError, RuntimeError) as exc:
                self.collection_fault=({**self.collection_fault,**exception_facts(exc)} if self.collection_fault.get('start_failed')
                    else dict(status_failed=True,**exception_facts(exc)))
                self.collection_healthy=False
                result['collection_issue'] = str(exc)
            result.update(collection_fault=dict(self.collection_fault),collection_healthy=self.collection_healthy,
                          collection_snapshot_at=self.collection_snapshot_at)
        if self.manager:
            self.remember_proxy(result)
            m = self.manager
            with ProcessLock(m.control_lock):
                updating = read_json(m.directory / 'proxy-update.json').get('phase') in BUSY
                dead = not updating and self.target.enabled() and (result.get('probe_state') == 'refused') and (not m.task.inspect().get('running')) and identity.port_free(m.url) and identity.locks_free(self.target.locks) and (not self.proxy_source or self.target.exited(self.proxy_source))
            self.retry('proxy', dead, m.resume)
        return result

    def save(self, **changes):
        path = session_path(self.manager)
        value = read_json(path)
        if value and value.get('scope') != self.target.scope:
            raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
        value.update(scope=self.target.scope, **changes)
        atomic_write(path, json.dumps(value, ensure_ascii=False).encode())
        return value

    def publish(self, phase, **value):
        self.progress(value.get('message', '관련 프로세스가 종료됐는지 확인하고 있습니다.'))

    def wait(self, done, message, seconds=30, *, checkpoint=lambda: None):
        deadline = self.clock() + seconds
        while True:
            checkpoint()
            if done():
                return
            if self.clock() >= deadline:
                raise RuntimeError(message)
            self.sleep(0.2)

    def stop_proxy(self, *, force_requested=lambda: False, control=None, publish=None):
        from .model_evidence import home_key
        from .observer_task import ObserverTask
        from .proxy_update import ProxyUpdate, BUSY
        from .proxy_drain import ProxyDrain
        m = self.manager
        control = control or ExitControl()
        operation_id = uuid.uuid4().hex
        previous = None

        def report(phase, **value):
            self.publish(phase, **value)
            if publish:
                publish(ExitProgress(phase, value.get('message', ''),
                    can_cancel=not control.committed and not control.cancelled,
                    can_force=bool(value.get('can_force')) and not control.cancelled,
                    responding=value.get('responding', 0), unknown=value.get('unknown_connections', 0)))

        def force():
            return control.force_requested or force_requested()

        def commit_route(_=None):
            with ProcessLock(m.control_lock, timeout=30):
                control.checkpoint()
                update = read_json(m.directory / 'proxy-update.json')
                if update.get('cutover_started') and update.get('phase') in BUSY:
                    raise RuntimeError('업데이트 적용 또는 복원이 진행 중입니다. 완료 후 다시 종료하세요.')
                data = read_json(session_path(m))
                if data.get('operation_id') != operation_id:
                    raise RuntimeError('종료 작업이 변경되어 기존 연결을 보존했습니다.')
                configured = m.config()[1].get('openai_base_url') == m.url
                direct = data.get('direct_url')
                if configured and direct not in (None, 'https://api.openai.com/v1', 'https://chatgpt.com/backend-api/codex'):
                    raise RuntimeError('원래 연결 주소를 확인하지 못했습니다.')
                control.commit(lambda: self.save(phase='closing', committed=True))
                update_task.suspend()
                for task in (m.task, m.legacy_task):
                    task.suspend()
                if configured and m.config()[1].get('openai_base_url') == m.url:
                    m.set_url(direct)
                state = m.state()
                state.update(enabled=False, pending=False, phase='off')
                m.write_state(state)

        try:
            report('preparing', message='업데이트 적용 또는 복원이 끝나기를 기다리고 있습니다.')
            # A committed replacement owns routing until verification or rollback.
            self.wait(lambda: not (read_json(m.directory / 'proxy-update.json').get('cutover_started')
                                  and read_json(m.directory / 'proxy-update.json').get('phase') in BUSY),
                      '업데이트 적용 또는 복원이 진행 중입니다. 완료 후 다시 종료하세요.', checkpoint=control.checkpoint)
            with ProcessLock(m.control_lock, timeout=30):
                control.checkpoint()
                update = read_json(m.directory / 'proxy-update.json')
                if update.get('cutover_started') and update.get('phase') in BUSY:
                    raise RuntimeError('업데이트 적용 또는 복원이 진행 중입니다. 완료 후 다시 종료하세요.')
                previous = read_json(session_path(m))
                if previous and previous.get('scope') != self.target.scope:
                    raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
                if previous.get('phase') == 'preparing':
                    owner = previous.get('preparation_owner')
                    if not owner or identity.same_process(owner):
                        raise RuntimeError('다른 종료 준비 작업이 진행 중이거나 소유권을 확인하지 못했습니다.')
                    previous = {**previous, 'phase': previous.get('preparation_previous_phase', 'active'),
                                'committed': previous.get('preparation_previous_committed', False)}
                state = m.state()
                configured = m.config()[1].get('openai_base_url') == m.url
                preparation_owner = identity.process_identity(os.getpid())
                if not preparation_owner:
                    raise RuntimeError('종료 준비 프로세스의 소유권을 확인하지 못했습니다.')
                preparation = dict(preparation_owner=preparation_owner,
                    preparation_previous_phase=previous.get('phase', 'active'),
                    preparation_previous_committed=previous.get('committed', False))
                if previous.get('phase') not in ('closing', 'failed', 'stopped', 'resuming'):
                    self.save(phase='preparing', operation_id=operation_id, committed=False,
                              resume=bool(configured), state=state, source=previous.get('source'),
                              direct_url=state.get('previous_url'), **preparation)
                else:
                    self.save(phase='preparing', operation_id=operation_id, committed=False,
                              **preparation)
                updater = ProxyUpdate(m)
                update_task = ObserverTask(home_key(m.home), role='ProxyUpdate')
            # The preparing journal inhibits new mutations. Existing pre-cutover
            # updaters observe it through target.enabled() and finish cancellation.
            report('preparing', message='진행 중인 연결 설정 작업이 끝나기를 기다리고 있습니다.')
            self.wait(lambda: identity.locks_free([m.directory / 'proxy-update.lock'])
                      and not update_task.inspect().get('running'),
                      '업데이트 프로세스 종료 확인 지연', checkpoint=control.checkpoint)
            with ProcessLock(m.control_lock, timeout=30):
                control.checkpoint()
                health = m.health(timeout=3)
                data = read_json(session_path(m))
                source = self.target.capture(health) if health else self.proxy_source or data.get('source')
                if not source and not health:
                    interrupted = read_json(updater.path)
                    if interrupted.get('scope') == self.target.scope:
                        source = interrupted.get('source')
                if source:
                    self.target.validate_command(source['command'])
                    self.save(source=source)
                elif not (identity.port_free(m.url) and identity.locks_free(self.target.locks)):
                    raise RuntimeError('관련 프로세스 소유권을 확인하지 못해 종료를 보류했습니다.')
            if source:
                if not self.target.stopped(source) and self.target.released(source):
                    commit_route()
                    self.save(shutdown_incomplete=True, shutdown_incomplete_instance=source['instance'])
                else:
                    settled = ProxyDrain(m, self.target, allowed=control.checkpoint, publish=report,
                        before_drain=commit_route, clock=self.clock, sleep=self.sleep,
                        force_requested=force, purpose='exit', allow_missing_flush=True).run(source)
                    if settled is False:
                        self.save(shutdown_incomplete=True, shutdown_incomplete_instance=source['instance'])
            if not control.committed:
                commit_route()
            m.cleanup_legacy_check()
            self.save(phase='stopped')
            data = read_json(session_path(m))
            if data.get('shutdown_incomplete') and data.get('shutdown_incomplete_instance') == (source or {}).get('instance'):
                return '종료한 프록시의 기록 저장 완료를 확인하지 못했습니다.'
            return ''
        except Exception as exc:
            with ProcessLock(m.control_lock, timeout=30):
                data = read_json(session_path(m))
                if data.get('operation_id') == operation_id:
                    phase = 'failed' if control.committed else (previous or {}).get('phase', 'active')
                    if phase == 'preparing':
                        phase = 'active'
                    self.save(phase=phase, committed=control.committed or bool((previous or {}).get('committed')),
                              error='' if isinstance(exc, ExitCancelled) else str(exc))
            raise

    def stop_collection(self):
        from .usage_collection import CollectionChannel, locked
        from .observer_task import ObserverTask
        channel = CollectionChannel(self.homes, self.index, self.evidence)
        try:
            with ProcessLock(channel.companion('.collector-update.lock'), timeout=30):
                from .collection_lifecycle import retire_legacy
                retire_legacy(channel)
                snapshot = channel.read_header()
                collection = (snapshot or {}).get('collection') or {}
                lock = channel.companion('.collector.lock')
                process = None
                if locked(lock):
                    if not collection.get('instance'):
                        raise RuntimeError('수집기 종료 소유권 확인 대기')
                    process = identity.process_identity(collection['pid'])
                    if not process:
                        raise RuntimeError('수집기 실행 정보가 변경되었습니다.')
                    command = identity.process_command(process['pid'])
                    if '--usage-collector' not in command or Path(option(command, '--index-path', '')).resolve() != channel.path or Path(process['executable']).resolve() != Path(collection['executable']).resolve() or (not set((snapshot or {}).get('homes', [])).issubset(set(channel.homes))):
                        raise RuntimeError('다른 수집기 또는 다른 Codex 홈의 기록을 보존합니다.')
                atomic_write(channel.companion('.session.json'), json.dumps(dict(scope=channel.scope, stopped=True)).encode())
                task = ObserverTask(str(channel.path), role='UsageCollector')
                task.suspend()
                if process:
                    channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)', (collection['instance'], 'stop'))
                    channel.db.commit()
                    self.progress('수집 기록 저장과 수집기 종료를 확인하고 있습니다.')
                    self.wait(lambda: not identity.same_process(process) and (not locked(lock)), '수집 기록 저장 또는 수집기 종료 확인 지연')
                self.wait(lambda: not task.inspect().get('running'), '수집기 작업 종료 확인 지연')
        finally:
            channel.close()

    def stop(self, force_requested=None, *, control=None, publish=None):
        control = control or ExitControl()
        warning = ''
        self.deactivate()
        with self.operation_lock:
            try:
                control.checkpoint()
                if self.manager:
                    warning = self.stop_proxy(force_requested=force_requested or (lambda: False),
                                              control=control, publish=publish)
                if self.collection:
                    if not control.committed:
                        control.commit()
                    if publish:
                        publish(ExitProgress('stopping', '수집 기록 저장과 수집기 종료를 확인하고 있습니다.', False))
                    self.stop_collection()
                if not control.committed:
                    control.commit()
                return ExitResult('completed', committed=control.committed, warning=warning)
            except ExitCancelled:
                self.release_exit(defer_recovery=control.committed)
                return ExitResult('cancelled', committed=control.committed)
            except Exception as exc:
                self.release_exit(defer_recovery=control.committed)
                return ExitResult('failed', committed=control.committed, error=str(exc), warning=warning)

def resume_proxy(manager):
    """Resume only the connection suspended by this app, with its saved role."""
    m = manager
    target = ProxyTarget(m)
    path = session_path(m)
    data = read_json(path)
    if not data:
        return False
    if data.get('scope') != target.scope:
        raise RuntimeError('다른 연결의 종료 기록을 보존합니다.')
    if data.get('phase') == 'active':
        return False
    services = AppServices(m, [], progress=lambda _: None)
    if data.get('phase') == 'preparing':
        with ProcessLock(m.control_lock, timeout=30):
            data = read_json(path)
            if data.get('phase') == 'preparing':
                owner = data.get('preparation_owner')
                if not owner:
                    raise RuntimeError('종료 준비 소유권을 확인하지 못해 연결 재개를 보류했습니다.')
                if identity.same_process(owner):
                    return True
                # Preparation never changed routing or service registrations.
                phase = data.get('preparation_previous_phase', 'active')
                services.save(phase=phase, committed=data.get('preparation_previous_committed', False))
                if phase == 'active':
                    return False
                data = read_json(path)
    if data.get('phase') in ('closing', 'failed', 'resuming'):
        services.stop_proxy()
        data = read_json(path)
    with ProcessLock(m.control_lock, timeout=30):
        if not data.get('resume') or m.config()[1].get('openai_base_url') != data.get('direct_url'):
            services.save(phase='active', resume=False)
            return True
        source = data.get('source')
        if not source:
            raise RuntimeError('이전 프록시의 실행 정보를 확인하지 못해 연결 재개를 보류했습니다.')
        if not target.stopped(source) and not (data.get('shutdown_incomplete')
                and data.get('shutdown_incomplete_instance') == source.get('instance') and target.released(source)):
            raise RuntimeError('이전 프로세스 종료 확인 후에만 재개할 수 있습니다.')
        command = target.replacement(source)
        services.save(phase='resuming')
        state = dict(data['state'])
        state.update(enabled=False, pending=True, phase='starting')
        m.write_state(state)
        try:
            from .version import PROXY_VERSION
            import sys
            distribution = identity.deployment(command[0]) if getattr(sys, 'frozen', False) else None
            m.task.start(command, autostart=False)
            deadline = time.monotonic() + 30
            consecutive = 0
            instance = None
            while time.monotonic() < deadline:
                health = m.health(timeout=1)
                valid = target.ready(health, source, command, PROXY_VERSION, distribution)
                candidate = (health or {}).get('instance')
                consecutive = consecutive + 1 if valid and instance == candidate else 1 if valid else 0
                instance = candidate
                if consecutive >= 3:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('앱 연결 재개 준비 확인 실패 / 직접 연결 유지')
            m.set_url(m.url)
            state = dict(data['state'])
            state.update(enabled=True, pending=False, phase='active')
            m.write_state(state)
            services.save(phase='active')
        except Exception:
            services.save(phase='failed')
            raise
    return True
