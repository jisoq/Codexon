"""Build and verify the locked Rust extension with the selected Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ABI_VERSION = 1


def extension_path(root=ROOT):
    suffix = '.pyd' if sys.platform == 'win32' else '.abi3.so'
    return Path(root) / 'cachemonitor' / ('_cpu_native' + suffix)


def source_digest(root=ROOT):
    root = Path(root)
    native = root / 'native'
    paths = [native / 'Cargo.toml', native / 'Cargo.lock', root / 'rust-toolchain.toml']
    paths.extend((native / 'src').rglob('*.rs'))
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


def checked_extension(root=ROOT, *, smoke=True):
    """Reject missing, stale or replaced binaries before creating a release."""
    root = Path(root)
    binary = extension_path(root)
    try:
        receipt = json.loads(receipt_path(root).read_text(encoding='utf-8'))
        valid = (receipt['source_sha256'] == source_digest(root)
                 and receipt['binary_sha256'] == binary_digest(binary)
                 and receipt['abi_version'] == ABI_VERSION)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Rust extension is missing or unverified; run tools/build_native.py.') from error
    if not valid:
        raise RuntimeError('Rust extension is stale or changed; run tools/build_native.py.')
    if smoke:
        smoke_extension(root)
    return binary.resolve()


def build_native(root=ROOT, *, test=False, offline=False):
    root = Path(root).resolve()
    native = root / 'native'
    target = native / 'target'
    source_before = source_digest(root)
    cargo = shutil.which('cargo')
    if not cargo:
        raise RuntimeError('The pinned Rust toolchain is required; install rustup before building.')
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
    if source_digest(root) != source_before:
        raise RuntimeError('Rust sources changed during compilation; build again before packaging.')
    built = built_extension(result.stdout, native / 'Cargo.toml', target)
    binary = extension_path(root)
    if not binary.is_file() or binary_digest(binary) != binary_digest(built):
        temporary = binary.with_name(binary.name + '.tmp')
        shutil.copyfile(built, temporary)
        os.replace(temporary, binary)
    smoke_extension(root)
    receipt_path(root).write_text(json.dumps({
        'abi_version': ABI_VERSION,
        'source_sha256': source_before,
        'binary_sha256': binary_digest(binary),
    }, indent=2) + '\n', encoding='utf-8')
    return binary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Require an existing verified native build')
    parser.add_argument('--test', action='store_true', help='Run Rust unit tests before building')
    parser.add_argument('--offline', action='store_true', help='Use only locally cached Cargo dependencies')
    args = parser.parse_args(argv)
    if args.check and (args.test or args.offline):
        parser.error('--check cannot be combined with build options')
    binary = checked_extension() if args.check else build_native(test=args.test, offline=args.offline)
    print(json.dumps({'native_extension': str(binary), 'abi_version': ABI_VERSION,
                      'sha256': binary_digest(binary)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
