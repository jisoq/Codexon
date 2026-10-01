"""Role-specific ownership and readiness contracts for the shared updater."""
from pathlib import Path
import sys
from urllib.parse import urlsplit
from . import proxy_identity as identity
from .proxy_websocket import POLICY

def option(command, name, default=None):
    return command[command.index(name) + 1] if name in command else default

class ProxyTarget:

    def __init__(self, manager):
        self.manager = manager

    @property
    def scope(self):
        m = self.manager
        return dict(home=str(m.home.resolve()), evidence=str(m.evidence.resolve()), url=m.url, index=None)

    @property
    def locks(self):
        m = self.manager
        return [m.directory / 'proxy-supervisor.lock']

    def enabled(self):
        m = self.manager
        from .app_services import suspended
        if suspended(m):
            return False
        return m.config()[1].get('openai_base_url') == m.url and m.state().get('enabled')

    def validate_command(self, command):
        m = self.manager
        if not any((flag in command for flag in ('--model-proxy', '--proxy-supervisor'))):
            raise RuntimeError('교체 대상의 실행 역할이 다릅니다.')
        for name, value in (('--codex-home', m.home), ('--evidence-path', m.evidence)):
            if Path(option(command, name, '')).resolve() != value.resolve():
                raise RuntimeError('교체 대상의 저장 경로가 다릅니다.')
        if int(option(command, '--port', 8768)) != urlsplit(m.url).port:
            raise RuntimeError('교체 대상의 포트가 다릅니다.')
        from .retired_cache import legacy_command
        if legacy_command(command):raise RuntimeError('구버전 연결 전환 필요')

    def capture(self, health):
        m = self.manager
        worker = identity.process_identity(health['pid'])
        if not worker:
            raise RuntimeError('기존 프록시가 종료되었습니다.')
        if identity.listener_pids(m.url) != [worker['pid']]:
            raise RuntimeError('프록시 리스너 소유권이 다릅니다.')
        command = identity.process_command(worker['pid'])
        self.validate_command(command)
        receipt_required = health.get('storage_flush_receipt', health.get('lifecycle_revision', 0) >= 2 and '--managed' in command)
        if Path(command[0]).resolve() != Path(worker['executable']).resolve():
            raise RuntimeError('실행 명령과 프로세스 경로가 다릅니다.')
        processes = [worker]
        root = m.runtime().get('pid') or worker['pid']
        if root != worker['pid']:
            supervisor = identity.process_identity(root)
            if not supervisor:
                raise RuntimeError('기존 supervisor 소유권 확인 실패')
            command = identity.process_command(root)
            self.validate_command(command)
            if '--proxy-supervisor' not in command:
                raise RuntimeError('기존 supervisor 역할 확인 실패')
            processes.append(supervisor)
        if not all((identity.same_process(p) for p in processes)):
            raise RuntimeError('확인 도중 프로세스가 바뀌었습니다.')
        return dict(processes=processes, command=command, version=health['version'], instance=health['instance'], executable_sha256=identity.digest(command[0]), state_phase=m.state().get('phase'), lifecycle_revision=health.get('lifecycle_revision', 0), flush_receipt_required=receipt_required, control_id=health.get('control_id'), registration=m.task.inspect(), role='observer', upstream=option(command, '--upstream-url') or {'chatgpt': 'https://chatgpt.com/backend-api/codex', 'openai': 'https://api.openai.com/v1'}[option(command, '--upstream', 'chatgpt')])

    def replacement(self, source):
        command = list(source['command'])
        command[0] = sys.executable
        for name in ('--control-file', '--control-id'):
            if name in command:
                pos = command.index(name)
                del command[pos:pos + 2]
        if '--proxy-supervisor' in command:
            command[command.index('--proxy-supervisor')] = '--model-proxy'
        if '--managed' not in command:
            command.append('--managed')
        self.validate_command(command)
        return command

    def exited(self, source):
        return all((not identity.same_process(p) for p in source['processes']))

    def stopped(self, source):
        if not (self.exited(source) and identity.port_free(self.manager.url) and identity.locks_free(self.locks)):
            return False
        from .observer_state import read_json
        receipt_required = source.get('flush_receipt_required', source.get('lifecycle_revision', 0) >= 2 and '--managed' in source.get('command', []))
        if receipt_required:
            receipt = read_json(self.manager.directory / ('proxy-control-' + source['control_id'] + '.json'))
            if receipt.get('action') != 'stopped' or not receipt.get('storage_flushed'):
                return False
        return True

    def ready(self, health, source, command, version, distribution=None, *, read_only=False):
        if not health or health.get('version') != version or health.get('status') != 'ok' or health.get('draining'):
            return False
        if health.get('instance') == source['instance']:
            return False
        worker = identity.process_identity(health['pid'])
        if not worker or Path(worker['executable']).resolve() != Path(command[0]).resolve():
            return False
        actual = identity.process_command(worker['pid'])
        self.validate_command(actual)
        if not distribution:
            return self.manager.runtime().get('phase') in ('active', 'ready')
        m = self.manager
        expected = dict(role=source['role'], home=str(m.home), evidence_path=str(m.evidence), index_path=None, upstream=source['upstream'], listen_url=m.url, executable_sha256=distribution['sha256'], deployment_commit=distribution['commit'], process_created=worker['created'], websocket_policy=vars(POLICY))
        if any((health.get(k) != v for k, v in expected.items())):
            return False
        if identity.digest(worker['executable']) != distribution['sha256']:
            return False
        return health.get('lifecycle') == 'managed' and self.manager.runtime().get('phase') in ('active', 'ready')
