"""One testable policy for source, packaging and compatibility checks in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_changes import changed_files, file_hashes, matches, release_version_only, select_tests, tests_require_native
from tools.verify_release_tools import can_reuse
from tools.source_receipt import coverage_suite, environment_key, runner_environment


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


def build_plan(changed, *, base='', candidate=False, extended=False, reuse_qa=False, deleted=(), version_only=False,
               package_changed=..., package_version_only=False, fallback_base='', release_artifact=False):
    impact = select_tests(changed, deleted=deleted, version_only=version_only)
    deployment = impact if package_changed is ... else select_tests(
        package_changed, deleted=deleted, version_only=package_version_only)
    # A release keeps the last successful ancestor's source coverage. A version
    # bump must not erase that baseline and rerun unrelated source checks.
    full = extended or impact['full']
    package = candidate or extended or deployment['package_impact']
    qa_reuse = bool(deployment['qa_smokes']) and not package and reuse_qa
    package = package or (bool(deployment['qa_smokes']) and not qa_reuse)
    release_artifact = bool(package and release_artifact)
    deployment_changes = changed if package_changed is ... else package_changed
    unknown = deployment_changes is None
    paths = deployment_changes or ()
    install_changed = unknown or any(matches(p, INSTALL_PATTERNS) for p in paths)
    # The MSIX test already performs the complete native installation lifecycle.
    # Select one origin instead of executing that lifecycle twice.
    msix = package and (extended or install_changed)
    proxy = extended or deployment['proxy_impact'] or (
        not qa_reuse and bool(set(deployment['qa_smokes']) & {'proxy_update', 'app_services', 'retired_worker'}))
    args = ['--extended'] if extended else ['--full'] if full else []
    comparison = base or (fallback_base if changed is None else '')
    if comparison and not extended:
        args += ['--base', comparison]
    affected = (deployment if changed is None and comparison else impact).get('affected_tests', ())
    tests = ('tests',) if full else impact['tests']
    return dict(impact, full=full, tests=tests, affected_tests=affected,
                source_suite=coverage_suite(args, affected), source_comparison_base=comparison,
                unmapped=sorted(set(impact['unmapped']) | set(deployment['unmapped'])),
                qa_smokes=deployment['qa_smokes'],
                source_args=args, package=bool(package), proxy=bool(proxy), release_artifact=release_artifact,
                rust=bool(package or tests_require_native(tests)),
                msix=bool(msix), native=bool(package and not msix), qa_reuse=qa_reuse,
                scales=extended or changed is None or any(matches(p, SCALE_PATTERNS) for p in changed or ()),
                dependencies=candidate or release_artifact or extended or unknown or any(p.startswith('requirements') for p in paths),
                changed=sorted(paths) if not unknown else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='')
    parser.add_argument('--source-base')
    parser.add_argument('--release-plan', type=Path)
    parser.add_argument('--candidate', choices=('true', 'false'), default='false')
    parser.add_argument('--extended', choices=('true', 'false'), default='false')
    args = parser.parse_args()
    base = args.base if args.base and set(args.base) != {'0'} else ''
    source = base if args.source_base is None else args.source_base
    current = file_hashes()
    changed = changed_files(current, base_ref=source) if source else None
    package_changed = changed_files(current, base_ref=base) if base else None
    impact = select_tests(package_changed)
    reuse = can_reuse() if impact['qa_smokes'] and not impact['package_impact'] else False
    release = json.loads(args.release_plan.read_text(encoding='utf-8')) if args.release_plan else {}
    plan = build_plan(changed, base=source, candidate=args.candidate == 'true',
                      extended=args.extended == 'true', reuse_qa=reuse,
                      deleted=(set(changed or ()) | set(package_changed or ())) - current.keys(),
                      version_only=release_version_only(base_ref=source),
                      package_changed=package_changed, package_version_only=release_version_only(base_ref=base),
                      fallback_base=base, release_artifact=release.get('release_artifact', False))
    plan.update(source_base=source, package_base=base, source_environment=environment_key(ROOT),
                source_runner=runner_environment(),
                commit=os.environ.get('GITHUB_SHA', ''), candidate=args.candidate == 'true',
                verify=release.get('verify', True) or args.extended == 'true',
                reuse_run=release.get('reuse_run', ''), published=release.get('published', False))
    if plan['unmapped']:
        raise ValueError('Unmapped changes: ' + ', '.join(plan['unmapped']))
    folder = ROOT/'artifacts/verification'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'plan.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            print('plan=' + json.dumps(plan, ensure_ascii=True), file=output)
            for key in ('package', 'proxy', 'native', 'msix', 'scales', 'dependencies', 'qa_reuse', 'rust'):
                print(f'{key}={str(plan[key]).lower()}', file=output)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
