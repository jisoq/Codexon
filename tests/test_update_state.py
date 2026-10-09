"""Durable update handoff, cancellation boundaries and stale result isolation."""
from types import SimpleNamespace
import hashlib
import io
import pytest
from cachemonitor.update_state import UpdateState,scope_for
from cachemonitor import app_update


def manager(root):
    return SimpleNamespace(home=root/'custom-home',evidence=root/'records.sqlite',directory=root/'observer',
                           url='http://127.0.0.1:18768',status=lambda:{'configured':False})


def test_install_handoff_survives_restart_and_rejects_previous_results(tmp_path):
    journal=UpdateState(tmp_path);scope=scope_for(manager(tmp_path))
    first=journal.begin('app','2026.10.09.1',scope=scope)
    product=tmp_path/'versions'/'new'/'Codexon'
    manifest=dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40)
    journal.installed(first['operation_id'],product,manifest)
    resumed=UpdateState(tmp_path)
    resumed.proxy_result(first['operation_id'],dict(phase='waiting',required_action='close_client',unknown_connections=1,scope=scope))
    assert resumed.read()['phase']=='needs_exit'
    assert resumed.read()['app']['executable']==str(product/'Codexon.exe')
    # A button or timeout never asserts connection closure.
    assert resumed.read()['proxy']['unknown_connections']==1
    resumed.proxy_result(first['operation_id'],dict(phase='failed',restored=True,scope=scope))
    assert resumed.read()['phase']=='partial'
    second=resumed.begin('proxy','2026.10.09.1',scope=scope)
    assert not journal.proxy_result(first['operation_id'],dict(phase='complete'))
    assert not journal.change(first['operation_id'],phase='complete')
    assert resumed.read()['operation_id']==second['operation_id'] and resumed.read()['phase']=='preparing'


@pytest.mark.parametrize('cancel_first',[True,False])
def test_cancel_and_swap_have_one_atomic_winner(tmp_path,cancel_first):
    journal=UpdateState(tmp_path);record=journal.begin('proxy','2026.10.09.1');op=record['operation_id']
    journal.proxy_result(op,dict(phase='waiting'))
    if cancel_first:
        assert journal.cancel(later=True)
        assert not journal.begin_switch(op)
        journal.proxy_result(op,dict(phase='waiting'))
        assert journal.read()['cancel_requested']
    else:
        assert journal.begin_switch(op)
        assert not journal.cancel(later=True)
        journal.proxy_result(op,dict(phase='waiting',cutover_started=True,required_action='close_client'))
        assert not journal.cancel()


def test_partial_retry_keeps_app_and_disabled_proxy(tmp_path,monkeypatch):
    from cachemonitor import install_management,retired_cache
    journal=UpdateState(tmp_path);m=manager(tmp_path)
    m.cleanup_legacy_check=lambda:None;m.adopt_registrations=lambda:None
    monkeypatch.setattr(retired_cache,'retire',lambda m:None)
    record=journal.begin('app','2026.10.09.1',scope=scope_for(m));op=record['operation_id']
    journal.installed(op,tmp_path/'versions'/'new'/'Codexon',dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40))
    journal.proxy_result(op,dict(phase='failed',restored=True))
    app=journal.read()['app']
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('Must not reinstall the app'))
    result=app_update.retry_proxy(m,journal)
    assert result['phase']=='complete' and result['app']==app and result['proxy']['phase']=='off'
    assert result['operation_id']==op


def test_download_cancel_never_launches_installer_or_changes_auth(tmp_path,monkeypatch):
    journal=UpdateState(tmp_path);m=manager(tmp_path)
    auth=m.home/'auth.json';auth.parent.mkdir();auth.write_bytes(b'preserve authentication')
    payload=b'payload';digest=hashlib.sha256(payload).hexdigest()
    from tests.test_app_update import release
    offered=release();record=journal.begin('app',offered['tag_name'].lstrip('v'))
    monkeypatch.setattr(app_update,'read_url',lambda *a:(digest+'  Codexon-Setup.exe').encode())
    class Download(io.BytesIO):
        def read(self,size):
            journal.cancel();return super().read(size)
    monkeypatch.setattr(app_update.urllib.request,'urlopen',lambda *a,**k:Download(payload))
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('Cancelled installer launched'))
    with pytest.raises(app_update.UpdateCancelled):
        app_update.download_and_install(offered,dict(InstallRoot=str(tmp_path)),lambda _:None,journal,record['operation_id'])
    assert not list(tmp_path.rglob('*.exe')) and not list(tmp_path.rglob('*.partial'))
    assert auth.read_bytes()==b'preserve authentication'


def test_wrong_target_install_is_rejected_before_activation(tmp_path,monkeypatch):
    from tests.test_install_management import fixture
    from cachemonitor import install_management
    product,recovery=fixture(tmp_path)
    journal=UpdateState(tmp_path);op=journal.begin('app','another-version')['operation_id']
    monkeypatch.setattr(install_management,'register',lambda *a,**k:pytest.fail('Incorrect version activated'))
    with pytest.raises(RuntimeError,match='다릅니다'):
        install_management.finish(tmp_path,product,recovery,isolated=True,update_id=op)
    assert not (tmp_path/'installation.json').exists()


def test_abandoned_download_requires_owner_exit_not_elapsed_time(tmp_path,monkeypatch):
    from cachemonitor import proxy_identity
    journal=UpdateState(tmp_path);op=journal.begin('app','2026.10.09.1')['operation_id']
    record=journal.read();record.update(phase='downloading',updated_at=0)
    import json
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:True)
    assert app_update.reconcile_update(journal)['phase']=='downloading'
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:False)
    assert app_update.reconcile_update(journal)['phase']=='interrupted'


def test_previous_app_worker_completion_is_not_new_deployment_completion(tmp_path,monkeypatch):
    from cachemonitor import install_management
    m=manager(tmp_path);journal=UpdateState(tmp_path);op=journal.begin('app','2026.10.09.1',scope=scope_for(m))['operation_id']
    journal.installed(op,tmp_path/'versions'/'new'/'Codexon',dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40))
    journal.proxy_result(op,dict(phase='waiting',source_instance='previous-worker'))
    m.update_status=lambda:dict(phase='complete',source_instance='previous-worker')
    calls=[]
    def apply(manager,**kwargs):
        calls.append(kwargs)
        return dict(phase='queued',operation_id=op)
    monkeypatch.setattr(install_management,'activate_proxy',apply)
    result=app_update.reconcile_update(journal,m)
    assert result['phase']=='waiting' and calls==[dict(operation_id=op,update_root=tmp_path)]
