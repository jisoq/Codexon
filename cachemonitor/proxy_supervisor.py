"""Independent observer supervision. Never replays requests or kills active relays."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .model_evidence import default_path, home_key
from .observer_control import ObserverManager, atomic_write
from .observer_state import ProcessLock, read_json
from .version import PROXY_VERSION


class Protection:
    """Only positive, repeated local failure evidence can trip protection."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.problem = None
        self.last = 0
        self.count = 0

    def observe(self, health, probe_state, exited=False):
        if exited: return '프록시 프로세스가 예기치 않게 종료됐습니다.'
        problem = probe_state if probe_state in ('refused', 'identity_mismatch') else None
        if health and health.get('storage_failure_streak', 0) >= 3: problem = 'storage'
        if health and health.get('internal_failure_streak', 0) >= 3: problem = 'internal'
        now = self.clock()
        if problem is None:
            self.problem = None; self.count = 0
            return None
        if problem != self.problem:
            self.problem = problem; self.count = 1; self.last = now
            return None
        if now-self.last < 5: return None
        self.last = now; self.count += 1
        if self.count < 3: return None
        return {'refused': '프록시에 반복해서 연결할 수 없습니다.',
                'identity_mismatch': '프록시 식별 정보가 일치하지 않습니다.',
                'storage': '모델 관측 기록을 연속해서 저장하지 못했습니다.',
                'internal': '프록시 내부 처리 오류가 반복됐습니다.'}[problem]


