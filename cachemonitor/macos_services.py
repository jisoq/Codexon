"""User LaunchAgents with cooperative suspension and ownership-checked updates.

launchd owns the worker itself: the small launch entry uses execve, not a
resident Popen supervisor. Configuration changes never boot out a live worker.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import time
import uuid

from .observer_state import ProcessLock


RESTART_ROLES = frozenset(('ModelObserver', 'ProxySupervisor', 'ProxyUpdate',
                          'CacheObservation', 'CacheObservationV2', 'CacheWorker', 'UsageCollector'))
SCHEMA = 1


def service_root():
    test = os.environ.get('CODEXON_SERVICE_TEST_ROOT')
    if test:
        return Path(test).expanduser().resolve() / 'services'
    from .platform_paths import app_data_dir
    return app_data_dir() / 'services'


def agent_root():
    test = os.environ.get('CODEXON_SERVICE_TEST_ROOT')
    return (Path(test).expanduser().resolve() / 'LaunchAgents' if test else
            Path.home() / 'Library' / 'LaunchAgents')


def _atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    path = Path(path)
    if not path.exists():
        return None
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise RuntimeError('서비스 설정 파일 소유권을 확인하지 못했습니다.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RuntimeError('서비스 설정 파일을 읽지 못했습니다.') from None
    if not isinstance(value, dict):
        raise RuntimeError('서비스 설정 파일 형식이 올바르지 않습니다.')
    return value


def _job(label):
    # Unlike launchctl print, this public API returns a structured dictionary.
    from ServiceManagement import SMJobCopyDictionary, kSMDomainUserLaunchd
    value = SMJobCopyDictionary(kSMDomainUserLaunchd, label)
    return dict(value) if value is not None else None


def _run(*arguments):
    result = subprocess.run(['/bin/launchctl', *arguments], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=20)
    if result.returncode:
        # launchctl can echo paths/arguments; never place its arbitrary output
        # in application diagnostics or a model-visible error.
        raise RuntimeError(f'macOS 백그라운드 서비스 작업 실패 ({arguments[0]}, {result.returncode})')
    return result


def _launcher(executable, state_path):
    command = [str(executable)]
    if not getattr(sys, 'frozen', False):
        # A source checkout may be under Documents. The independent launchd
        # process cannot inherit the terminal's TCC access to that folder.
        # Stage only our own stdlib launcher; production uses the signed app.
        entry = state_path.parent / 'launch.py'
        _atomic(entry, Path(__file__).with_name('macos_service_entry.py').read_bytes())
        return [str(getattr(sys, '_base_executable', sys.executable)), str(entry), str(state_path)]
    return command + ['--launchd-service', str(state_path)]


def _environment():
    # Do not persist API keys, tokens, auth settings, or the parent's complete
    # environment in a LaunchAgent. Codex retains ownership of its login.
    names = ('PATH', 'LANG', 'LC_CTYPE', 'CODEXON_DATA_DIR', 'CODEXON_SERVICE_TEST_ROOT')
    return {name: os.environ[name] for name in names if name in os.environ}


class LaunchAgent:
    def __init__(self, scope, role='ModelObserver'):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,63}', role):
            raise ValueError('Invalid service role')
        self.scope, self.role = str(scope), role
        self.marker = 'CacheMonitor model observer: ' + self.scope
        key = hashlib.sha256(self.scope.encode()).hexdigest()[:12]
        test = os.environ.get('CODEXON_SERVICE_TEST_ROOT')
        prefix = 'com.codexon'
        if test:
            prefix += '.qa.' + hashlib.sha256(str(Path(test).resolve()).encode()).hexdigest()[:12]
        self.label = f'{prefix}.{role.lower()}.{key}'
        self.directory = service_root() / self.label
        self.state_path = self.directory / 'job.json'
        self.plist_path = self.directory / f'{self.label}.plist'
        self.login_path = agent_root() / f'{self.label}.plist'
        self.domain = f'gui/{os.getuid()}'
        self.target = f'{self.domain}/{self.label}'

    def state(self):
        value = _read(self.state_path)
        if value is not None and (value.get('schema') != SCHEMA or value.get('label') != self.label or
                                  value.get('marker') != self.marker or value.get('role') != self.role or
                                  value.get('scope') != self.scope):
            raise RuntimeError('다른 프로그램의 백그라운드 서비스를 보존합니다.')
        return value

    def save(self, value):
        _atomic(self.state_path, json.dumps(value, ensure_ascii=False).encode())

    def loaded(self, value):
        job = _job(self.label)
        if job is not None:
            # The previous executable may still be draining when its login
            # plist is retargeted. After the next login launchd loads the new
            # owned launcher, even though the saved runtime was the old one.
            expected = [(value or {}).get(key) for key in ('loaded_launcher', 'launcher')]
            if list(job.get('ProgramArguments', ())) not in expected:
                raise RuntimeError('다른 프로그램이 같은 서비스 이름을 사용 중입니다.')
        return job

    def plist(self, value, *, login=False):
        result = dict(Label=self.label, ProgramArguments=value['launcher'],
                      WorkingDirectory=str(Path(value['command'][0]).parent),
                      RunAtLoad=bool(login and value['autostart'] and value['enabled']),
                      ProcessType='Interactive' if self.role == 'Desktop' else 'Background',
                      ThrottleInterval=60, StandardOutPath='/dev/null', StandardErrorPath='/dev/null',
                      EnvironmentVariables=value.get('environment', {}))
        if value['restart_limit']:
            result['KeepAlive'] = {'SuccessfulExit': False, 'AfterInitialDemand': True}
        if value['periodic']:
            result['StartInterval'] = 60
        return result

    def write_plists(self, value):
        _atomic(self.plist_path, plistlib.dumps(self.plist(value)))
        if value['enabled'] and value['autostart'] and not value.get('removed'):
            _atomic(self.login_path, plistlib.dumps(self.plist(value, login=True)))
        else:
            self.login_path.unlink(missing_ok=True)

    def _register(self, value, job):
        if job and job.get('PID'):
            return False
        if job:
            _run('bootout', self.target)
        _run('enable', self.target)
        _run('bootstrap', self.domain, str(self.plist_path))
        value['loaded_launcher'] = list(value['launcher'])
        value['loaded_signature'] = self.signature(value)
        self.save(value)
        if self.loaded(value) is None:
            raise RuntimeError('macOS가 백그라운드 서비스 등록을 확인하지 못했습니다.')
        return True

    @staticmethod
    def signature(value):
        return hashlib.sha256(json.dumps([value['launcher'], value['periodic'], value['restart_limit']],
                                         sort_keys=True).encode()).hexdigest()

    def configure(self, command, *, autostart=False, periodic=False, start=False):
        if not command or not all(isinstance(arg, str) and '\0' not in arg for arg in command):
            raise ValueError('Invalid service arguments')
        executable = Path(command[0]).expanduser().absolute()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError('백그라운드 서비스 실행 파일이 없습니다.')
        with ProcessLock(self.directory / 'control.lock', timeout=30):
            previous = self.state()
            job = self.loaded(previous)
            value = dict(previous or {}, schema=SCHEMA, label=self.label, marker=self.marker,
                         scope=self.scope, role=self.role, command=[str(executable), *command[1:]],
                         state_path=str(self.state_path),
                         launcher=_launcher(executable, self.state_path), enabled=True, removed=False,
                         autostart=bool(autostart), periodic=bool(periodic or self.role == 'ProxyUpdate'),
                         execution_seconds=45 if periodic else 0,
                         restart_limit=3 if self.role in RESTART_ROLES else 0, environment=_environment())
            if start or not previous or not previous.get('enabled'):
                value.update(attempts=0, restart_exhausted=False, generation=uuid.uuid4().hex, armed=True)
            if job:
                value['loaded_launcher'] = list(job['ProgramArguments'])
            self.save(value)
            self.write_plists(value)
            if not job or not job.get('PID'):
                self._register(value, job)
            if start:
                # No -k: an existing process and its connections are preserved.
                _run('kickstart', self.target)
            return self._inspect(value, self.loaded(value))

    def _inspect(self, value, job):
        if not value:
            return dict(registered=False, autostart=False, enabled=False, running=0, periodic=False)
        pid = int((job or {}).get('PID') or 0)
        return dict(registered=not value.get('removed', False), autostart=bool(value.get('autostart')),
                    periodic=bool(value.get('periodic')), enabled=bool(value.get('enabled')),
                    running=int(pid > 0), pid=pid or None, state=4 if pid else 3,
                    executable=value['command'][0], arguments=shlex.join(value['command'][1:]),
                    command=list(value['command']), restartCount=value['restart_limit'] if value['enabled'] else 0,
                    restart_exhausted=bool(value.get('restart_exhausted')),
                    execution_limit=f"PT{value.get('execution_seconds', 0)}S", backend='launchd', label=self.label)

    def inspect(self):
        value = self.state()
        return self._inspect(value, self.loaded(value))

    def deactivate(self, *, remove=False, stop=False, finish_update=False):
        with ProcessLock(self.directory / 'control.lock', timeout=30):
            value = self.state()
            job = self.loaded(value)
            if not value:
                return dict(registered=False, stopped=True, suspended=True)
            if finish_update:
                value.update(periodic=False, armed=False, execution_seconds=0)
            else:
                value.update(enabled=False, autostart=False, periodic=False, armed=False,
                             removed=bool(remove or value.get('removed')))
            self.save(value)
            self.write_plists(value)
            if stop and job and job.get('PID'):
                try:_run('kill', 'SIGTERM', self.target)
                except RuntimeError:
                    # A worker can exit between the snapshot and the signal.
                    # Accept only confirmed absence; all other refusals remain
                    # ownership failures, never a reason to signal another PID.
                    job=self.loaded(value)
                    if job and job.get('PID'):raise
            # bootout would terminate a live relay. Suspension only changes its
            # next-launch gate; existing IPC controls let it drain and exit.
            if job and not job.get('PID'):
                try:_run('bootout', self.target)
                except RuntimeError:
                    if self.loaded(value) is not None:raise
                job = None
            result = self._inspect(value, job)
            result.update(stopped=not bool(result['running']), suspended=not value['enabled'], watchdog=False)
            return result

    def call(self, operation, command=None, autostart=False, periodic=False):
        if operation == 'inspect':
            return self.inspect()
        if operation in ('configure', 'run'):
            return self.configure(command, autostart=autostart, periodic=periodic, start=operation == 'run')
        if operation not in ('remove', 'stop', 'suspend', 'finish_update'):
            raise ValueError('Unknown service operation')
        return self.deactivate(remove=operation == 'remove', stop=operation == 'stop',
                               finish_update=operation == 'finish_update')


def run_service(path):
    """Validate one owned launch receipt, then become its worker in the same PID."""
    from .macos_service_entry import run_service as execute
    return execute(path)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('state_path', type=Path)
    args = parser.parse_args()
    return run_service(args.state_path)


def installation_tasks(root, *, roles=None):
    root = Path(root).resolve()
    selected = []
    for path in service_root().glob('*/job.json'):
        value = _read(path)
        if not value or (roles is not None and value.get('role') not in roles):
            continue
        command = value.get('command') or []
        if not command or not Path(command[0]).is_absolute() or not Path(command[0]).resolve().is_relative_to(root):
            continue
        task = LaunchAgent(value.get('scope', ''), value.get('role', ''))
        if task.state_path != path or task.state() != value:
            raise RuntimeError('설치된 서비스 소유권을 확인하지 못했습니다.')
        task.loaded(value)  # A conflicting launchd label is never ours to remove.
        selected.append(task)
    return selected


def remove_installation_tasks(root, *, roles=None):
    tasks = installation_tasks(root, roles=roles)
    if any(task.inspect().get('running') for task in tasks):
        raise RuntimeError('설치된 서비스가 아직 실행 중입니다. 작업 종료를 기다려 주세요.')
    for task in tasks:
        task.deactivate(remove=True)
    return {'removed': [task.label for task in tasks]}


def retire_desktop_startups(root):
    tasks = installation_tasks(root, roles={'Desktop'})
    for task in tasks:
        task.configure(task.state()['command'], autostart=False)
    return {'retired': [task.label for task in tasks]}
