import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cachemonitor import install_management as install


def fixture(root):
    product=root/'versions'/'new'/'Codexon';product.mkdir(parents=True)
    recovery=root/'maintenance'/'new'/'CodexonRecovery.exe';recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b'recovery')
    (product/'Codexon.exe').write_bytes(b'new')
    (product/'build-manifest.json').write_text(json.dumps(dict(product='Codexon',version='test',commit='a'*40,
        sha256=hashlib.sha256(b'new').hexdigest(),recovery_sha256=hashlib.sha256(b'recovery').hexdigest())))
    return product,recovery


def test_runtime_failure_keeps_previous_launch_pointer_and_files(tmp_path,monkeypatch):
    product,recovery=fixture(tmp_path)
    old={'product':'old','version':'old'}
    (tmp_path/'installation.json').write_text(json.dumps(old))
    monkeypatch.setattr(install,'register',lambda *a,**k:pytest.fail('Must not activate failed runtime'))
    monkeypatch.setattr(install.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError,match='실행 검사'):install.finish(tmp_path,product,recovery,isolated=True)
    assert json.loads((tmp_path/'installation.json').read_text())==old


def test_success_preserves_previous_receipt_and_unrelated_records(tmp_path,monkeypatch):
    product,recovery=fixture(tmp_path)
    old=tmp_path/'versions'/'old';old.mkdir();(old/'Codexon.exe').write_bytes(b'old')
    (tmp_path/'installation.json').write_text(json.dumps({'product':str(old)}))
    (tmp_path/'record.sqlite').write_bytes(b'user record')
    def run(command,**_):
        Path(command[-1]).write_text(json.dumps({'errors':[],'version':'test'}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install.subprocess,'run',run)
    calls=[];monkeypatch.setattr(install,'register',lambda *a,**k:calls.append(a))
    install.finish(tmp_path,product,recovery,isolated=True)
    assert calls and (old/'Codexon.exe').read_bytes()==b'old'
    assert (tmp_path/'record.sqlite').read_bytes()==b'user record'
    assert list(tmp_path.glob('installation-*.json'))


def test_uninstall_defers_when_any_payload_process_is_in_use(tmp_path,monkeypatch):
    monkeypatch.setattr(install,'processes_under',lambda root:[dict(ExecutablePath=str(root/'versions'/'Codexon.exe'),ProcessId=42)])
    with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(tmp_path,isolated=True)


def test_product_cannot_escape_installation_root(tmp_path):
    with pytest.raises(ValueError):install.contained(tmp_path/'outside',tmp_path/'versions')


def test_login_startup_preserves_custom_home_and_disabled_or_unrelated_values():
    command='"C:\\old folder\\CacheMonitor.exe" --hidden --codex-home "C:\\custom home"'
    changed=install.startup_replacement(command,Path('C:/new/Codexon.exe'))
    assert changed.endswith('--hidden --codex-home "C:\\custom home"')
    assert 'old folder' not in changed
    for unrelated in ('', '"C:\\pythonw.exe" run.py --hidden'):
        assert install.startup_replacement(unrelated,Path('C:/new/Codexon.exe'))==unrelated
