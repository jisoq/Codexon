"""Run changed packaged QA tools against a matching published binary, without rebuilding it."""
import argparse
import fnmatch
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_pipeline import version

# Reuse is valid only when every packaged input still matches the public tag.
INPUTS = ('cachemonitor/*', 'icons/*', 'LICENSES/*', 'requirements*', '*.spec', 'build*.ps1',
          'run.py', 'recovery_main.py', 'installer/*', 'tools/Build-*.ps1',
          'tools/collect_notices.py', 'tools/package_release.py', 'tools/prepare_sources.py',
          'tools/release_github.py', 'third-party-sources.lock.json', 'SOURCE-OFFER.md',
          'THIRD-PARTY-NOTICES.md', 'LICENSE', 'README*')


def can_reuse(root=ROOT):
    result = subprocess.run(['git', 'diff', '--name-only', 'v' + version(root), 'HEAD'],
                            cwd=root, capture_output=True, text=True)
    return result.returncode == 0 and not any(
        fnmatch.fnmatchcase(path, pattern) for path in result.stdout.splitlines() for pattern in INPUTS)


def commands(names, old, new, output):
    result = []
    for name in names:
        target = output/name
        if name == 'proxy_update':
            result.append(['tools/verify_proxy_update.py', '--old-exe', old, '--new-exe', new,
                           '--idle-connections', '100', '--output', target])
        elif name == 'app_services':
            result.append(['tools/verify_app_services.py', '--executable', new,
                           '--legacy-collector-exe', old, '--output', target])
        elif name == 'retired_worker':
            result.append(['tools/verify_retired_worker.py', '--old-exe', old, '--new-exe', new, '--output', target])
        elif name == 'gui_handoff':
            result.append(['tools/verify_gui_handoff.py', '--executable', new, '--output', target])
        elif name == 'recovery':
            result.append(['tools/verify_recovery.py', '--executable', Path(new).with_name('CodexonRecovery.exe'),
                           '--output', target])
        else:
            raise ValueError('Unknown packaged QA tool: ' + name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--can-reuse', action='store_true')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/verification/changed-release-tools')
    args = parser.parse_args()
    if args.can_reuse:
        print(json.dumps(dict(reuse=can_reuse())))
        return
    if not can_reuse():
        raise ValueError('Packaged inputs changed; build the current product before running QA tools')
    names = json.loads(args.plan.read_text(encoding='utf-8-sig'))['qa_smokes']
    def published(tag, destination):
        subprocess.run([sys.executable, str(ROOT/'tools/prepare_legacy_release.py'),
                        '--tag', tag, '--output', str(destination)], check=True, cwd=ROOT)
        return json.loads((destination/'result.json').read_text(encoding='utf-8'))['executable']
    new = published('v' + version(), args.output/'current')
    old = published('v2026.09.25.6', args.output/'previous') if any(
        name in ('proxy_update', 'app_services', 'retired_worker') for name in names) else ''
    for command in commands(names, old, new, args.output):
        subprocess.run([sys.executable, *map(str, command)], check=True, cwd=ROOT)


if __name__ == '__main__':
    main()
