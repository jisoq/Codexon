"""Native distributions retain the exact dependency and standard-library notices."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

from tools import build_native, collect_notices
from tools.collect_notices import copy_rust_notices, rust_dependencies


def rust_graph(root):
    packages = []
    for name, kind in [('app', 'cdylib'), ('runtime', 'lib'), ('macros', 'proc-macro'), ('helper', 'lib')]:
        source = root/'registry'/name
        source.mkdir(parents=True)
        (source/'LICENSE-MIT').write_bytes(b'Copyright notice\r\nExact license bytes\r\n')
        packages.append(dict(id=name, name=name, version='1.0.0',
                             manifest_path=str(source/'Cargo.toml'), license='MIT', license_file=None,
                             targets=[dict(kind=[kind])]))
    def dependency(name, kind=None):
        return dict(pkg=name, dep_kinds=[dict(kind=kind)])
    nodes = [dict(id='app', deps=[dependency('runtime'), dependency('helper', 'build')]),
             dict(id='runtime', deps=[dependency('macros')]),
             dict(id='macros', deps=[dependency('helper')]), dict(id='helper', deps=[])]
    return dict(packages=packages, resolve=dict(root='app', nodes=nodes))


def rust_sysroot(root):
    sysroot = root/'toolchain'
    notice = sysroot/'share/doc/rust/COPYRIGHT-library.html'
    notice.parent.mkdir(parents=True)
    notice.write_bytes(b'<html>Standard library and component license texts</html>\n')
    return sysroot, notice


def test_rust_notices_retain_runtime_build_and_standard_library_licenses(tmp_path):
    graph = rust_graph(tmp_path)
    assert {package['name']: role for package, role in rust_dependencies(graph)} == {
        'runtime': 'Runtime dependency', 'macros': 'Build dependency', 'helper': 'Build dependency'}
    sysroot, standard = rust_sysroot(tmp_path)
    product = tmp_path/'product'
    assert copy_rust_notices(product, graph, sysroot, '1.97.1') == 3
    for package in graph['packages'][1:]:
        source = Path(package['manifest_path']).parent/'LICENSE-MIT'
        assert (product/f"LICENSES/rust/{package['name']}-1.0.0/LICENSE-MIT").read_bytes() == source.read_bytes()
    assert (product/'LICENSES/rust/standard-library/COPYRIGHT-library.html').read_bytes() == standard.read_bytes()
    inventory = (product/'BUNDLED-RUST.md').read_text(encoding='utf-8')
    assert '| Rust standard library | 1.97.1 |' in inventory
    assert str(tmp_path) not in inventory


def test_missing_crate_or_standard_library_notice_stops_packaging(tmp_path):
    graph = rust_graph(tmp_path)
    sysroot, standard = rust_sysroot(tmp_path)
    source = tmp_path/'registry/runtime/LICENSE-MIT'
    source.unlink()
    with pytest.raises(RuntimeError, match='No cached license file'):
        copy_rust_notices(tmp_path/'product', graph, sysroot, '1.97.1')
    source.write_bytes(b'restored notice')
    standard.unlink()
    with pytest.raises(RuntimeError, match='standard-library copyright notice'):
        copy_rust_notices(tmp_path/'product', graph, sysroot, '1.97.1')


def test_license_file_cannot_copy_a_file_outside_its_crate(tmp_path):
    graph = rust_graph(tmp_path)
    sysroot, _ = rust_sysroot(tmp_path)
    (tmp_path/'registry/private.txt').write_text('private content', encoding='utf-8')
    graph['packages'][1]['license_file'] = '../private.txt'
    with pytest.raises(RuntimeError, match='escaped the crate directory'):
        copy_rust_notices(tmp_path/'product', graph, sysroot, '1.97.1')


RUSTC_VERSION = 'rustc 1.97.1 (fixture)\nhost: x86_64-pc-windows-msvc\nrelease: 1.97.1'


def native_receipt(root, *, tested=True):
    binary = build_native.extension_path(root)
    if not binary.exists():
        binary.write_bytes(b'synthetic native extension')
    receipt = {'schema': build_native.RECEIPT_SCHEMA,
               'identity': {**build_native.artifact_identity(root), 'toolchain': {'rustc': RUSTC_VERSION}},
               'binary_sha256': build_native.binary_digest(binary), 'tests_passed': tested}
    build_native.receipt_path(root).write_text(json.dumps(receipt), encoding='utf-8')


@pytest.fixture
def native_distribution(tmp_path, monkeypatch):
    root = tmp_path/'source'
    for name, data in {
        'native/Cargo.toml': '[package]\nname = "fixture"\n',
        'native/Cargo.lock': 'version = 4\n',
        'native/src/lib.rs': 'pub fn fixture() {}\n',
        'rust-toolchain.toml': '[toolchain]\nchannel = "1.97.1"\n',
        'requirements-release.lock': 'fixture==1.0\n',
        'tools/build_native.py': 'fixture build helper\n',
        'tools/collect_notices.py': 'fixture notice helper\n',
    }.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding='utf-8')
    (root/'cachemonitor').mkdir()
    graph = rust_graph(root)
    sysroot, standard = rust_sysroot(root)
    native_receipt(root)
    calls = []
    def command(arguments, *, cwd, text):
        assert Path(cwd) == root
        assert text is True
        calls.append(arguments)
        if arguments[:2] == ['rustc', '--version']:
            return RUSTC_VERSION + '\n'
        if arguments == ['rustc', '--print', 'sysroot']:
            return str(sysroot) + '\n'
        assert arguments[:2] == ['cargo', 'metadata']
        assert '--locked' in arguments
        assert '--offline' not in arguments, 'Cold producers must be allowed to fetch locked crates'
        return json.dumps(graph)
    monkeypatch.setattr(collect_notices.subprocess, 'check_output', command)
    return SimpleNamespace(root=root, graph=graph, sysroot=sysroot, standard=standard, calls=calls,
                           artifact=root/'cachemonitor/_cpu_native.notices.zip')


def forbid_rust(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Verified notice handoff must not invoke Rust or Cargo')
    monkeypatch.setattr(collect_notices.subprocess, 'check_output', forbidden)
    monkeypatch.setattr(build_native, 'toolchain_identity', forbidden)
    monkeypatch.setattr(build_native, 'compiler_identity', forbidden)


def rewrite_archive(path, change):
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    change(contents)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in contents.items():
            archive.writestr(name, data)


def test_cold_producer_generates_locked_notices_with_exact_license_bytes(native_distribution):
    fixture = native_distribution
    assert collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root) == fixture.artifact.resolve()
    assert [command[0] for command in fixture.calls] == ['rustc', 'rustc', 'cargo']
    contents, count = collect_notices.read_rust_artifact(fixture.artifact, fixture.root)
    assert count == 3
    for package in fixture.graph['packages'][1:]:
        source = Path(package['manifest_path']).parent/'LICENSE-MIT'
        assert contents[f"LICENSES/rust/{package['name']}-1.0.0/LICENSE-MIT"] == source.read_bytes()
    assert contents[collect_notices.RUST_STANDARD_NOTICE] == fixture.standard.read_bytes()
    assert b'| Rust standard library | 1.97.1 |' in contents['BUNDLED-RUST.md']
    with zipfile.ZipFile(fixture.artifact) as archive:
        manifest = json.loads(archive.read(collect_notices.RUST_ARTIFACT_MANIFEST))
    assert set(manifest['files']) == contents.keys()
    assert manifest['identity'] == collect_notices.rust_artifact_identity(fixture.root)


def test_fresh_consumer_copies_notices_without_crates_toolchain_or_rust_calls(native_distribution, monkeypatch):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    expected, _ = collect_notices.read_rust_artifact(fixture.artifact, fixture.root)
    shutil.rmtree(fixture.root/'registry')
    shutil.rmtree(fixture.sysroot)
    forbid_rust(monkeypatch)
    product = fixture.root/'product'
    assert collect_notices.collect_rust_notices(product, fixture.root, rust_artifact=fixture.artifact) == 3
    actual = {path.relative_to(product).as_posix(): path.read_bytes()
              for path in product.rglob('*') if path.is_file()}
    assert actual == expected


def test_producer_cache_hit_needs_no_cargo_cache_or_rust_calls(native_distribution, monkeypatch):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    original = fixture.artifact.read_bytes()
    shutil.rmtree(fixture.root/'registry')
    shutil.rmtree(fixture.sysroot)
    forbid_rust(monkeypatch)
    assert collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root) == fixture.artifact.resolve()
    assert fixture.artifact.read_bytes() == original


@pytest.mark.parametrize('changed', ['native/Cargo.toml', 'native/Cargo.lock', 'rust-toolchain.toml',
                                    'tools/collect_notices.py', 'native_receipt', 'native_binary'])
def test_consumer_rejects_stale_notice_binding_without_network_fallback(native_distribution, monkeypatch, changed):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    if changed == 'native_receipt':
        path = build_native.receipt_path(fixture.root)
        receipt = json.loads(path.read_text(encoding='utf-8'))
        receipt['runner_image_version'] = 'changed diagnostic receipt'
        path.write_text(json.dumps(receipt), encoding='utf-8')
    elif changed == 'native_binary':
        build_native.extension_path(fixture.root).write_bytes(b'different tested native extension')
        native_receipt(fixture.root)
    else:
        with (fixture.root/changed).open('a', encoding='utf-8') as output:
            output.write('changed input\n')
        native_receipt(fixture.root)
    forbid_rust(monkeypatch)
    product = fixture.root/'rejected-product'
    with pytest.raises(collect_notices.InvalidRustArtifact, match='stale'):
        collect_notices.collect_rust_notices(product, fixture.root, rust_artifact=fixture.artifact)
    assert not product.exists()


def test_stale_producer_archive_regenerates_from_locked_metadata(native_distribution):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    before = fixture.artifact.read_bytes()
    (fixture.root/'tools/collect_notices.py').write_text('new collector', encoding='utf-8')
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    assert fixture.artifact.read_bytes() != before
    assert sum(command[0] == 'cargo' for command in fixture.calls) == 2
    assert collect_notices.read_rust_artifact(fixture.artifact, fixture.root)[1] == 3


@pytest.mark.parametrize('corruption', ['missing_archive', 'invalid_zip', 'missing_license',
                                       'changed_license', 'missing_inventory', 'missing_standard'])
def test_consumer_rejects_corrupt_or_incomplete_archive_before_copy(native_distribution, monkeypatch, corruption):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    if corruption == 'missing_archive':
        fixture.artifact.unlink()
    elif corruption == 'invalid_zip':
        fixture.artifact.write_bytes(b'not a zip file')
    else:
        def corrupt(contents):
            license_name = 'LICENSES/rust/runtime-1.0.0/LICENSE-MIT'
            if corruption == 'changed_license':
                contents[license_name] = b'changed license bytes'
            else:
                removed = {'missing_license': license_name,
                           'missing_inventory': 'BUNDLED-RUST.md',
                           'missing_standard': collect_notices.RUST_STANDARD_NOTICE}[corruption]
                del contents[removed]
        rewrite_archive(fixture.artifact, corrupt)
    forbid_rust(monkeypatch)
    product = fixture.root/'rejected-product'
    with pytest.raises(collect_notices.InvalidRustArtifact):
        collect_notices.copy_rust_artifact(product, fixture.artifact, fixture.root)
    assert not product.exists()


@pytest.mark.parametrize('unsafe', ['../escape.txt', '/escape.txt',
                                   'LICENSES/rust/crate/../../../../escape.txt',
                                   'LICENSES\\rust\\crate\\LICENSE',
                                   'LICENSES/rust/crate/C:license'])
def test_archive_paths_cannot_escape_the_product(native_distribution, unsafe):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    with zipfile.ZipFile(fixture.artifact, 'a') as archive:
        member = zipfile.ZipInfo(unsafe)
        member.filename = unsafe
        archive.writestr(member, b'outside content')
    product = fixture.root/'rejected-product'
    with pytest.raises(collect_notices.InvalidRustArtifact, match='unsafe path'):
        collect_notices.copy_rust_artifact(product, fixture.artifact, fixture.root)
    assert not product.exists()
    assert not (fixture.root/'escape.txt').exists()


def test_archive_links_are_rejected_before_copy(native_distribution):
    fixture = native_distribution
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    with zipfile.ZipFile(fixture.artifact, 'a') as archive:
        link = zipfile.ZipInfo('LICENSES/rust/extra/LICENSE')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b'../../outside.txt')
    with pytest.raises(collect_notices.InvalidRustArtifact, match='linked members'):
        collect_notices.copy_rust_artifact(fixture.root/'product', fixture.artifact, fixture.root)


def test_producer_requires_the_native_builds_exact_rust_toolchain(native_distribution, monkeypatch):
    fixture = native_distribution
    monkeypatch.setattr(collect_notices.subprocess, 'check_output', lambda *args, **kwargs: 'different rustc')
    with pytest.raises(RuntimeError, match='toolchain differs'):
        collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    assert not fixture.artifact.exists()


def test_failed_cargo_metadata_cannot_produce_an_archive(native_distribution, monkeypatch):
    fixture = native_distribution
    command = collect_notices.subprocess.check_output
    def fail(arguments, **kwargs):
        if arguments[0] == 'cargo':
            raise subprocess.CalledProcessError(1, arguments)
        return command(arguments, **kwargs)
    monkeypatch.setattr(collect_notices.subprocess, 'check_output', fail)
    with pytest.raises(subprocess.CalledProcessError):
        collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    assert not fixture.artifact.exists()


def test_producer_cli_does_not_require_installed_python_distributions(native_distribution, monkeypatch, capsys):
    fixture = native_distribution
    prepare = collect_notices.prepare_rust_artifact
    monkeypatch.setattr(collect_notices, 'prepare_rust_artifact', lambda artifact: prepare(artifact, fixture.root))
    def forbidden():
        raise AssertionError('Producer must not inspect installed Python distributions')
    monkeypatch.setattr(collect_notices.metadata, 'packages_distributions', forbidden)
    assert collect_notices.main(['--prepare-rust-artifact', str(fixture.artifact)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['rust_artifact'] == str(fixture.artifact.resolve())
    assert output['sha256'] == build_native.binary_digest(fixture.artifact)



def test_binary_only_native_cache_fetches_locked_crates_for_missing_notices(native_distribution, monkeypatch):
    fixture = native_distribution
    licenses = {Path(package['manifest_path']).parent/'LICENSE-MIT':
                (Path(package['manifest_path']).parent/'LICENSE-MIT').read_bytes()
                for package in fixture.graph['packages']}
    shutil.rmtree(fixture.root/'registry')
    command = collect_notices.subprocess.check_output
    def fetch_locked_crates(arguments, **kwargs):
        if arguments[0] == 'cargo':
            assert '--locked' in arguments and '--offline' not in arguments
            for path, data in licenses.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        return command(arguments, **kwargs)
    monkeypatch.setattr(collect_notices.subprocess, 'check_output', fetch_locked_crates)
    assert not fixture.artifact.exists()
    collect_notices.prepare_rust_artifact(fixture.artifact, fixture.root)
    assert sum(arguments[0] == 'cargo' for arguments in fixture.calls) == 1
    forbid_rust(monkeypatch)
    assert collect_notices.read_rust_artifact(fixture.artifact, fixture.root)[1] == 3
