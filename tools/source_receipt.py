"""Source coverage receipts are independent of package and publication status."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re

POLICY = 'source-coverage-v3'
INPUTS = ('requirements-release.lock', 'rust-toolchain.toml', 'native/Cargo.lock',
          'pytest.ini', 'tests/conftest.py', 'tools/verify_changes.py', 'tools/ci_plan.py',
          'tools/check_environment.py', 'tools/run_ui_checks.py', 'tools/check_processes.py',
          'tools/check_progress.py', 'tools/check_worker.py', 'tools/source_receipt.py',
          'tools/build_native.py', 'tools/clear_check_scopes.ps1')


def runner_environment():
    return dict(python=platform.python_version(), system=platform.system(),
                machine=platform.machine(), image_os=os.environ.get('ImageOS', ''),
                image_version=os.environ.get('ImageVersion', ''))


def without_comment(line):
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote and (not index or line[index - 1] not in ('\\', '`')):
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == '#' and (not index or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def workflow_contract(root):
    path = root / '.github/workflows/windows.yml'
    if not path.exists():
        return ''
    jobs, current, step = [], None, []
    global_environment = False

    def add_step():
        if not step:
            return
        text = '\n'.join(step)
        if 'uses: actions/upload-artifact@' in text or 'release_pipeline.py source-receipt' in text:
            return
        jobs.extend(re.sub(r'^      - ', '        ', line) for line in step
                    if not re.match(r'\s*-?\s*(name|id):', line))

    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = without_comment(line)
        if not line.strip():
            continue
        if line == 'env:':
            global_environment = True
        elif line and not line.startswith(' '):
            global_environment = False
        if global_environment:
            jobs.append(line)
        job = re.fullmatch(r'  ([a-zA-Z_][\w-]*):', line)
        if job:
            add_step()
            step = []
            current = job[1] if job[1] in ('source', 'native') else None
        if current is None:
            continue
        if line.startswith('      - '):
            add_step()
            step = [line]
        elif step:
            step.append(line)
        else:
            jobs.append(line)
    add_step()
    return '\n'.join(jobs)


def environment_key(root):
    root = Path(root)
    value = hashlib.sha256(POLICY.encode())
    environment = runner_environment()
    for name in ('python', 'system', 'machine', 'image_os'):
        value.update(environment[name].encode() + b'\0')
    for name in INPUTS:
        path = root / name
        value.update(name.encode() + b'\0')
        value.update(path.read_bytes().replace(b'\r\n', b'\n') if path.exists() else b'missing')
    value.update(workflow_contract(root).encode())
    return value.hexdigest()


def check_environment(plan, sha, root):
    key = environment_key(root)
    if key != plan['source_environment'] or sha != plan['commit']:
        raise ValueError('Source environment changed after planning; replan this run before running checks')
    return dict(environment=key, runner=runner_environment(), commit=sha)


def coverage_suite(args, affected=()):
    if '--extended' in args:
        return 'extended'
    if '--integration' in args:
        return 'integration'
    if '--full' in args:
        return 'essential+affected' if affected else 'essential'
    return 'affected'


def write_receipt(folder, plan, sha, run_id, root):
    environment = check_environment(plan, sha, root)
    affected = list(plan.get('affected_tests', ()))
    suite = coverage_suite(plan['source_args'], affected)
    if plan.get('source_suite', suite) != suite:
        raise ValueError('Source coverage profile does not match the executed arguments')
    receipt = dict(schema=2, policy=POLICY, commit=sha, run_id=str(run_id),
                   environment=environment['environment'], runner=environment['runner'],
                   base=plan['source_base'], comparison_base=plan.get('source_comparison_base', plan['source_base']),
                   scope=list(plan['tests']), args=plan['source_args'],
                   suite=suite, affected_tests=affected, scales=plan['scales'], complete=True)
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(receipt, indent=2)
    if len(text.encode('utf-8')) > 32768:
        raise ValueError('Source receipt exceeds the 32 KiB metadata budget')
    (folder / 'source-verification.json').write_text(text, encoding='utf-8')
    return receipt


def valid(receipt, sha, run_id, root):
    if not (isinstance(receipt, dict) and receipt.get('schema') == 2
            and receipt.get('policy') == POLICY and receipt.get('commit') == sha
            and receipt.get('run_id') == str(run_id)
            and receipt.get('environment') == environment_key(root)
            and receipt.get('complete') is True):
        return False
    scope, args, affected = (receipt.get(name) for name in ('scope', 'args', 'affected_tests'))
    if not all(isinstance(items, list) and all(isinstance(item, str) for item in items)
               for items in (scope, args, affected)):
        return False
    return receipt.get('suite') == coverage_suite(args, affected)
