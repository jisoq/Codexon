import json
import sys
import threading
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


def test_saved_home_without_journal_takes_precedence_over_environment(tmp_path, monkeypatch):
    from cachemonitor import launch_context
    saved=tmp_path/'saved';saved.mkdir()
    other=tmp_path/'environment';other.mkdir()
    evidence=tmp_path/'observations'/'selected.sqlite'
    index=tmp_path/'usage'/'selected.sqlite'
    preferences=tmp_path/'launch.json'
    preferences.write_text(json.dumps(dict(homes=[str(saved)],connection_paths=dict(
        evidence_path=str(evidence),index_path=str(index)))))
    monkeypatch.setattr(launch_context,'preference_path',lambda:preferences)
    monkeypatch.setenv('CODEX_HOME',str(other))
    selected=target()
    assert selected.home==saved.resolve()
    assert selected.evidence==evidence.resolve()
    assert selected.retirement_index==index.resolve()
    assert target(home=other,directory=tmp_path/'explicit').home==other.resolve()


@pytest.mark.parametrize('explicit_url',['http://127.0.0.1:18992','http://127.0.0.1:8768'])
def test_explicit_proxy_url_survives_journal_and_route_discovery(manager,explicit_url):
    manager.write_state(dict(home=home_key(manager.home),url=manager.url))
    index=manager.directory/'index.sqlite'
    index.with_name('cache-route.json').write_text(json.dumps(dict(url='http://127.0.0.1:18993')))
    selected=target(manager.home,manager.directory,explicit_url,index=index)
    assert selected.url==explicit_url
    assert selected.retirement_index==index.resolve()


def test_invalid_config_still_opens_recovery_target_without_overwriting(manager,monkeypatch):
    manager.config_path.write_bytes(b'model = "broken\n')
    original=manager.config_path.read_bytes()
    selected=target(manager.home,manager.directory)
    monkeypatch.setattr(selected,'health',lambda **_:None)
    assert inspect(selected)['code']=='unreadable'
    with pytest.raises(ValueError):restore(selected)
    assert manager.config_path.read_bytes()==original


def test_foreign_listener_does_not_block_modern_direct_restore(manager,monkeypatch):
    def foreign(**_):
        manager.health_state='identity_mismatch'
        raise RuntimeError('foreign listener')
    monkeypatch.setattr(manager,'health',foreign)
    assert inspect(manager)['code']=='identity_mismatch'
    assert restore(manager)['code']=='restored'
    assert manager.config()[1]=={'model':'keep'}
    assert not list(manager.directory.glob('proxy-control-*.json'))


def test_restore_completion_and_cleanup_retry_are_idempotent(manager):
    attempts=[]
    def cleanup():
        attempts.append(True)
        if len(attempts)==1:raise RuntimeError('task removal unavailable')
    manager.task=manager.legacy_task=SimpleNamespace(remove=cleanup)
    first=restore(manager)
    assert first['code']=='restored' and first['can_recover']
    assert first['cleanup_warning']
    direct=manager.config_path.read_bytes()
    backups=list((manager.directory/'backups').glob('codex-config-*.toml'))
    second=restore(manager)
    assert second['code']=='restored' and not second['can_recover']
    assert not second.get('cleanup_warning')
    assert manager.config_path.read_bytes()==direct
    assert list((manager.directory/'backups').glob('codex-config-*.toml'))==backups
    assert not restore(manager)['can_recover']


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


@pytest.fixture
def recovery_window(manager):
    from cachemonitor.connection_recovery import RecoveryWindow
    window=RecoveryWindow(manager)
    def wait(predicate):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            window.root.update()
            if predicate():return
        pytest.fail('Recovery window did not reach the expected state')
    try:
        wait(lambda:not window.busy)
        yield window,wait
    finally:
        window.root.destroy()


@pytest.mark.integration
def test_native_recheck_enter_observes_config_without_restoring(recovery_window,manager):
    window,wait=recovery_window
    manager.config_path.write_text('# keep\nmodel="changed"\n')
    before=manager.config_path.read_bytes()
    window.root.focus_force()
    window.recheck_button.focus_set()
    wait(lambda:window.root.focus_get()==window.recheck_button)
    window.recheck_button.event_generate('<Return>')
    wait(lambda:not window.busy and window.result['code']=='direct')
    assert manager.config_path.read_bytes()==before
    assert not (manager.directory/'backups').exists()
    assert window.button.winfo_ismapped() and window.button.instate(['!disabled'])


@pytest.mark.integration
def test_native_default_enter_restores_once_and_offers_close(recovery_window,manager,monkeypatch):
    from cachemonitor import connection_recovery as recovery
    window,wait=recovery_window
    entered=threading.Event();release=threading.Event();workers=[]
    actual=recovery.restore
    def blocked(manager):
        workers.append(threading.get_ident())
        entered.set()
        if not release.wait(5):raise RuntimeError('Test worker was not released')
        return actual(manager)
    monkeypatch.setattr(recovery,'restore',blocked)
    window.root.focus_force()
    window.path.focus_set()
    wait(lambda:window.root.focus_get()==window.path)
    try:
        window.root.event_generate('<Return>')
        wait(entered.is_set)
        assert window.busy
        assert window.button.instate(['disabled']) and window.recheck_button.instate(['disabled'])
        assert not window.submit(recovery.inspect)
        window.root.event_generate('<Return>')
        window.path.selection_range(0,'end')
        assert window.path.selection_present()
    finally:release.set()
    wait(lambda:not window.busy and window.result['code']=='restored')
    assert workers==[workers[0]] and workers[0]!=threading.get_ident()
    assert not window.button.winfo_ismapped()
    assert window.close_button.instate(['!disabled'])
    assert window.root.focus_get()==window.path
    assert window.recheck_timer is None
    assert manager.config()[1]=={'model':'keep'}