class Supervisor:
    def __init__(self, manager, upstream, *, worker_command=None, clock=time.monotonic):
        self.manager, self.upstream = manager, upstream
        self.worker_command = worker_command
        self.clock = clock
        self.protection = Protection(clock)
        self.child = None
        self.instance = uuid.uuid4().hex
        self.control = manager.directory/('proxy-control-'+self.instance+'.json')
        self.started = clock()
        self.phase = 'starting'
        self.adopted = False
        self.draining = False
        self.exit_reason = ''
        self.publish_error = ''
        self.publish_failures = 0

    def publish(self, health=None, **extra):
        value = {'home': home_key(self.manager.home), 'url': self.manager.url,
                 'version': PROXY_VERSION, 'pid': os.getpid(), 'instance': self.instance,
                 'phase': self.phase, 'updated_at': time.time(),
                 'worker_pid': (health or {}).get('pid') or (self.child.pid if self.child else None),
                 'launcher_pid': self.child.pid if self.child else None,
                 'worker_version': (health or {}).get('version'), 'adopted': self.adopted,
                 'control_file': str(self.control), 'exit_reason': self.exit_reason,
                 'publication_error': self.publish_error, 'publication_failures': self.publish_failures, **extra}
        try:
            atomic_write(self.manager.runtime_path, json.dumps(value, ensure_ascii=False).encode())
        except OSError as error:
            # A status heartbeat is not a relay failure. Keep protecting the
            # worker and retry publication on the next supervision tick.
            self.publish_error=str(error);self.publish_failures+=1
            return False
        self.publish_error='';self.publish_failures=0
        return True

    def health(self):
        try:return self.manager.health(timeout=1)
        except RuntimeError:return None

    def begin(self):
        health = self.health()
        if health:
            # A previous supervisor may have exited while its worker stayed alive.
            previous = read_json(self.manager.runtime_path)
            old_control = Path(previous.get('control_file') or self.control)
            if (health.get('control_id') and old_control.parent.resolve() == self.manager.directory.resolve()
                    and old_control.name == 'proxy-control-'+health['control_id']+'.json'):
                self.control = old_control
            self.adopted = True
        elif self.manager.health_state == 'identity_mismatch':
            # Do not bind over or signal a process we do not own.
            self.phase = 'conflict'; self.publish()
            return
        else:
            state=self.manager.state()
            if not state.get('enabled') and state.get('phase') in ('off','faulted','failed'):
                self.phase='stopped';self.publish();return
            atomic_write(self.control, json.dumps({'action': 'run', 'id': self.instance}).encode())
            command = self.worker_command or self.manager.command(self.upstream)
            command = command+['--control-file', str(self.control), '--control-id', self.instance]
            self.child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.phase = 'ready'; self.publish(health)

    def trip(self, reason):
        try:
            with ProcessLock(self.manager.control_lock):
                state = self.manager.state()
                if not state.get('enabled'):return False
                incident = {'id': uuid.uuid4().hex, 'at': time.time(), 'reason': reason}
                try:
                    self.manager.recover_direct()
                    state = self.manager.state()
                    state.update(phase='faulted', incident=incident, restart_required=True)
                except Exception as exc:
                    state.update(phase='recovery_failed', incident={**incident, 'recovery_error': str(exc)},
                                 restart_required=True)
                    self.manager.write_state(state)
                    self.phase = 'recovery_failed'; self.publish()
                    return False
                self.manager.write_state(state)
                return True
        except RuntimeError:
            return False  # An explicit UI transaction owns the state; retry next tick.

    def drain(self, health):
        self.draining = True; self.phase = 'draining'
        if health and health.get('control_id'):
            expected = 'proxy-control-'+health['control_id']+'.json'
            if self.control.name == expected:
                atomic_write(self.control, json.dumps({'action':'drain','id':health['control_id']}).encode())
        # Legacy processes have no drain protocol; their sockets remain untouched.
        self.publish(health, legacy_wait=bool(health and not health.get('control_id')))

    def tick(self):
        state = self.manager.state()
        configured = self.manager.config()[1].get('openai_base_url') == self.manager.url
        health = self.health()
        exited = self.child is not None and self.child.poll() is not None
        if self.draining:
            if exited or (health is None and self.manager.health_state == 'refused'):
                self.phase = 'stopped'; self.publish(); return False
            self.drain(health); return True
        if state.get('enabled') and not configured:
            try:
                with ProcessLock(self.manager.control_lock):self.manager.recover_direct()
            except RuntimeError:return True
            self.drain(health);return True
        if state.get('phase') in ('off', 'faulted','failed'):
            self.drain(health); return True
        if state.get('enabled') and configured:
            reason = self.protection.observe(health, self.manager.health_state, exited)
            if reason and self.trip(reason): self.drain(health)
            elif self.phase != 'recovery_failed': self.phase = 'active'; self.publish(health)
        else:
            self.phase = 'ready' if health else 'starting'; self.publish(health)
            if exited:
                self.exit_reason = '프록시 시작에 실패했습니다.'
                self.phase = 'failed'; self.publish(); return False
            if self.clock()-self.started > 120:
                self.drain(health)
        return True

    def run(self):
        with ProcessLock(self.manager.directory/'proxy-supervisor.lock'):
            self.begin()
            if self.phase=='stopped':return
            while self.tick(): time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Cache Monitor independent proxy supervisor')
    parser.add_argument('--codex-home', required=True)
    parser.add_argument('--evidence-path', type=Path, default=default_path())
    parser.add_argument('--upstream', choices=('chatgpt','openai'), default='chatgpt')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--upstream-url',help='Fixed upstream for isolated verification')
    args = parser.parse_args()
    manager = ObserverManager(args.codex_home, args.evidence_path.parent, url=f'http://127.0.0.1:{args.port}')
    worker_command=manager.command(args.upstream)
    if args.upstream_url:worker_command+=['--upstream-url',args.upstream_url]
    supervisor = Supervisor(manager, args.upstream,worker_command=worker_command)
    try:supervisor.run()
    except RuntimeError as exc:
        # A second scheduled launch is harmless; the lifetime lock owns the worker.
        if '다른 프록시 설정 작업' in str(exc):return 0
        raise
    return 0


if __name__ == '__main__':raise SystemExit(main())
