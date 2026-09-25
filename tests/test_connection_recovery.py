import json
import time
import tomllib
from types import SimpleNamespace

import pytest

from cachemonitor.connection_recovery import inspect, restore, check_once, target, assess
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
    manager.task=manager.legacy_task=manager.check_task=task
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


def test_check_persists_dedup_across_runs_and_never_changes_route(manager):
    calls=[];notify=lambda *args:(calls.append(1) or True)
    before=manager.config_path.read_bytes()
    check_once(manager,notification=notify,now=100)
    assert calls==[]
    check_once(manager,notification=notify,now=160)
    check_once(manager,notification=notify,now=220)
    assert calls==[1]
    # A missed run must not produce a new notification for the same unresolved fault.
    check_once(manager,notification=notify,now=1000)
    check_once(manager,notification=notify,now=1060)
    assert calls==[1]
    assert manager.config_path.read_bytes()==before


def test_unknown_is_not_an_alert_and_does_not_clear_existing_incident(manager,monkeypatch):
    calls=[];notify=lambda *a:(calls.append(1) or True)
    check_once(manager,notification=notify,now=100)
    check_once(manager,notification=notify,now=160)
    def unknown(**_):manager.health_state='unknown';return None
    monkeypatch.setattr(manager,'health',unknown)
    for now in (220,280,340):assert check_once(manager,notification=notify,now=now)['code']=='unknown'
    assert calls==[1]
    assert read_json(manager.directory/'connection-check.json')['notified_code']=='refused'


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
