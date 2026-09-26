import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

import pytest

from cachemonitor import macos_services as services

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS user service contract')

@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEXON_SERVICE_TEST_ROOT', str(tmp_path / 'isolated'))
    jobs = {}
    calls = []
    def run(*args):
        calls.append(args)
        if args[0] == 'bootstrap':
            job = plistlib.loads(Path(args[2]).read_bytes())
            jobs[job['Label']] = job
        elif args[0] == 'bootout':
            jobs.pop(args[1].rsplit('/', 1)[1], None)
        return subprocess.CompletedProcess(args, 0, b'', b'')
    monkeypatch.setattr(services, '_run', run)
    monkeypatch.setattr(services, '_job', lambda name: jobs.get(name))
    return jobs, calls


def test_configuration_is_not_launch_and_login_setting_does_not_kill_live_worker(launchd, tmp_path):
    jobs, calls = launchd
    service = services.LaunchAgent(str(tmp_path), 'Desktop')
    command = [sys.executable, str(tmp_path / 'script with spaces.py')]
    service.configure(command, autostart=True)
    assert not any(call[0] == 'kickstart' for call in calls)
    assert not jobs[service.label]['RunAtLoad']
    assert plistlib.loads(service.login_path.read_bytes())['RunAtLoad']
    jobs[service.label]['PID'] = 123
    calls.clear()
    service.configure(command, autostart=False)
    assert not calls and not service.login_path.exists()
    state = service.inspect()
    assert state['running'] == 1 and state['restartCount'] == 0 and not state['autostart']


def test_suspend_remove_and_reconfigure_preserve_running_relay(launchd, tmp_path):
    jobs, calls = launchd
    service = services.LaunchAgent(str(tmp_path), 'CacheWorker')
    command = [sys.executable, '-c', 'pass']
    service.configure(command, start=True)
    assert ('kickstart', service.target) in calls
    jobs[service.label]['PID'] = 123
    calls.clear()
    service.deactivate()
    assert calls == [] and service.inspect()['running'] == 1 and not service.inspect()['enabled']
    service.deactivate(remove=True)
    assert calls == [] and not service.inspect()['registered']
    service.configure([*command, 'new argument'], start=True)
    assert calls == [('kickstart', service.target)]
    assert service.inspect()['command'][-1] == 'new argument'


def test_foreign_loaded_job_is_never_controlled(launchd, tmp_path):
    jobs, calls = launchd
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    jobs[service.label] = {'ProgramArguments': ['/other/program'], 'PID': 123}
    with pytest.raises(RuntimeError, match='같은 서비스 이름'):
        service.configure([sys.executable, '-c', 'pass'], start=True)
    assert calls == [] and not service.state_path.exists()


def test_login_adopts_retargeted_owned_launcher_after_live_update(launchd, tmp_path, monkeypatch):
    jobs, calls = launchd
    monkeypatch.setattr(services, '_launcher', lambda exe, path: [str(exe), '--launchd-service', str(path)])
    programs=[]
    for version in ('old','new','next'):
        path=tmp_path/version;path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o700)
        programs.append(str(path))
    service=services.LaunchAgent(str(tmp_path),'Desktop')
    service.configure([programs[0]],autostart=True)
    jobs[service.label]['PID']=123
    service.configure([programs[1]],autostart=True)
    assert jobs[service.label]['ProgramArguments'][0]==programs[0]
    jobs[service.label]=plistlib.loads(service.login_path.read_bytes())
    jobs[service.label]['PID']=456
    assert service.inspect()['running']==1
    service.configure([programs[2]],autostart=True)
    assert service.state()['loaded_launcher'][0]==programs[1]
    assert service.inspect()['running']==1


def test_restarts_are_bounded_and_suspension_blocks_late_launch(launchd, tmp_path, monkeypatch):
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    service.configure([sys.executable, '-c', 'pass'], start=True)
    launched = []
    class Executed(Exception):
        pass
    def execute(path, args, env):
        launched.append(list(args))
        raise Executed()
    monkeypatch.setattr(services.os, 'execve', execute)
    for _ in range(4):
        with pytest.raises(Executed):
            services.run_service(service.state_path)
    assert services.run_service(service.state_path) == 0
    assert len(launched) == 4 and service.inspect()['restart_exhausted']
    service.configure([sys.executable, '-c', 'pass'], start=True)
    assert not service.inspect()['restart_exhausted']
    service.deactivate()
    assert services.run_service(service.state_path) == 0 and len(launched) == 4


