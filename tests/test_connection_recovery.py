import json
import time
import tomllib
from types import SimpleNamespace

import pytest

from cachemonitor.connection_recovery import inspect, restore, target, assess
from cachemonitor.observer_control import ObserverManager
from cachemonitor.model_evidence import home_key
from cachemonitor.observer_state import read_json


def test_start_menu_recovery_uses_the_saved_cache_connection(tmp_path,monkeypatch):
    from cachemonitor import launch_context,install_management
    from cachemonitor.cache_worker_control import CacheWorkerManager
    home=tmp_path/'custom home';home.mkdir()
    index=tmp_path/'analysis'/'index.sqlite';index.parent.mkdir()
    evidence=tmp_path/'data'/'evidence.sqlite'
    route='http://127.0.0.1:18771'
    index.with_name('cache-route.json').write_text(json.dumps(dict(url=route)))
    (home/'config.toml').write_text('openai_base_url="'+route+'"\nmodel="preserve"\n')
    (home/'auth.json').write_text('preserve authentication')
    monkeypatch.setattr(launch_context,'cache_paths',lambda:dict(index_path=str(index),evidence_path=str(evidence)))
    monkeypatch.setattr(launch_context,'resolve_homes',lambda:[str(home)])
    manager=target()
    manager.health_state='refused'
    assert isinstance(manager,CacheWorkerManager) and manager.url==route
    assert isinstance(target(home),CacheWorkerManager)
    assert isinstance(target(home,evidence.parent,route),CacheWorkerManager)
    assert install_management.connection_manager().index==manager.index
    monkeypatch.setattr('cachemonitor.observer_control.startup_value',lambda *a:None)
    monkeypatch.setattr(manager,'health',lambda **_:None)
    from cachemonitor.cache_db import connect
    db=connect(index.with_name('cache-control.sqlite'))
    db.executemany('INSERT INTO cache_operating_grants(id,home,data) VALUES(?,?,?)',
                   [('owned',str(home),'{}'),('other',str(tmp_path/'other'),'{}')])
    db.close()
    task=SimpleNamespace(remove=lambda:None,inspect=lambda:dict(registered=False))
    manager.task=manager.legacy_task=task
    monkeypatch.setattr(manager,'cleanup_legacy_check',lambda:None)
    assert restore(manager)['code']=='restored'
    assert manager.config()[1]=={'model':'preserve'}
    assert (home/'auth.json').read_text()=='preserve authentication'
    assert inspect(manager)['status']['restart_required']
    db=connect(index.with_name('cache-control.sqlite'))
    assert db.execute('SELECT id,stopped FROM cache_operating_grants ORDER BY id').fetchall()==[('other',None),('owned','revoked')]
    db.close()
    explicit=target(home,tmp_path/'explicit',url='http://127.0.0.1:18772')
    assert not isinstance(explicit,CacheWorkerManager) and explicit.url.endswith(':18772')


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
