"""Build and verify the locked Rust extension with the selected Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig


ROOT = Path(__file__).resolve().parents[1]
ABI_VERSION = 1
RECEIPT_SCHEMA = 2


class InvalidBuild(RuntimeError):
    pass


def extension_path(root=ROOT):
    suffix = '.pyd' if sys.platform == 'win32' else '.abi3.so'
    return Path(root) / 'cachemonitor' / ('_cpu_native' + suffix)


def source_digest(root=ROOT):
    root = Path(root)
    native = root / 'native'
    paths = [native / 'Cargo.toml', native / 'Cargo.lock', root / 'rust-toolchain.toml']
    paths.extend(path for path in (native / 'src').rglob('*') if path.is_file())
    if (native / 'build.rs').is_file():
        paths.append(native / 'build.rs')
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


def binary_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def receipt_path(root=ROOT):
    return Path(root) / 'cachemonitor' / '_cpu_native.build.json'


def toolchain_identity(root):
    result = {}
    for tool, arguments in (('rustc', ['--version', '--verbose']), ('cargo', ['--version'])):
        executable = shutil.which(tool)
        if not executable:
            raise RuntimeError('The pinned Rust toolchain is required; install rustup before building.')
        probe = subprocess.run([executable, *arguments], cwd=root, check=True,
                               stdout=subprocess.PIPE, text=True)
        result[tool] = probe.stdout.strip()
    return result


def compiler_identity():
    if sys.platform != 'win32':
        result = {}
        for name in ('cc', 'ld'):
            executable = shutil.which(name)
            if executable:
                result[name] = binary_digest(executable)
        return result
    program_files = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    vswhere = Path(program_files) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    if not vswhere.is_file():
        raise RuntimeError('Microsoft C++ Build Tools and vswhere are required for native builds.')
    probe = subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires',
                            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json', '-utf8'],
                           check=True, stdout=subprocess.PIPE, text=True, encoding='utf-8')
    installations = json.loads(probe.stdout)
    if len(installations) != 1:
        raise RuntimeError('Could not identify the Microsoft C++ toolchain for native builds.')
    installation = installations[0]
    toolsets = {}
    tools = Path(installation['installationPath']) / 'VC/Tools/MSVC'
    for directory in sorted(tools.glob('*/bin/Hostx64/x64')):
        toolsets[directory.parents[2].name] = {
            name: binary_digest(directory / name) for name in ('cl.exe', 'link.exe')
        }
    if not toolsets:
        raise RuntimeError('Microsoft x64 C++ compiler and linker are missing.')
    sdk = Path(program_files) / 'Windows Kits/10/Include'
    return {'installation_version': installation['installationVersion'], 'toolsets': toolsets,
            'sdk_versions': sorted(path.name for path in sdk.glob('*') if path.is_dir())}


def build_identity(root=ROOT):
    root = Path(root).resolve()
    cargo_home = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
    configs = {}
    for label, folder in (('repository', root / '.cargo'), ('native', root / 'native/.cargo'),
                          ('user', cargo_home),
                          *((f'ancestor-{index}', parent / '.cargo')
                            for index, parent in enumerate(root.parents, start=1))):
        for name in ('config', 'config.toml'):
            path = folder / name
            if path.is_file():
                configs[f'{label}/{name}'] = binary_digest(path)
    ignored = {'CARGO_HOME', 'RUSTUP_HOME', 'PYO3_PYTHON'}
    environment = {name: value for name, value in os.environ.items()
                   if name not in ignored and (name.startswith(('CARGO_', 'RUST', 'PYO3_',
                                                                'CC_', 'CXX_', 'AR_'))
                   or name in {'CC', 'CXX', 'AR', 'CFLAGS', 'CPPFLAGS', 'CXXFLAGS', 'LDFLAGS',
                               'VCToolsVersion', 'WindowsSDKVersion', 'VSCMD_ARG_TGT_ARCH',
                               'ImageOS', 'CL', '_CL_', 'VSINSTALLDIR', 'VCINSTALLDIR',
                               'VCToolsInstallDir', 'WindowsSdkDir', 'UCRTVersion', 'UniversalCRTSdkDir'})}
    return {
        'schema': RECEIPT_SCHEMA,
        'abi_version': ABI_VERSION,
        'source_sha256': source_digest(root),
        'build_helper_sha256': binary_digest(root / 'tools/build_native.py'),
        'requirements_sha256': binary_digest(root / 'requirements-release.lock'),
        'python': {'implementation': sys.implementation.name, 'version': sys.version,
                   'cache_tag': sys.implementation.cache_tag, 'soabi': sysconfig.get_config_var('SOABI'),
                   'extension_suffix': sysconfig.get_config_var('EXT_SUFFIX'),
                   'gil_disabled': sysconfig.get_config_var('Py_GIL_DISABLED')},
        'platform': {'system': sys.platform, 'machine': platform.machine(),
                     'platform': sysconfig.get_platform(), 'bits': struct.calcsize('P') * 8},
        'toolchain': toolchain_identity(root),
        'compiler': compiler_identity(),
        'cargo_config': configs,
        'environment': environment,
    }


def cache_key(root=ROOT, *, identity=None):
    identity = build_identity(root) if identity is None else identity
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return f'native-v{RECEIPT_SCHEMA}-' + hashlib.sha256(encoded).hexdigest()


def built_extension(messages, manifest, target):
    """Use Cargo's actual output, including configured target subdirectories."""
    manifest, target = Path(manifest).resolve(), Path(target).resolve()
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    candidates = set()
    for line in messages.splitlines():
        event = json.loads(line)
        if (event.get('reason') == 'compiler-artifact'
                and Path(event['manifest_path']).resolve() == manifest
                and event['target']['name'] == '_cpu_native'
                and 'cdylib' in event['target']['crate_types']):
            candidates.update(Path(name).resolve() for name in event['filenames'] if name.endswith(suffix))
    if len(candidates) != 1:
        raise RuntimeError('Cargo did not report exactly one native extension artifact')
    binary = candidates.pop()
    if not binary.is_relative_to(target) or not binary.is_file():
        raise RuntimeError('Cargo native extension artifact is missing or outside the target directory')
    return binary


