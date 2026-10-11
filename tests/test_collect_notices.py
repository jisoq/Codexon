"""Native distributions retain the exact dependency and standard-library notices."""
from pathlib import Path

import pytest

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