@pytest.mark.integration
def test_native_compact_diagnosis_scrolls_with_reachable_controls(recovery_window,manager):
    window,wait=recovery_window
    window.root.geometry('480x280')
    window.result=dict(code='unreadable',can_recover=True,title='Connection settings could not be read. '*4,
        detail='A long diagnostic remains selectable and scrollable. '*80)
    window.render()
    wait(lambda:window.scrollbar.winfo_ismapped())
    assert window.path.get()==str(manager.config_path)
    assert window.path.instate(['readonly'])
    for control in (window.diagnosis,window.path,window.recheck_button,window.close_button,window.button):
        x=control.winfo_rootx()-window.root.winfo_rootx()
        y=control.winfo_rooty()-window.root.winfo_rooty()
        assert control.winfo_ismapped() and control.winfo_width()>0 and control.winfo_height()>0
        assert 0<=x<x+control.winfo_width()<=window.root.winfo_width()
        assert 0<=y<y+control.winfo_height()<=window.root.winfo_height()
    window.diagnosis.yview_moveto(1)
    wait(lambda:window.diagnosis.yview()[0]>0)


@pytest.mark.integration
def test_native_failed_restore_keeps_changed_settings_visible_for_recheck(recovery_window,manager,monkeypatch):
    from cachemonitor import connection_recovery as recovery
    window,wait=recovery_window
    def partial(manager):
        manager.set_url(None)
        raise RuntimeError('cleanup failed after changing config')
    monkeypatch.setattr(recovery,'restore',partial)
    window.button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='error')
    assert manager.config()[1]=={'model':'keep'}
    assert window.result['can_recover'] and window.result['error']
    assert window.button.instate(['!disabled']) and window.recheck_button.instate(['!disabled'])
    window.recheck_button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='direct')


@pytest.mark.integration
def test_native_exiting_rechecks_preserve_diagnosis_selection_and_scroll(recovery_window,monkeypatch):
    from cachemonitor import connection_recovery as recovery
    window,wait=recovery_window
    entered=threading.Event();release=threading.Event();checks=[]
    result=dict(code='exiting',can_recover=False,title='Exit ownership is being checked',
        detail='Existing active responses must finish before changing the route. '*60)
    def exiting(manager):
        checks.append(True)
        if len(checks)>1:
            entered.set()
            if not release.wait(5):raise RuntimeError('Test inspection was not released')
        return dict(result)
    monkeypatch.setattr(recovery,'inspect',exiting)
    window.root.geometry('480x280')
    window.recheck_button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='exiting')
    wait(lambda:window.scrollbar.winfo_ismapped())
    window.diagnosis.yview_moveto(1)
    window.diagnosis.tag_add('sel','2.0','2.30')
    wait(lambda:window.diagnosis.yview()[0]>0)
    content=window.diagnosis.get('1.0','end-1c')
    selection=tuple(map(str,window.diagnosis.tag_ranges('sel')))
    scroll=window.diagnosis.yview()
    assert selection and result['detail'] in content
    try:
        for _ in range(2):
            wait(entered.is_set)
            assert window.busy and not window.progress.winfo_ismapped()
            assert window.diagnosis.get('1.0','end-1c')==content
            assert tuple(map(str,window.diagnosis.tag_ranges('sel')))==selection
            assert window.diagnosis.yview()==pytest.approx(scroll)
            assert not window.submit(recovery.inspect)
            release.set()
            wait(lambda:not window.busy)
            assert window.diagnosis.get('1.0','end-1c')==content
            assert tuple(map(str,window.diagnosis.tag_ranges('sel')))==selection
            assert window.diagnosis.yview()==pytest.approx(scroll)
            release.clear()
            entered.clear()
    finally:release.set()


@pytest.mark.integration
def test_native_cleanup_warning_survives_recheck_until_successful_retry(recovery_window,manager):
    window,wait=recovery_window
    warning='Scheduled task cleanup is unavailable'
    allow_cleanup=threading.Event()
    def cleanup():
        if not allow_cleanup.is_set():raise RuntimeError(warning)
    manager.task=manager.legacy_task=SimpleNamespace(remove=cleanup)
    window.button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='restored')
    persisted=manager.state()['cleanup_warning']
    assert warning in persisted
    assert persisted in window.diagnosis.get('1.0','end-1c')
    assert window.button.winfo_ismapped() and window.button.instate(['!disabled'])
    direct=manager.config_path.read_bytes()
    backups=list((manager.directory/'backups').glob('codex-config-*.toml'))
    window.recheck_button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='direct')
    assert manager.state()['cleanup_warning']==persisted
    assert persisted in window.diagnosis.get('1.0','end-1c')
    assert window.button.winfo_ismapped() and window.button.instate(['!disabled'])
    allow_cleanup.set()
    window.button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='restored')
    assert not manager.state().get('cleanup_warning')
    assert warning not in window.diagnosis.get('1.0','end-1c')
    assert not window.button.winfo_ismapped()
    assert manager.config_path.read_bytes()==direct
    assert list((manager.directory/'backups').glob('codex-config-*.toml'))==backups
    window.recheck_button.event_generate('<<Invoke>>')
    wait(lambda:not window.busy and window.result['code']=='direct')
    assert warning not in window.diagnosis.get('1.0','end-1c')
    assert window.button.winfo_ismapped() and window.button.instate(['!disabled'])