def smoke_extension(root=ROOT):
    """Import in a fresh process so checks cannot reuse an already-loaded DLL."""
    root = Path(root).resolve()
    script = (
        'import importlib, pathlib, sys; '
        'sys.path.insert(0, sys.argv[1]); '
        'm = importlib.import_module("cachemonitor._cpu_native"); '
        'assert pathlib.Path(m.__file__).resolve() == pathlib.Path(sys.argv[2]).resolve(), '
        '"Native extension loaded from an unexpected location"; '
        f'assert m.ABI_VERSION == {ABI_VERSION}, "Native extension ABI mismatch"'
    )
    subprocess.run([sys.executable, '-I', '-c', script, str(root), str(extension_path(root))],
                   cwd=root, check=True)


def checked_extension(root=ROOT, *, smoke=True, require_tested=False, identity=None):
    root = Path(root).resolve()
    binary = extension_path(root)
    identity = build_identity(root) if identity is None else identity
    try:
        receipt = json.loads(receipt_path(root).read_text(encoding='utf-8'))
        valid = (isinstance(receipt, dict) and receipt.get('schema') == RECEIPT_SCHEMA
                 and receipt.get('identity') == identity
                 and receipt.get('binary_sha256') == binary_digest(binary)
                 and (not require_tested or receipt.get('tests_passed') is True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InvalidBuild('Rust extension is missing or unverified; run tools/build_native.py --test.') from error
    if not valid:
        raise InvalidBuild('Rust extension is stale or changed, or lacks required tests; run tools/build_native.py --test.')
    if smoke:
        smoke_extension(root)
    return binary.resolve()


def build_native(root=ROOT, *, test=False, offline=False, reuse=False):
    root = Path(root).resolve()
    native = root / 'native'
    target = native / 'target'
    identity = build_identity(root)
    if reuse:
        try:
            return checked_extension(root, require_tested=test, identity=identity)
        except (InvalidBuild, subprocess.CalledProcessError) as error:
            print(f'Cached native build rejected; rebuilding: {error}', file=sys.stderr)
    receipt_path(root).unlink(missing_ok=True)
    cargo = shutil.which('cargo')
    env = os.environ.copy()
    env['PYO3_PYTHON'] = sys.executable
    arguments = ['--manifest-path', str(native / 'Cargo.toml'), '--release', '--locked',
                 '--target-dir', str(target)]
    if offline:
        arguments.append('--offline')
    if test:
        subprocess.run([cargo, 'test', *arguments], cwd=root, env=env, check=True)
    result = subprocess.run([cargo, 'build', *arguments, '--message-format=json-render-diagnostics'],
                            cwd=root, env=env, check=True, stdout=subprocess.PIPE, text=True)
    if build_identity(root) != identity:
        raise RuntimeError('Native build inputs changed during compilation; build again before packaging.')
    built = built_extension(result.stdout, native / 'Cargo.toml', target)
    binary = extension_path(root)
    if not binary.is_file() or binary_digest(binary) != binary_digest(built):
        temporary = binary.with_name(binary.name + '.tmp')
        shutil.copyfile(built, temporary)
        os.replace(temporary, binary)
    smoke_extension(root)
    temporary = receipt_path(root).with_suffix('.json.tmp')
    temporary.write_text(json.dumps({
        'schema': RECEIPT_SCHEMA,
        'identity': identity,
        'binary_sha256': binary_digest(binary),
        'tests_passed': test,
        'runner_image_version': os.environ.get('ImageVersion', ''),
    }, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, receipt_path(root))
    return binary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-key', action='store_true', help='Print the exact native cache key before pip installation')
    parser.add_argument('--check', action='store_true', help='Require an existing verified native build')
    parser.add_argument('--reuse', action='store_true', help='Reuse a matching verified binary, otherwise rebuild')
    parser.add_argument('--test', action='store_true', help='Require Rust unit tests before building or reusing')
    parser.add_argument('--offline', action='store_true', help='Use only locally cached Cargo dependencies')
    args = parser.parse_args(argv)
    if args.cache_key:
        if args.check or args.reuse or args.test or args.offline:
            parser.error('--cache-key cannot be combined with build or check options')
        print(cache_key())
        return 0
    if args.check and (args.reuse or args.offline):
        parser.error('--check cannot be combined with --reuse or --offline')
    binary = (checked_extension(require_tested=args.test) if args.check
              else build_native(test=args.test, offline=args.offline, reuse=args.reuse))
    print(json.dumps({'native_extension': str(binary), 'abi_version': ABI_VERSION,
                      'sha256': binary_digest(binary)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
