"""Cross-platform boundaries: recorded paths, runtime discovery and safe hooks."""
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from cachemonitor import codex_runtime, platform_paths
from cachemonitor.analytics import project_key
from cachemonitor.codex_names import path_key


def test_posix_project_identity_never_merges_case_sensitive_directories():
    assert project_key('/tmp/ProjectA') != project_key('/tmp/projecta')
    assert path_key('/tmp/ProjectA') == project_key('/tmp/ProjectA')
    assert platform_paths.path_within('/tmp/ProjectA/sub', '/tmp/ProjectA')
    assert not platform_paths.path_within('/tmp/projecta/sub', '/tmp/ProjectA')
    assert not platform_paths.path_within('/tmp/ProjectAB', '/tmp/ProjectA')
    assert platform_paths.path_within('/tmp/project', '/')
    assert project_key(r'C:\Work\APP') == project_key('c:/work/app')
    assert platform_paths.path_within(r'C:\Work\APP\child', 'c:/work/app')
    assert platform_paths.parent_path('/tmp/한글 작업/output') == '/tmp/한글 작업'


def test_storage_override_is_shared_and_does_not_create_directories(tmp_path, monkeypatch):
    target = tmp_path / 'app data'
    monkeypatch.setenv('CODEXON_DATA_DIR', str(target))
    from cachemonitor.model_evidence import default_path
    from cachemonitor.quota_cycles import ledger_path
    from cachemonitor.launch_context import preference_path
    assert default_path() == target / 'model-observer' / 'model-evidence.sqlite'
    assert ledger_path() == target / 'quota-cycles.sqlite'
    assert preference_path() == target / 'launch.json'
    assert not target.exists()


def test_explicit_runtime_is_validated_without_falling_back(tmp_path, monkeypatch):
    binary = tmp_path / 'Codex CLI'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o700)
    monkeypatch.setenv('CODEXON_CODEX_PATH', str(binary))
    assert codex_runtime.locate_codex() == str(binary.resolve())
    binary.unlink()
    with pytest.raises(RuntimeError, match='실행 경로'):
        codex_runtime.locate_codex()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX launch quoting')
def test_hook_command_round_trips_spaces_quotes_and_shell_metacharacters(tmp_path, monkeypatch):
    from cachemonitor import cache_hooks
    from cachemonitor.launch_context import command_arguments
    binary = tmp_path / "Codexon ' $(literal)"
    database = tmp_path / "한글 공간 ' \" $()" / 'control.sqlite'
    normal, windows = cache_hooks.command(database, executable=binary)
    assert command_arguments(normal) == [str(binary.resolve()), '--cache-hook', '--database', str(database.resolve())]
    assert windows.startswith('& ')
    home = tmp_path / 'codex'
    cache_hooks.configure(home, database, True, executable=binary)
    definition = json.loads((home / 'hooks.json').read_text())
    assert shlex.split(definition['hooks']['Stop'][0]['hooks'][0]['command']) == command_arguments(normal)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS credential store')
def test_keychain_account_type_selects_chatgpt_without_auth_file(tmp_path, monkeypatch):
    from cachemonitor.observer_control import ObserverManager
    from cachemonitor.quota_live import AccountClient
    monkeypatch.setattr(AccountClient, 'account_type', lambda self: 'chatgpt')
    home = tmp_path / 'codex'
    home.mkdir()
    manager = ObserverManager(home, tmp_path / 'app')
    assert manager.upstream() == 'chatgpt'
    assert not (home / 'auth.json').exists()
    (home / 'auth.json').write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"synthetic"}')
    assert manager.upstream() == 'openai'


def test_native_instance_name_is_user_scoped(monkeypatch):
    if hasattr(os, 'getuid'):
        monkeypatch.setattr(os, 'getuid', lambda: 123)
        assert platform_paths.instance_name() == 'CacheMonitor-123'


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS installed bundle links')
def test_installed_hook_keeps_trusted_command_across_atomic_app_updates(tmp_path, monkeypatch):
    from cachemonitor import cache_hooks, installation
    applications = tmp_path / 'Applications'
    applications.mkdir()
    versions = []
    for number in (1, 2):
        bundle = tmp_path / str(number) / 'Codexon.app'
        binary = bundle / 'Contents/MacOS/Codexon'
        binary.parent.mkdir(parents=True)
        binary.write_text('synthetic executable')
        versions.append((bundle, binary))
    link = applications / 'Codexon.app'
    link.symlink_to(versions[0][0], target_is_directory=True)
    receipt = {'applications': str(applications), 'AppPath': str(versions[0][1])}
    monkeypatch.setattr(installation, 'installed', lambda: receipt)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(versions[0][1]))
    before = cache_hooks.command(tmp_path / 'control.sqlite')[0]
    replacement = applications / 'new.app'
    replacement.symlink_to(versions[1][0], target_is_directory=True)
    os.replace(replacement, link)
    receipt['AppPath'] = str(versions[1][1])
    monkeypatch.setattr(sys, 'executable', str(versions[1][1]))
    after = cache_hooks.command(tmp_path / 'control.sqlite')[0]
    assert before == after
    assert Path(shlex.split(after)[0]).resolve() == versions[1][1]
    # An unrelated replacement link must not become a trusted hook executable.
    receipt['AppPath'] = str(versions[0][1])
    monkeypatch.setattr(sys, 'executable', str(versions[0][1]))
    assert shlex.split(cache_hooks.command(tmp_path / 'control.sqlite')[0])[0] == str(versions[0][1])