def test_healthy_interval_resets_crash_budget_and_only_health_checks_have_timeout(launchd, tmp_path, monkeypatch):
    from cachemonitor import macos_service_entry as entry
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    service.configure([sys.executable, '-c', 'pass'])
    value = service.state()
    value.update(attempts=4,last_launch=time.time()-600)
    service.save(value)
    calls = []
    monkeypatch.setattr(entry.os, 'execve', lambda *args: calls.append(args))
    services.run_service(service.state_path)
    assert len(calls) == 1 and service.state()['attempts'] == 1
    check = services.LaunchAgent(str(tmp_path), 'ConnectionCheck')
    check.configure([sys.executable, '-c', 'pass'], periodic=True)
    assert check.inspect()['execution_limit'] == 'PT45S'
    update = services.LaunchAgent(str(tmp_path), 'ProxyUpdate')
    update.configure([sys.executable, '-c', 'pass'])
    assert update.inspect()['periodic'] and update.inspect()['execution_limit'] == 'PT0S'


def test_service_environment_never_persists_parent_credentials(launchd, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'isolated-test-secret')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN', 'isolated-test-token')
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    service.configure([sys.executable, '-c', 'pass'])
    assert 'OPENAI_API_KEY' not in service.state()['environment']
    assert 'CODEX_ACCESS_TOKEN' not in service.state()['environment']
    assert 'isolated-test-secret' not in service.plist_path.read_text()


def test_stop_tolerates_only_confirmed_exit_between_inspection_and_signal(launchd, tmp_path, monkeypatch):
    jobs, _ = launchd
    service=services.LaunchAgent(str(tmp_path),'UsageCollector')
    service.configure([sys.executable,'-c','pass'])
    jobs[service.label]['PID']=123
    def failed(*args):raise RuntimeError('Simulated launchctl failure')
    monkeypatch.setattr(services,'_run',failed)
    with pytest.raises(RuntimeError,match='launchctl'):service.deactivate(stop=True)
    def exited(*args):
        jobs.pop(service.label,None)
        raise RuntimeError('Simulated launchctl exit race')
    monkeypatch.setattr(services,'_run',exited)
    assert service.deactivate(stop=True)['stopped']


def test_update_watchdog_finishes_without_terminating_updater(launchd, tmp_path):
    jobs, calls = launchd
    service = services.LaunchAgent(str(tmp_path), 'ProxyUpdate')
    service.configure([sys.executable, '-c', 'pass'], start=True)
    assert service.inspect()['periodic']
    jobs[service.label]['PID'] = 123
    calls.clear()
    service.deactivate(finish_update=True)
    assert not calls and not service.inspect()['periodic']
    assert service.inspect()['execution_limit'] == 'PT0S'
    assert services.run_service(service.state_path) == 0


def test_uninstall_removes_only_owned_idle_jobs_and_preserves_live_services(launchd, tmp_path):
    jobs, calls = launchd
    own = tmp_path / 'installed'
    own.mkdir()
    executable = own / 'Codexon'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    first = services.LaunchAgent('first', 'UsageCollector')
    second = services.LaunchAgent('second', 'ProxyUpdate')
    unrelated = services.LaunchAgent('third', 'Desktop')
    first.configure([str(executable), '--usage-collector'])
    second.configure([str(executable), '--proxy-update'])
    unrelated.configure([sys.executable, '-c', 'pass'])
    jobs[second.label]['PID'] = 123
    calls.clear()
    with pytest.raises(RuntimeError, match='실행 중'):
        services.remove_installation_tasks(own)
    assert calls == [] and first.inspect()['registered']
    jobs[second.label].pop('PID')
    result = services.remove_installation_tasks(own)
    assert set(result['removed']) == {first.label, second.label}
    assert unrelated.inspect()['registered']


@pytest.mark.skipif(sys.platform != 'darwin' or os.environ.get('CODEXON_RUN_LAUNCHD_TESTS') != '1',
                    reason='Opt-in isolated user LaunchAgent integration')
