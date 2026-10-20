import json
import sys
import time
import tomllib
from types import SimpleNamespace

import pytest

from cachemonitor.connection_recovery import inspect, restore, target, assess
from cachemonitor.observer_control import ObserverManager
from cachemonitor.model_evidence import home_key
from cachemonitor.observer_state import read_json




@pytest.mark.parametrize('phase',['queued','waiting','stopping','starting','verifying','rollback'])
def test_intentional_update_does_not_report_listener_gap_as_failure(phase):
    result=assess(dict(configured=True,probe_state='refused',update=dict(phase=phase,message='current phase')))
    assert result['code']=='updating' and not result['confirmed'] and result['detail']=='current phase'


@pytest.fixture
def manager(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    manager=ObserverManager(home,tmp_path/'data',url='http://127.0.0.1:18991')
    manager.config_path.write_text('# preserve me\nopenai_base_url="http://127.0.0.1:18991"\nmodel="keep"\n')
    task=SimpleNamespace(remove=lambda:None)
    manager.task=manager.legacy_task=task
    monkeypatch.setattr(manager,'cleanup_legacy_check',lambda:None)
    monkeypatch.setattr('cachemonitor.observer_control.startup_value',lambda *a:None)
    def refused(**_):manager.health_state='refused';return None
    monkeypatch.setattr(manager,'health',refused)
    return manager


@pytest.mark.parametrize('journal',[None,'corrupt','stale'])
def test_offline_restore_without_gui_supervisor_or_valid_journal(manager,journal):
    if journal:
        manager.directory.mkdir(parents=True)
        manager.state_path.write_text('not-json' if journal=='corrupt' else json.dumps(dict(home=home_key(manager.home),enabled=False)))
    assert inspect(manager)['code']=='refused'
    result=restore(manager)
    assert result['code']=='restored'
    assert manager.config()[1]=={'model':'keep'}
    assert '# preserve me' in manager.config_path.read_text()
    assert manager.state()['enabled'] is False
    assert list((manager.directory/'backups').glob('codex-config-*.toml'))


def test_corrupt_config_never_claims_healthy_or_overwrites(manager):
    manager.config_path.write_text('model = "broken\n')
    before=manager.config_path.read_bytes()
    state=inspect(manager)
    assert state['code']=='unreadable' and state['can_recover']
    with pytest.raises(ValueError):restore(manager)
    assert manager.config_path.read_bytes()==before


def test_custom_connection_is_never_replaced(manager):
    manager.config_path.write_text('openai_base_url="https://custom.example/v1"\n')
    before=manager.config_path.read_bytes()
    restore(manager)
    assert manager.config_path.read_bytes()==before


def test_app_checks_deduplicate_without_changing_route(manager,monkeypatch):
    from cachemonitor.notifications import ConfirmedNotifications
    clock=[100]
    notices=ConfirmedNotifications(clock=lambda:clock[0])
    before=manager.config_path.read_bytes()
    assert notices.proxy(manager.status())==[]
    clock[0]=115;assert notices.proxy(manager.status())==[]
    clock[0]=130;assert len(notices.proxy(manager.status()))==1
    for at in (145,1000,1015):
        clock[0]=at;assert notices.proxy(manager.status())==[]
    monkeypatch.setattr(manager,'health',lambda **k:setattr(manager,'health_state','unknown'))
    clock[0]=1030;assert notices.proxy(manager.status())==[]
    assert notices.incident['code']=='refused'
    assert manager.config_path.read_bytes()==before
    assert not (manager.directory/'connection-check.json').exists()


def test_health_response_does_not_claim_model_connectivity():
    state={'configured':True,'probe_state':'healthy','health':{}}
    assert assess(state)['code']=='responding'
    state['health']['internal_failure_streak']=3
    assert assess(state)['code']=='relay_failure'
    state['health']={'storage_failure_streak':3}
    assert assess(state)['code']=='observation_failure'
    state['update']={'phase':'switching'}
    assert not assess(state)['confirmed']


def test_custom_home_recovered_from_record_and_url_restricted(manager):
    manager.write_state(dict(home=home_key(manager.home),url=manager.url))
    restored=target(directory=manager.directory)
    assert restored.home==manager.home and restored.url==manager.url
    with pytest.raises(ValueError):target(manager.home,manager.directory,'https://remote.example')


@pytest.fixture
def interrupted_exit(manager, monkeypatch):
    from cachemonitor import app_services as services
    owner = services.AppServices(manager, [], collection=False)
    manager.write_state(dict(home=home_key(manager.home), enabled=True, phase='active', previous_url=None))

    def write(phase='preparing', **extra):
        return owner.save(phase=phase, operation_id='interrupted-exit', resume=True,
            preparation_owner=dict(pid=123, created=1, executable=sys.executable),
            source=dict(instance='still-serving'), shutdown_incomplete=True,
            shutdown_incomplete_instance='older-instance', **extra)

    def status():
        journal = read_json(services.session_path(manager))
        return dict(configured=manager.config()[1].get('openai_base_url') == manager.url,
                    phase=journal.get('phase', 'active'), service_phase=journal.get('phase'),
                    health=dict(instance='still-serving', draining=False), probe_state='healthy')

    monkeypatch.setattr(manager, 'status', status)
    monkeypatch.setattr('cachemonitor.retired_cache.retire', lambda *a, **k: pytest.fail('Recovery must preserve the old proxy'))
    monkeypatch.setattr('cachemonitor.proxy_drain.ProxyDrain.run', lambda *a, **k: pytest.fail('Recovery must not drain'))
    monkeypatch.setattr(services.AppServices, 'stop_proxy', lambda *a, **k: pytest.fail('Recovery must not finish shutdown'))
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask.start', lambda *a, **k: pytest.fail('Recovery must not start services'))
    return SimpleNamespace(manager=manager, owner=owner, write=write)


@pytest.mark.parametrize('phase', ['preparing', 'closing'])
def test_proven_gui_exit_allows_direct_restore_without_touching_proxy(interrupted_exit, monkeypatch, phase):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write(phase)
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    before = services.session_path(case.manager).read_bytes()
    result = inspect(case.manager)
    assert result['code'] == 'interrupted_exit' and result['can_recover']
    assert services.session_path(case.manager).read_bytes() == before
    assert restore(case.manager)['code'] == 'restored'
    assert case.manager.config()[1] == {'model': 'keep'}
    assert '# preserve me' in case.manager.config_path.read_text()
    journal = read_json(services.session_path(case.manager))
    assert journal['phase'] == 'active' and journal['resume'] is False
    assert journal['source'] == dict(instance='still-serving')
    assert journal['shutdown_incomplete'] and journal['shutdown_incomplete_instance'] == 'older-instance'
    assert not list(case.manager.directory.glob('proxy-control-*.json'))
    assert inspect(case.manager)['code'] == 'direct'


@pytest.mark.parametrize('phase', ['preparing', 'closing'])
@pytest.mark.parametrize('condition', ['live', 'unknown', 'missing'])
def test_live_or_unknown_exit_owner_blocks_inspection_and_restore(interrupted_exit, monkeypatch, phase, condition):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write(phase)
    if condition == 'missing':
        journal = read_json(services.session_path(case.manager))
        journal.pop('preparation_owner')
        services.atomic_write(services.session_path(case.manager), json.dumps(journal).encode())

    def same_process(_):
        if condition == 'unknown':
            raise OSError('owner inspection unavailable')
        if condition == 'missing':
            pytest.fail('Missing owner cannot prove exit')
        return True

    monkeypatch.setattr(services.identity, 'same_process', same_process)
    before = services.session_path(case.manager).read_bytes()
    route = case.manager.config()[0]
    result = inspect(case.manager)
    assert result['code'] == 'exiting' and not result['can_recover']
    with pytest.raises(services.ExitRecoveryBlocked):
        restore(case.manager)
    assert services.session_path(case.manager).read_bytes() == before
    assert case.manager.config()[0] == route
    assert not list(case.manager.directory.glob('proxy-control-*.json'))


def test_restore_rechecks_operation_identity_after_acquiring_control_lock(interrupted_exit, monkeypatch):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write('closing')
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    lock_type = services.ProcessLock

    class NewOperationBeforeLock:
        def __init__(self, *args, **kwargs):
            self.lock = lock_type(*args, **kwargs)

        def __enter__(self):
            case.owner.save(phase='preparing', operation_id='new-operation', resume=True)
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    monkeypatch.setattr(services, 'ProcessLock', NewOperationBeforeLock)
    monkeypatch.setattr(case.manager, 'recover_direct', lambda: pytest.fail('A new operation owns recovery'))
    route = case.manager.config()[0]
    with pytest.raises(services.ExitRecoveryBlocked, match='종료 작업이 변경'):
        restore(case.manager)
    journal = read_json(services.session_path(case.manager))
    assert journal['operation_id'] == 'new-operation' and journal['resume'] is True
    assert case.manager.config()[0] == route


def test_restore_rechecks_owner_liveness_under_control_lock(interrupted_exit, monkeypatch):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write('preparing')
    observations = iter([False, True])
    monkeypatch.setattr(services.identity, 'same_process', lambda _: next(observations))
    monkeypatch.setattr(case.manager, 'recover_direct', lambda: pytest.fail('A live exit owner must retain control'))
    route = case.manager.config()[0]
    journal = services.session_path(case.manager).read_bytes()
    with pytest.raises(services.ExitRecoveryBlocked):
        restore(case.manager)
    assert case.manager.config()[0] == route
    assert services.session_path(case.manager).read_bytes() == journal


@pytest.mark.parametrize('route', [None, 'https://custom.example/v1'])
def test_interrupted_exit_recovery_preserves_prior_off_and_external_route(interrupted_exit, monkeypatch, route):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write('closing')
    case.manager.set_url(route)
    services.disable_resume(case.manager)
    before = case.manager.config()[0]
    monkeypatch.setattr(services.identity, 'same_process', lambda _: False)
    assert restore(case.manager)['code'] == 'restored'
    assert case.manager.config()[0] == before
    assert read_json(services.session_path(case.manager))['resume'] is False


def test_unreadable_status_cannot_bypass_live_exit_ownership(interrupted_exit, monkeypatch):
    from cachemonitor import app_services as services
    case = interrupted_exit
    case.write('closing')
    monkeypatch.setattr(services.identity, 'same_process', lambda _: True)

    def unreadable():
        raise ValueError('status unavailable')

    monkeypatch.setattr(case.manager, 'status', unreadable)
    result = inspect(case.manager)
    assert result['code'] == 'exiting' and not result['can_recover']
