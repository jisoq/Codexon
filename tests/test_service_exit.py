import json
import sys
from types import SimpleNamespace

import pytest

from cachemonitor import app_services as services
from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json
from cachemonitor.proxy_drain import ProxyDrain
from cachemonitor.proxy_target import ProxyTarget
from cachemonitor.shutdown_state import ExitControl


@pytest.fixture
def exit_service(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    manager = ObserverManager(home, tmp_path / 'data', url='http://127.0.0.1:19888')
    manager.set_url(manager.url)
    from cachemonitor.model_evidence import home_key
    manager.write_state(dict(home=home_key(home), enabled=True, phase='active', previous_url=None))
    current = dict(instance='owned', version='old', control_id='a' * 32,
                   supports_force_shutdown=True, websocket_states={'responding': 0, 'unknown': 0})
    runtime = dict(alive=True, flushed=True, resources_free=True, health=current,
                   suspended=[], commands=[], now=0., updater_running=False)

    class Task:
        def __init__(self, *args, role='proxy', **kwargs):
            self.role = role

        def inspect(self):
            return dict(running=int(self.role == 'ProxyUpdate' and runtime['updater_running']))

        def suspend(self):
            journal = read_json(services.session_path(manager))
            assert journal.get('committed') and journal.get('operation_id')
            runtime['suspended'].append(self.role)

        def start(self, *args, **kwargs):
            pytest.fail('exit must not launch a process')

    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask', Task)
    manager.task = Task()
    manager.legacy_task = Task(role='legacy')
    monkeypatch.setattr(manager, 'cleanup_legacy_check', lambda: None)
    command = [sys.executable, '--model-proxy', '--managed', '--codex-home', str(home),
               '--evidence-path', str(manager.evidence), '--port', '19888']

    def capture(health):
        return dict(instance=health['instance'], control_id=health['control_id'], command=command,
                    processes=[dict(pid=123, created=1, executable=sys.executable)],
                    version=health['version'], flush_receipt_required=True)

    def health(**kwargs):
        path = manager.directory / ('proxy-control-' + current['control_id'] + '.json')
        action = read_json(path).get('action')
        if action:
            runtime['commands'].append(action)
        if action in ('drain', 'force_shutdown'):
            runtime['alive'] = False
        manager.health_state = 'healthy' if runtime['alive'] else 'refused'
        return current if runtime['alive'] else None

    def sleep(seconds):
        runtime['now'] += seconds

    monkeypatch.setattr(manager, 'health', health)
    monkeypatch.setattr(services.identity, 'process_identity',
                        lambda pid: dict(pid=pid, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'locks_free', lambda _: runtime['resources_free'])
    monkeypatch.setattr(services.identity, 'port_free', lambda _: not runtime['alive'] and runtime['resources_free'])
    owner = services.AppServices(manager, [], collection=False, clock=lambda: runtime['now'], sleep=sleep)
    owner.active = True
    monkeypatch.setattr(owner.target, 'capture', capture)
    monkeypatch.setattr(owner.target, 'exited', lambda _: not runtime['alive'])
    monkeypatch.setattr(owner.target, 'released', lambda _: not runtime['alive'] and runtime['resources_free'])
    monkeypatch.setattr(owner.target, 'stopped',
                        lambda _: not runtime['alive'] and runtime['resources_free'] and runtime['flushed'])
    return SimpleNamespace(manager=manager, owner=owner, runtime=runtime, source=capture(current),
                           control_path=manager.directory / ('proxy-control-' + current['control_id'] + '.json'))


@pytest.mark.parametrize('pending', ['responding', 'unknown'])
def test_cancelling_pending_work_keeps_route_worker_and_usable_services(exit_service, pending):
    case = exit_service
    control = ExitControl()
    progress = []
    case.runtime['health']['websocket_states'][pending] = 1

    def cancel(_):
        assert case.manager.config()[1]['openai_base_url'] == case.manager.url
        assert not case.control_path.exists() and not case.runtime['suspended']
        assert read_json(services.session_path(case.manager))['phase'] == 'preparing'
        assert progress[-1].can_cancel and progress[-1].can_force
        assert getattr(progress[-1], pending if pending == 'responding' else 'unknown') == 1
        assert control.request_cancel()

    case.owner.sleep = cancel
    result = case.owner.stop(control=control, publish=progress.append)
    assert result.status == 'cancelled' and not result.committed
    assert case.runtime['alive'] and not case.control_path.exists()
    assert not case.runtime['suspended'] and case.manager.state()['enabled']
    assert case.owner.active and not case.owner.closing.is_set()
    assert read_json(services.session_path(case.manager))['phase'] == 'active'
    services.require_running(case.manager)


@pytest.mark.parametrize('edit', ['off', 'custom-route'])
def test_cancel_preserves_external_route_and_off_intent(exit_service, edit):
    case = exit_service
    control = ExitControl()
    case.runtime['health']['websocket_states']['responding'] = 1
    route = None if edit == 'off' else 'https://example.invalid/v1'

    def edit_and_cancel(_):
        if edit == 'off':
            services.disable_resume(case.manager)
        case.manager.set_url(route)
        state = case.manager.state()
        state.update(enabled=False, phase='off', external_edit=True)
        case.manager.write_state(state)
        control.request_cancel()

    case.owner.sleep = edit_and_cancel
    result = case.owner.stop(control=control)
    assert result.status == 'cancelled'
    assert case.manager.config()[1].get('openai_base_url') == route
    assert case.manager.state()['external_edit'] and not case.manager.state()['enabled']
    if edit == 'off':
        assert read_json(services.session_path(case.manager))['resume'] is False


def test_cancellation_winning_just_before_cutover_prevents_every_stop_effect(exit_service, monkeypatch):
    case = exit_service
    control = ExitControl()
    save = case.owner.save

    def cancel_after_capture(**changes):
        result = save(**changes)
        if changes.get('source'):
            assert control.request_cancel()
        return result

    monkeypatch.setattr(case.owner, 'save', cancel_after_capture)
    result = case.owner.stop(control=control)
    assert result.status == 'cancelled' and not result.committed
    assert case.runtime['alive'] and not case.runtime['suspended'] and not case.control_path.exists()
    assert case.manager.config()[1]['openai_base_url'] == case.manager.url


def test_cutover_winning_persists_before_route_change_and_rejects_cancel(exit_service, monkeypatch):
    case = exit_service
    control = ExitControl()
    set_url = case.manager.set_url
    cancellation = []

    def restore(route):
        journal = read_json(services.session_path(case.manager))
        assert journal['committed'] and journal['operation_id']
        cancellation.append(control.request_cancel())
        return set_url(route)

    monkeypatch.setattr(case.manager, 'set_url', restore)
    result = case.owner.stop(control=control)
    assert result.status == 'completed' and result.committed
    assert cancellation == [False] and not control.cancelled
    assert not case.runtime['alive'] and 'drain' in case.runtime['commands']
    assert 'pause' not in case.runtime['commands']
    assert read_json(services.session_path(case.manager))['phase'] == 'stopped'


def test_failed_commit_persistence_keeps_service_and_route_usable(exit_service, monkeypatch):
    case = exit_service
    save = case.owner.save

    def fail_commit(**changes):
        if changes.get('committed'):
            raise OSError('journal unavailable')
        return save(**changes)

    monkeypatch.setattr(case.owner, 'save', fail_commit)
    result = case.owner.stop()
    assert result.status == 'failed' and not result.committed and 'journal unavailable' in result.error
    assert case.runtime['alive'] and not case.runtime['suspended']
    assert case.manager.config()[1]['openai_base_url'] == case.manager.url
    assert case.owner.active and not case.owner.closing.is_set()
    services.require_running(case.manager)


def test_unsupported_force_fails_before_committing_or_draining(exit_service):
    case = exit_service
    case.runtime['health']['supports_force_shutdown'] = False
    control = ExitControl()
    assert control.request_force()
    result = case.owner.stop(control=control)
    assert result.status == 'failed' and not result.committed
    assert case.runtime['alive'] and not case.control_path.exists() and not case.runtime['suspended']
    assert case.manager.config()[1]['openai_base_url'] == case.manager.url
    assert case.owner.active and not case.owner.closing.is_set()


def test_explicit_force_only_targets_captured_control(exit_service):
    case = exit_service
    case.runtime['health']['websocket_states']['unknown'] = 1
    result = case.owner.stop(lambda: True)
    assert result.status == 'completed' and result.committed
    assert case.runtime['commands'] == ['force_shutdown']
    command = read_json(case.control_path)
    assert command == dict(action='force_shutdown', id=case.source['control_id'])


def test_failure_after_cutover_restores_controls_without_reversing_route(exit_service, monkeypatch):
    case = exit_service
    restore = case.manager.set_url

    def fail_after_route_change(route):
        restore(route)
        raise OSError('route changed but response was lost')

    monkeypatch.setattr(case.manager, 'set_url', fail_after_route_change)
    result = case.owner.stop()
    assert result.status == 'failed' and result.committed
    assert case.manager.config()[1].get('openai_base_url') is None
    assert case.runtime['alive'] and not case.control_path.exists()
    assert case.owner.active and not case.owner.closing.is_set()
    assert read_json(services.session_path(case.manager))['phase'] == 'failed'
    assert case.owner.recovery_deferred


def test_local_finalization_failure_releases_controls_without_auto_recovery(exit_service, monkeypatch):
    case = exit_service
    assert case.owner.stop().status == 'completed'
    journal = services.session_path(case.manager).read_bytes()
    route = case.manager.config()[0]
    case.owner.release_exit()
    assert case.owner.active and not case.owner.closing.is_set()
    monkeypatch.setattr(case.manager, 'status', lambda: dict(phase='off', health=None, probe_state='refused'))
    monkeypatch.setattr('cachemonitor.install_cleanup.after_services', lambda _: None)
    for at in (0, 60, 120, 1000):
        case.runtime['now'] = at
        assert case.owner.execute('ensure')['phase'] == 'off'
    assert services.session_path(case.manager).read_bytes() == journal
    assert case.manager.config()[0] == route
    monkeypatch.setattr(services, 'suspended', lambda _: False)
    monkeypatch.setattr(case.manager, 'resume', lambda: dict(explicit_resume=True))
    assert case.owner.execute('resume')['explicit_resume']
    assert not case.owner.recovery_deferred


def test_cutover_preserves_external_route_change_and_off_intent(exit_service):
    case = exit_service
    case.runtime['health']['websocket_states']['responding'] = 1
    route = 'https://example.invalid/v1'

    def change_route(_):
        case.manager.set_url(route)
        services.disable_resume(case.manager)
        case.runtime['health']['websocket_states']['responding'] = 0

    case.owner.sleep = change_route
    result = case.owner.stop()
    assert result.status == 'completed'
    assert case.manager.config()[1].get('openai_base_url') == route
    assert read_json(services.session_path(case.manager))['resume'] is False


def test_route_edit_during_task_suspension_is_preserved(exit_service, monkeypatch):
    case = exit_service
    route = 'https://example.invalid/v1'
    suspend = case.manager.task.suspend

    def suspend_and_edit():
        suspend()
        case.manager.set_url(route)

    monkeypatch.setattr(case.manager.task, 'suspend', suspend_and_edit)
    result = case.owner.stop()
    assert result.status == 'completed'
    assert case.manager.config()[1].get('openai_base_url') == route


def test_remembered_source_survives_uncaptured_proxy_crash(exit_service):
    case = exit_service
    case.owner.proxy_source = case.source
    case.runtime.update(alive=False, flushed=False)
    result = case.owner.stop()
    journal = read_json(services.session_path(case.manager))
    assert result.status == 'completed' and result.warning
    assert journal['source'] == case.source and journal['resume'] is True
    assert journal['shutdown_incomplete'] is True
    assert journal['shutdown_incomplete_instance'] == case.source['instance']
    assert not case.control_path.exists()


def test_live_source_takes_precedence_over_remembered_source(exit_service):
    case = exit_service
    case.owner.proxy_source = {**case.source, 'instance': 'obsolete'}
    result = case.owner.stop()
    assert result.status == 'completed'
    assert read_json(services.session_path(case.manager))['source']['instance'] == case.source['instance']


def test_matching_saved_update_source_recovers_missing_live_health(exit_service):
    case = exit_service
    case.runtime.update(alive=False, flushed=False)
    services.atomic_write(case.manager.directory / 'proxy-update.json', json.dumps(
        dict(scope=case.owner.target.scope, phase='failed', source=case.source)).encode())
    result = case.owner.stop()
    assert result.status == 'completed' and result.warning
    assert read_json(services.session_path(case.manager))['source'] == case.source


def test_missing_source_retains_unresolved_resume_intent(exit_service):
    case = exit_service
    case.runtime['alive'] = False
    assert case.owner.stop().status == 'completed'
    before = read_json(services.session_path(case.manager))
    assert before['resume'] and not before['source']
    with pytest.raises(RuntimeError, match='실행 정보'):
        services.resume_proxy(case.manager)
    after = read_json(services.session_path(case.manager))
    assert after['resume'] and after['phase'] == 'stopped'


def test_resume_leaves_a_live_exit_preparation_untouched(exit_service, monkeypatch):
    case = exit_service
    case.owner.save(phase='preparing', committed=False, operation_id='preparing',
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'same_process', lambda _: True)
    assert services.resume_proxy(case.manager)
    assert read_json(services.session_path(case.manager))['phase'] == 'preparing'
    assert case.runtime['alive'] and not case.runtime['suspended'] and not case.control_path.exists()
    assert case.manager.config()[1]['openai_base_url'] == case.manager.url


def test_dead_preparation_owner_recovers_without_draining_or_route_reversal(exit_service, monkeypatch):
    case = exit_service
    case.owner.save(phase='preparing', committed=False, operation_id='preparing', resume=True,
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    assert not services.resume_proxy(case.manager)
    assert read_json(services.session_path(case.manager))['phase'] == 'active'
    assert case.runtime['alive'] and not case.runtime['suspended'] and not case.control_path.exists()
    assert case.manager.config()[1]['openai_base_url'] == case.manager.url


def test_unknown_preparation_owner_cannot_authorize_recovery(exit_service, monkeypatch):
    case = exit_service
    case.owner.save(phase='preparing', committed=False, operation_id='preparing',
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))

    def unknown(_):
        raise OSError('process ownership unavailable')

    monkeypatch.setattr(services.identity, 'same_process', unknown)
    with pytest.raises(OSError, match='process ownership unavailable'):
        services.resume_proxy(case.manager)
    assert read_json(services.session_path(case.manager))['phase'] == 'preparing'
    assert case.runtime['alive'] and not case.control_path.exists()


def test_dead_retry_preparation_preserves_earlier_committed_resume_intent(exit_service, monkeypatch):
    case = exit_service
    case.manager.set_url(None)
    case.owner.save(phase='preparing', committed=False, operation_id='retry', resume=True, source=None,
                    direct_url=None, preparation_previous_phase='stopped', preparation_previous_committed=True,
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    with pytest.raises(RuntimeError, match='실행 정보'):
        services.resume_proxy(case.manager)
    journal = read_json(services.session_path(case.manager))
    assert journal['phase'] == 'stopped' and journal['committed'] and journal['resume']
    assert not case.runtime['suspended'] and not case.control_path.exists()


def test_another_stop_cannot_take_over_live_preparation(exit_service, monkeypatch):
    case = exit_service
    case.owner.save(phase='preparing', committed=False, operation_id='another-preparation',
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'same_process', lambda _: True)
    result = case.owner.stop()
    assert result.status == 'failed' and not result.committed
    assert read_json(services.session_path(case.manager))['operation_id'] == 'another-preparation'
    assert case.runtime['alive'] and not case.runtime['suspended'] and not case.control_path.exists()


def test_exit_records_missing_flush_after_physical_release(exit_service):
    case = exit_service
    case.runtime['flushed'] = False
    result = case.owner.stop()
    assert result.status == 'completed' and result.warning
    journal = read_json(services.session_path(case.manager))
    assert journal['shutdown_incomplete'] and journal['shutdown_incomplete_instance'] == case.source['instance']
    assert read_json(case.control_path) == dict(action='drain', id=case.source['control_id'])


def test_replacement_drain_still_requires_flush_receipt(exit_service):
    case = exit_service
    case.runtime['flushed'] = False
    with pytest.raises(RuntimeError, match='종료 확인 시간 초과'):
        ProxyDrain(case.manager, case.owner.target, clock=case.owner.clock, sleep=case.owner.sleep).run(case.source)
    assert not case.runtime['alive']
    assert not read_json(case.control_path).get('storage_flushed')


def test_another_instances_missing_flush_warning_cannot_authorize_resume(exit_service, monkeypatch):
    case = exit_service
    case.manager.set_url(None)
    case.owner.save(phase='stopped', resume=True, state=case.manager.state(), direct_url=None,
                    source=case.source, shutdown_incomplete=True, shutdown_incomplete_instance='another-instance')
    monkeypatch.setattr(ProxyTarget, 'stopped', lambda *_: False)
    monkeypatch.setattr(ProxyTarget, 'released', lambda *_: True)
    with pytest.raises(RuntimeError, match='이전 프로세스 종료'):
        services.resume_proxy(case.manager)


@pytest.mark.parametrize('phase', ['preparing', 'closing'])
def test_exit_recovery_owner_proves_gui_exit_without_mutating_proxy(exit_service, monkeypatch, phase):
    case = exit_service
    case.owner.save(phase=phase, operation_id='interrupted-exit', resume=True,
                    preparation_owner=dict(pid=123, created=1, executable=sys.executable))
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    journal = services.session_path(case.manager).read_bytes()
    route = case.manager.config()[0]
    proof = services.exit_recovery_owner(case.manager)
    assert proof['owner_state'] == 'dead' and proof['operation_id'] == 'interrupted-exit'
    assert proof['phase'] == phase
    assert services.session_path(case.manager).read_bytes() == journal
    assert case.manager.config()[0] == route
    assert case.runtime['alive'] and not case.runtime['suspended'] and not case.control_path.exists()


@pytest.mark.parametrize('owner', [None, {}, {'pid': 123}, {'pid': 123, 'created': 1, 'executable': ''}])
def test_exit_recovery_never_treats_missing_owner_identity_as_exit(exit_service, monkeypatch, owner):
    case = exit_service
    case.owner.save(phase='closing', operation_id='legacy-exit', preparation_owner=owner)
    monkeypatch.setattr(services.identity, 'same_process', lambda _: pytest.fail('Incomplete owner cannot prove exit'))
    assert services.exit_recovery_owner(case.manager)['owner_state'] == 'unknown'


def test_exit_recovery_proof_excludes_another_connection_scope(exit_service, monkeypatch):
    case = exit_service
    other_scope = {**case.owner.target.scope, 'url': 'http://127.0.0.1:19889'}
    services.atomic_write(services.session_path(case.manager), json.dumps(dict(
        phase='closing', scope=other_scope, operation_id='other-connection',
        preparation_owner=dict(pid=123, created=1, executable=sys.executable))).encode())
    monkeypatch.setattr(services.identity, 'same_process', lambda _: pytest.fail('Different scope is not this exit'))
    assert services.exit_recovery_owner(case.manager) is None