def test_launchd_worker_survives_launcher_and_is_cooperatively_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEXON_SERVICE_TEST_ROOT', str(tmp_path / 'isolated'))
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    marker, stop = tmp_path / 'pid.json', tmp_path / 'stop'
    script = tmp_path / 'worker.py'
    script.write_text('import json,os,sys,time\nfrom pathlib import Path\n'
                      'Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),"version":sys.argv[3]}))\n'
                      'while not Path(sys.argv[2]).exists():time.sleep(.05)\n')
    command = [str(getattr(sys, '_base_executable', sys.executable)), str(script), str(marker), str(stop), 'first']
    def wait(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        raise AssertionError('Isolated LaunchAgent did not reach expected state')
    try:
        service.configure(command)
        time.sleep(.3)
        assert not marker.exists(), 'configure must not start a worker'
        launcher = tmp_path / 'launcher.py'
        root = str(Path(__file__).resolve().parents[1])
        launcher.write_text('import sys\nsys.path.insert(0,'+repr(root)+')\n'
                            'from cachemonitor.macos_services import LaunchAgent\n'
                            f'LaunchAgent({str(tmp_path)!r},"UsageCollector").configure({command!r},start=True)\n')
        subprocess.run([sys.executable, str(launcher)], check=True, timeout=30)
        wait(marker.exists)
        first = json.loads(marker.read_text())
        from cachemonitor.proxy_identity import process_identity, process_command
        owned = process_identity(first['pid'])
        assert owned and process_command(first['pid'])[1:] == command[1:]
        assert service.inspect()['pid'] == first['pid']
        service.configure([*command[:-1], 'second'])
        assert service.inspect()['pid'] == first['pid']
        service.deactivate()
        stop.touch()
        wait(lambda: not service.inspect()['running'])
        marker.unlink()
        stop.unlink()
        service.configure([*command[:-1], 'second'], start=True)
        wait(marker.exists)
        assert json.loads(marker.read_text())['version'] == 'second'
    finally:
        service.deactivate()
        stop.touch()
        try:
            wait(lambda: not service.inspect()['running'])
        finally:
            # This PID belongs only to the synthetic fixture, never a relay.
            if service.inspect()['running']:
                service.deactivate(stop=True)
                wait(lambda: not service.inspect()['running'])
        service.deactivate(remove=True)
        assert services._job(service.label) is None


@pytest.mark.skipif(sys.platform != 'darwin' or os.environ.get('CODEXON_RUN_LAUNCHD_TESTS') != '1',
                    reason='Opt-in isolated user LaunchAgent integration')
def test_launchd_restarts_crashed_worker_and_alarm_survives_exec(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEXON_SERVICE_TEST_ROOT', str(tmp_path / 'isolated'))
    original = services.LaunchAgent.plist
    def fast_retry(self, value, **kwargs):
        result = original(self, value, **kwargs)
        result['ThrottleInterval'] = 1
        return result
    monkeypatch.setattr(services.LaunchAgent, 'plist', fast_retry)
    service = services.LaunchAgent(str(tmp_path), 'UsageCollector')
    marker = tmp_path / 'launches.json'
    worker = tmp_path / 'crash.py'
    worker.write_text('import json,os,sys,time\nfrom pathlib import Path\n'
                      'p=Path(sys.argv[1])\na=json.loads(p.read_text()) if p.exists() else []\n'
                      'a.append(os.getpid());p.write_text(json.dumps(a))\n'
                      'sys.exit(7) if len(a)<3 else time.sleep(20)\n')
    command = [str(getattr(sys, '_base_executable', sys.executable)), str(worker), str(marker)]
    def wait(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():return
            time.sleep(.05)
        raise AssertionError('Isolated launchd crash recovery did not complete')
    try:
        service.configure(command, start=True)
        wait(lambda: marker.exists() and len(json.loads(marker.read_text())) >= 3)
        pids = json.loads(marker.read_text())
        assert len(set(pids)) == 3 and service.inspect()['pid'] == pids[-1]
        service.deactivate(stop=True)
        wait(lambda: not service.inspect()['running'])
        service.deactivate(remove=True)
        assert services._job(service.label) is None
        # The bounded periodic role must time out after exec, without another
        # supervisor process that could outlive suspension or replacement.
        check = services.LaunchAgent(str(tmp_path), 'ConnectionCheck')
        check.configure([command[0], '-c', 'import time;time.sleep(30)'], periodic=True)
        value = check.state();value['execution_seconds'] = 1;check.save(value)
        services._run('kickstart', check.target)
        wait(lambda: check.inspect()['running'])
        wait(lambda: not check.inspect()['running'])
        assert check.state()['attempts'] == 1
        check.deactivate(remove=True)
        assert services._job(check.label) is None
    finally:
        for task in (service, locals().get('check')):
            if task is None:continue
            task.deactivate(stop=True)
            wait(lambda: not task.inspect()['running'])
            task.deactivate(remove=True)
