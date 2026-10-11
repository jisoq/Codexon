"""A release must never accept a missing, stale or replaced Rust extension."""
import json
import sys

import pytest

from tools import build_native


def native_tree(tmp_path):
    (tmp_path/'native/src').mkdir(parents=True)
    (tmp_path/'cachemonitor').mkdir()
    (tmp_path/'native/Cargo.toml').write_text('[package]\nname="fixture"\n', encoding='utf-8')
    (tmp_path/'native/Cargo.lock').write_text('version = 4\n', encoding='utf-8')
    (tmp_path/'rust-toolchain.toml').write_text('[toolchain]\nchannel="1.97.1"\n', encoding='utf-8')
    (tmp_path/'native/src/lib.rs').write_text('pub fn kernel() {}\n', encoding='utf-8')
    return tmp_path


def verified_binary(root):
    binary = build_native.extension_path(root)
    binary.write_bytes(b'synthetic compiled extension')
    build_native.receipt_path(root).write_text(json.dumps({
        'abi_version': build_native.ABI_VERSION,
        'source_sha256': build_native.source_digest(root),
        'binary_sha256': build_native.binary_digest(binary),
    }), encoding='utf-8')
    return binary


def test_missing_extension_cannot_pass_the_release_gate(tmp_path):
    root = native_tree(tmp_path)
    with pytest.raises(RuntimeError, match='missing or unverified'):
        build_native.checked_extension(root, smoke=False)


@pytest.mark.parametrize('changed', ['native/src/lib.rs', 'native/Cargo.lock', 'rust-toolchain.toml'])
def test_source_or_toolchain_change_invalidates_the_previous_binary(tmp_path, changed):
    root = native_tree(tmp_path)
    binary = verified_binary(root)
    assert build_native.checked_extension(root, smoke=False) == binary.resolve()
    with (root/changed).open('a', encoding='utf-8') as output:
        output.write('\nchanged input\n')
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
