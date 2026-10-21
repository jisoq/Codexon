"""One testable policy for source, packaging and compatibility checks in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_changes import basic_tests, changed_files, file_hashes, release_version_only, select_tests, tests_require_native
from tools.verify_release_tools import can_reuse
from tools.source_receipt import coverage_suite, environment_key, runner_environment


def select_qa_smokes(smokes, *, extended=False):
    return [name for name in smokes if extended or name == 'recovery']


def build_plan(changed, *, base='', candidate=False, extended=False, reuse_qa=False, deleted=(), version_only=False,
               package_changed=..., package_version_only=False, fallback_base='', release_artifact=False):
    impact = select_tests(changed, deleted=deleted, version_only=version_only)
    deployment = impact if package_changed is ... else select_tests(
        package_changed, deleted=deleted, version_only=package_version_only)
    tests = ('tests',) if extended else basic_tests()
    affected = impact['affected_tests'] if extended else ()
    args = ['--extended'] if extended else ['--basic']
    smokes = select_qa_smokes(deployment['qa_smokes'], extended=extended)
    package = candidate or extended or deployment['package_impact']
    qa_reuse = bool(smokes) and not package and reuse_qa
    package = package or (bool(smokes) and not qa_reuse)
    release_artifact = bool(package and release_artifact)
    deployment_changes = changed if package_changed is ... else package_changed
    unknown = deployment_changes is None
    paths = deployment_changes or ()
    msix = package and extended
    return dict(impact, full=extended, tests=tests, affected_tests=affected,
                groups=('full',) if extended else ('basic',),
                reasons={'tests':['Explicit extended CI checks']} if extended else
                        {test:['Audited basic CI contract'] for test in tests},
                source_suite=coverage_suite(args, affected), source_comparison_base='',
                unmapped=sorted(set(impact['unmapped']) | set(deployment['unmapped'])),
                qa_smokes=smokes,
                source_args=args, package=bool(package), proxy=bool(extended), release_artifact=release_artifact,
                rust=bool(package or tests_require_native(tests)),
                msix=bool(msix), native=bool(package and not msix), qa_reuse=qa_reuse,
                scales=bool(extended), gui_handoff=bool(package and extended),
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
    smokes = select_qa_smokes(impact['qa_smokes'], extended=args.extended == 'true')
    reuse = can_reuse() if smokes and not impact['package_impact'] else False
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
            for key in ('package', 'proxy', 'native', 'msix', 'scales', 'dependencies', 'qa_reuse', 'rust', 'gui_handoff'):
                print(f'{key}={str(plan[key]).lower()}', file=output)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
