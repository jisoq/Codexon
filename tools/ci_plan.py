"""One testable policy for source, packaging and compatibility checks in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_changes import changed_files, file_hashes, matches, release_version_only, select_tests
from tools.verify_release_tools import can_reuse


INSTALL_PATTERNS = (
    'cachemonitor/install*.py', 'cachemonitor/launch_context.py',
    'cachemonitor/shell_shortcut.py', 'cachemonitor/windows_integration.py',
    'cachemonitor/connection_recovery.py', 'recovery_main.py', 'installer/*',
    'tools/Build-*.ps1', 'tools/*installation*', 'tools/run_native_checks.py',
    'tools/prepare_bad_runtime.py', 'requirements*', 'build*.ps1', '*.spec',
)
SCALE_PATTERNS = (
    'cachemonitor/overlay*.py', 'cachemonitor/screens.py',
    'cachemonitor/quick_runtime.py', 'cachemonitor/qml/Overlay*.qml',
    'tools/run_ui_checks.py', 'tests/test_overlay*', 'tests/test_display_scaling.py',
    'requirements*',
)


def build_plan(changed, *, base='', candidate=False, extended=False, reuse_qa=False, deleted=(), version_only=False):
    impact = select_tests(changed, deleted=deleted, version_only=version_only)
    # A release keeps the last successful ancestor's source coverage. A version
    # bump must not erase that baseline and rerun unrelated source checks.
    full = extended or impact['full']
    package = candidate or extended or impact['package_impact']
    qa_reuse = bool(impact['qa_smokes']) and not package and reuse_qa
    package = package or (bool(impact['qa_smokes']) and not qa_reuse)
    unknown = changed is None
    paths = changed or ()
    install_changed = unknown or any(matches(p, INSTALL_PATTERNS) for p in paths)
    # The MSIX test already performs the complete native installation lifecycle.
    # Select one origin instead of executing that lifecycle twice.
    msix = package and (extended or install_changed)
    proxy = extended or impact['proxy_impact'] or (
        not qa_reuse and bool(set(impact['qa_smokes']) & {'proxy_update', 'app_services', 'retired_worker'}))
    args = ['--extended'] if extended else ['--integration'] if full else ['--base', base]
    return dict(impact, full=full, tests=('tests',) if full else impact['tests'],
                source_args=args, package=bool(package), proxy=bool(proxy),
                msix=bool(msix), native=bool(package and not msix), qa_reuse=qa_reuse,
                scales=extended or unknown or any(matches(p, SCALE_PATTERNS) for p in paths),
                dependencies=candidate or extended or unknown or any(p.startswith('requirements') for p in paths),
                changed=sorted(paths) if not unknown else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='')
    parser.add_argument('--candidate', choices=('true', 'false'), default='false')
    parser.add_argument('--extended', choices=('true', 'false'), default='false')
    args = parser.parse_args()
    base = args.base if args.base and set(args.base) != {'0'} else ''
    current = file_hashes()
    changed = changed_files(current, base_ref=base) if base else None
    impact = select_tests(changed)
    reuse = can_reuse() if impact['qa_smokes'] and not impact['package_impact'] else False
    plan = build_plan(changed, base=base, candidate=args.candidate == 'true',
                      extended=args.extended == 'true', reuse_qa=reuse,
                      deleted=set(changed or ()) - current.keys(),
                      version_only=release_version_only(base_ref=base))
    if plan['unmapped']:
        raise ValueError('Unmapped changes: ' + ', '.join(plan['unmapped']))
    folder = ROOT/'artifacts/verification'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'plan.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            for key in ('package', 'proxy', 'native', 'msix', 'scales', 'dependencies', 'qa_reuse'):
                print(f'{key}={str(plan[key]).lower()}', file=output)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
