import pytest


@pytest.fixture(autouse=True)
def no_running_desktop(monkeypatch, tmp_path):
    from cachemonitor import client_restart, chatgpt_startup
    from cachemonitor.update_state import UpdateState
    from cachemonitor.quick_runtime import Confirmation
    from cachemonitor.proxy_update import ProxyUpdate
    original = UpdateState.begin
    def begin(self, *args, **kwargs):
        if kwargs.get('client') is None:kwargs['client']=dict(phase='not_running')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(UpdateState, 'begin', begin)
    monkeypatch.setattr(chatgpt_startup, 'desktop_processes', lambda: [])
    monkeypatch.setattr(client_restart, 'capture', lambda: dict(phase='not_running', targets=[], reopen_required=False))
    monkeypatch.setattr(client_restart, 'confirm_native', lambda language: True)
    monkeypatch.setattr(client_restart, 'recovery_path', lambda *a: tmp_path/'recovery.exe')
    monkeypatch.setattr(Confirmation, 'open', lambda self: self.finish(1))
    monkeypatch.setattr(ProxyUpdate, 'preflight', lambda self: {})
