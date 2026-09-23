"""Public-facing translation and payload checks keep internal records intact."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cachemonitor.i18n import set_language, tr
from cachemonitor.presentation import Choice, Text
from tools.package_release import payload


def test_runtime_check_requires_report_path():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root/'run.py'), '--verify-runtime'],
                            cwd=root, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 2
    assert 'Usage: Codexon.exe --verify-runtime <report.json>' in result.stderr
    assert 'Traceback' not in result.stderr


def test_english_token_labels_do_not_change_stored_values():
    set_language('en')
    try:
        label = Text('일반 입력')
        choice = Choice()
        choice.addItem('캐시 읽기', 'cached')
        assert label.text() == '일반 입력'
        assert label.state['text'] == 'Uncached input'
        assert choice.itemData(0) == 'cached'
        assert choice.state['items'][0]['text'] == 'Cached input'
        assert tr('2 / 2호출') == '2 / 2 calls'
        assert tr('모양') == 'Appearance'
        assert tr('알림') == 'Notifications'
    finally:
        set_language('ko')


def test_public_payload_rejects_local_paths_and_unneeded_qt(tmp_path):
    folder = tmp_path/'Codexon';folder.mkdir()
    exe = folder/'Codexon.exe';exe.write_bytes(b'synthetic executable')
    recovery=folder/'CodexonRecovery.exe';recovery.write_bytes(b'synthetic recovery')
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.md', 'BUNDLED-PYTHON.md', 'BUNDLED-QT.md',
                 'SOURCE-OFFER.md', 'USER-GUIDE.md', 'USER-GUIDE.ko.md'):
        (folder/name).write_text('Synthetic release test', encoding='utf-8')
    manifest = {'product':'Codexon','version':'2026.09.23.6','commit':'a'*40,
                'architecture':'x64','executable':'Codexon.exe',
                'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
                'recovery_sha256':hashlib.sha256(recovery.read_bytes()).hexdigest()}
    file = folder/'build-manifest.json'
    file.write_text(json.dumps(manifest), encoding='utf-8')
    assert payload(folder)[2] == manifest
    internal = folder/'_internal';internal.mkdir()
    (internal/'base_library.zip').write_bytes(b'python standard library archive')
    assert payload(folder)[2] == manifest
    (internal/'diagnostics.zip').write_bytes(b'not an app dependency')
    with pytest.raises(ValueError):payload(folder)
    (internal/'diagnostics.zip').unlink()
    file.write_text(json.dumps({**manifest,'source':r'C:\Users\developer\repo'}), encoding='utf-8')
    with pytest.raises(ValueError):payload(folder)
    file.write_text(json.dumps(manifest), encoding='utf-8')
    dll = folder/'_internal'/'PySide6'/'Qt6Graphs.dll';dll.parent.mkdir(parents=True)
    dll.write_bytes(b'not a redistributable module for this product')
    with pytest.raises(ValueError):payload(folder)
