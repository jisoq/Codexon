"""A release must never reuse a stale, untested or replaced Rust extension."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import build_native


@pytest.fixture(autouse=True)
def isolated_toolchain(monkeypatch, tmp_path):
    # Contract tests must neither build Rust nor probe the host's installed compiler.
    monkeypatch.setattr(build_native, 'toolchain_identity',
                        lambda root: {'rustc': 'fixture rustc 1.97.1', 'cargo': 'fixture cargo 1.97.1'})
    monkeypatch.setattr(build_native, 'compiler_identity', lambda: {'msvc': 'fixture toolset'})
    monkeypatch.setenv('CARGO_HOME', str(tmp_path / 'cargo-home'))


def native_tree(tmp_path):
    (tmp_path/'native/src').mkdir(parents=True)
    (tmp_path/'cachemonitor').mkdir()
    (tmp_path/'tools').mkdir()
    (tmp_path/'native/Cargo.toml').write_text('[package]\nname="fixture"\n', encoding='utf-8')
    (tmp_path/'native/Cargo.lock').write_text('version = 4\n', encoding='utf-8')
    (tmp_path/'rust-toolchain.toml').write_text('[toolchain]\nchannel="1.97.1"\n', encoding='utf-8')
    (tmp_path/'native/src/lib.rs').write_text('pub fn kernel() {}\n', encoding='utf-8')
    (tmp_path/'tools/build_native.py').write_text('fixture build helper\n', encoding='utf-8')
    (tmp_path/'requirements-release.lock').write_text('fixture==1.0\n', encoding='utf-8')
    return tmp_path


def verified_binary(root, *, tested=True):
    binary = build_native.extension_path(root)
    binary.write_bytes(b'synthetic compiled extension')
    build_native.receipt_path(root).write_text(json.dumps({
        'schema': build_native.RECEIPT_SCHEMA,
        'identity': build_native.build_identity(root),
        'binary_sha256': build_native.binary_digest(binary),
        'tests_passed': tested,
    }), encoding='utf-8')
    return binary


def fake_compilation(root, monkeypatch):
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    built = root/'native/target/release'/('_cpu_native' + suffix)
    built.parent.mkdir(parents=True)
    built.write_bytes(b'new tested native extension')
    artifact = dict(reason='compiler-artifact', manifest_path=str(root/'native/Cargo.toml'),
                    target=dict(name='_cpu_native', crate_types=['cdylib']), filenames=[str(built)])
    commands = []
    smokes = []

    def run(command, **kwargs):
        assert kwargs['check'] is True
        commands.append(command)
        assert command[1] in ('test', 'build'), 'Unexpected live tool invocation'
        assert kwargs['cwd'] == root.resolve()
        assert kwargs['env']['PYO3_PYTHON'] == sys.executable
        return SimpleNamespace(stdout=json.dumps(artifact) if command[1] == 'build' else '')

    monkeypatch.setattr(build_native.shutil, 'which', lambda name: name)
    monkeypatch.setattr(build_native.subprocess, 'run', run)
    monkeypatch.setattr(build_native, 'smoke_extension', lambda root: smokes.append(root))
    return commands, smokes


def test_missing_extension_cannot_pass_the_release_gate(tmp_path):
    root = native_tree(tmp_path)
    with pytest.raises(RuntimeError, match='missing or unverified'):
        build_native.checked_extension(root, smoke=False)


@pytest.mark.parametrize('changed', [
    'native/src/lib.rs', 'native/Cargo.lock', 'rust-toolchain.toml',
    'requirements-release.lock', 'tools/build_native.py', 'native/src/compiled-data.json',
    '.cargo/config.toml', 'native/.cargo/config.toml',
])
def test_changed_build_input_invalidates_binary_and_cache_key(tmp_path, changed):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    key = build_native.cache_key(root)
    assert build_native.checked_extension(root, smoke=False) == binary.resolve()
    path = root/changed
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as output:
        output.write('\nchanged input\n')
    assert build_native.cache_key(root) != key
    with pytest.raises(RuntimeError, match='stale or changed'):
        build_native.checked_extension(root, smoke=False)


def test_replaced_binary_and_new_rust_module_invalidate_the_build(tmp_path):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    binary.write_bytes(b'replaced binary')
    with pytest.raises(RuntimeError, match='stale or changed'):
        build_native.checked_extension(root, smoke=False)
    verified_binary(root)
    (root/'native/src/new_kernel.rs').write_text('pub fn added() {}\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='stale or changed'):
        build_native.checked_extension(root, smoke=False)


@pytest.mark.parametrize('dimension', ['python', 'abi', 'platform', 'rustc', 'compiler', 'flags'])
def test_changed_build_environment_invalidates_binary_and_cache_key(tmp_path, monkeypatch, dimension):
    root = native_tree(tmp_path)
    verified_binary(root)
    key = build_native.cache_key(root)
    if dimension == 'python':
        monkeypatch.setattr(build_native.sys, 'version', sys.version + ' changed Python build')
    elif dimension == 'abi':
        monkeypatch.setattr(build_native, 'ABI_VERSION', build_native.ABI_VERSION + 1)
    elif dimension == 'platform':
        monkeypatch.setattr(build_native.sysconfig, 'get_platform', lambda: 'different-architecture')
    elif dimension == 'rustc':
        monkeypatch.setattr(build_native, 'toolchain_identity', lambda root: {'rustc': 'different compiler'})
    elif dimension == 'compiler':
        monkeypatch.setattr(build_native, 'compiler_identity', lambda: {'msvc': 'different toolset'})
    else:
        monkeypatch.setenv('RUSTFLAGS', '--cfg changed_build_flag')
    assert build_native.cache_key(root) != key
    with pytest.raises(RuntimeError, match='stale or changed'):
        build_native.checked_extension(root, smoke=False, require_tested=True)


def test_runner_image_patch_alone_preserves_verified_binary(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    monkeypatch.setenv('ImageVersion', '20261018.264.0')
    binary = verified_binary(root)
    key = build_native.cache_key(root)
    monkeypatch.setenv('ImageVersion', '20261018.275.0')
    assert build_native.cache_key(root) == key
    assert build_native.checked_extension(root, smoke=False, require_tested=True) == binary.resolve()


def test_tested_cache_skips_compilation_but_checks_import(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    commands, smokes = fake_compilation(root, monkeypatch)
    assert build_native.build_native(root, reuse=True, test=True) == binary.resolve()
    assert commands == []
    assert smokes == [root.resolve()]


def test_untested_cache_cannot_satisfy_requested_tests(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    verified_binary(root, tested=False)
    with pytest.raises(RuntimeError, match='lacks required tests'):
        build_native.checked_extension(root, smoke=False, require_tested=True)
    commands, smokes = fake_compilation(root, monkeypatch)
    binary = build_native.build_native(root, reuse=True, test=True)
    assert [command[1] for command in commands] == ['test', 'build']
    assert smokes == [root.resolve()]
    assert binary.read_bytes() == b'new tested native extension'
    assert build_native.checked_extension(root, require_tested=True) == binary.resolve()


@pytest.mark.parametrize('corruption', ['source', 'binary', 'missing_binary', 'receipt', 'legacy', 'import'])
def test_invalid_cache_rebuilds_and_retests(tmp_path, monkeypatch, corruption):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    if corruption == 'source':
        (root/'native/src/lib.rs').write_text('changed source', encoding='utf-8')
    elif corruption == 'binary':
        binary.write_bytes(b'corrupt native binary')
    elif corruption == 'missing_binary':
        binary.unlink()
    elif corruption == 'receipt':
        build_native.receipt_path(root).write_text('null', encoding='utf-8')
    elif corruption == 'legacy':
        build_native.receipt_path(root).write_text(json.dumps({'source_sha256': build_native.source_digest(root)}),
                                                   encoding='utf-8')
    commands, smokes = fake_compilation(root, monkeypatch)
    if corruption == 'import':
        def smoke(root):
            smokes.append(root)
            if len(smokes) == 1:
                raise subprocess.CalledProcessError(1, ['python', 'import-check'])
        monkeypatch.setattr(build_native, 'smoke_extension', smoke)
    build_native.build_native(root, reuse=True, test=True)
    assert [command[1] for command in commands] == ['test', 'build']
    assert build_native.checked_extension(root, smoke=False, require_tested=True) == binary.resolve()


@pytest.mark.parametrize('stage', ['test', 'build', 'smoke'])
def test_failed_build_never_keeps_a_tested_receipt(tmp_path, monkeypatch, stage):
    root = native_tree(tmp_path)
    verified_binary(root)
    commands, smokes = fake_compilation(root, monkeypatch)
    run = build_native.subprocess.run

    def fail(command, **kwargs):
        if command[1] == stage:
            raise subprocess.CalledProcessError(1, command)
        return run(command, **kwargs)

    monkeypatch.setattr(build_native.subprocess, 'run', fail)
    if stage == 'smoke':
        def smoke(root):
            raise subprocess.CalledProcessError(1, ['python', 'import-check'])
        monkeypatch.setattr(build_native, 'smoke_extension', smoke)
    with pytest.raises(subprocess.CalledProcessError):
        build_native.build_native(root, test=True)
    assert not build_native.receipt_path(root).exists()
    with pytest.raises(RuntimeError, match='missing or unverified'):
        build_native.checked_extension(root, smoke=False, require_tested=True)


def test_toolchain_failure_is_not_hidden_by_a_valid_cache(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    verified_binary(root)
    def fail(root):
        raise subprocess.CalledProcessError(1, ['rustc', '--version'])
    monkeypatch.setattr(build_native, 'toolchain_identity', fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_native.build_native(root, reuse=True, test=True)


def test_changing_inputs_during_compilation_prevents_a_receipt(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    commands, smokes = fake_compilation(root, monkeypatch)
    run = build_native.subprocess.run
    def mutate(command, **kwargs):
        result = run(command, **kwargs)
        if command[1] == 'build':
            (root/'native/src/lib.rs').write_text('changed mid-build', encoding='utf-8')
        return result
    monkeypatch.setattr(build_native.subprocess, 'run', mutate)
    with pytest.raises(RuntimeError, match='inputs changed during compilation'):
        build_native.build_native(root, test=True)
    assert not build_native.receipt_path(root).exists()


def test_cache_key_does_not_depend_on_installed_binary_or_receipt(tmp_path, monkeypatch):
    root = native_tree(tmp_path)
    key = build_native.cache_key(root)
    fake_compilation(root, monkeypatch)
    build_native.build_native(root, test=True)
    assert build_native.cache_key(root) == key


def test_cli_cache_key_and_tested_check_contract(tmp_path, monkeypatch, capsys):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    key = build_native.cache_key(root)
    monkeypatch.setattr(build_native, 'cache_key', lambda: key)
    assert build_native.main(['--cache-key']) == 0
    assert capsys.readouterr().out == key + '\n'
    checked = []
    def check(*, require_tested):
        checked.append(require_tested)
        return binary
    monkeypatch.setattr(build_native, 'checked_extension', check)
    assert build_native.main(['--check', '--test']) == 0
    assert checked == [True]
    assert json.loads(capsys.readouterr().out)['sha256'] == build_native.binary_digest(binary)


def test_cargo_target_configuration_cannot_select_a_stale_host_binary(tmp_path):
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    root = native_tree(tmp_path)
    target = root/'native/target'
    stale = target/'release'/('_cpu_native' + suffix)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b'previous source build')
    actual = target/'configured-target/release'/('_cpu_native' + suffix)
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b'new source build')
    artifact = dict(reason='compiler-artifact', manifest_path=str(root/'native/Cargo.toml'),
                    target=dict(name='_cpu_native', crate_types=['cdylib']), filenames=[str(actual)])
    assert build_native.built_extension(json.dumps(artifact), root/'native/Cargo.toml', target) == actual.resolve()
    with pytest.raises(RuntimeError, match='exactly one'):
        build_native.built_extension('{"reason":"build-finished","success":true}', root/'native/Cargo.toml', target)
