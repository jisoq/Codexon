"""Source coverage receipts are independent of package and publication status."""
import hashlib
import json
import os
from pathlib import Path
import platform

POLICY = 'source-coverage-v2'
INPUTS = ('requirements-release.lock', 'rust-toolchain.toml', 'native/Cargo.lock',
          'pytest.ini', 'tests/conftest.py', 'tools/verify_changes.py', 'tools/ci_plan.py',
          'tools/check_environment.py', 'tools/run_ui_checks.py', 'tools/check_processes.py',
          'tools/check_progress.py', 'tools/check_worker.py', 'tools/source_receipt.py',
          'tools/clear_check_scopes.ps1',
          '.github/workflows/windows.yml')


def environment_key(root):
    value = hashlib.sha256(POLICY.encode())
    # Exact runner image changes invalidate inherited coverage too. Resolve and
    # source run on Windows; a rollout between jobs is rejected, never ignored.
    for text in (platform.python_version(), platform.system(), platform.machine(),
                 os.environ.get('ImageOS', ''), os.environ.get('ImageVersion', '')):
        value.update(text.encode() + b'\0')
    for name in INPUTS:
        path = root / name
        value.update(name.encode() + b'\0')
        value.update(path.read_bytes().replace(b'\r\n', b'\n') if path.exists() else b'missing')
    return value.hexdigest()


def write_receipt(folder, plan, sha, run_id, root):
    key = environment_key(root)
    if key != plan['source_environment'] or sha != plan['commit']:
        raise ValueError('Source environment changed after planning; replan this run')
    receipt = dict(schema=1, policy=POLICY, commit=sha, run_id=str(run_id), environment=key,
                   base=plan['source_base'], scope=plan['tests'], args=plan['source_args'],
                   scales=plan['scales'], complete=True)
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(receipt, indent=2)
    if len(text.encode('utf-8')) > 32768:
        raise ValueError('Source receipt exceeds the 32 KiB metadata budget')
    (folder / 'source-verification.json').write_text(text, encoding='utf-8')
    return receipt


def valid(receipt, sha, run_id, root):
    return (isinstance(receipt, dict) and receipt.get('schema') == 1 and receipt.get('policy') == POLICY
            and receipt.get('commit') == sha and receipt.get('run_id') == str(run_id)
            and receipt.get('environment') == environment_key(root)
            and receipt.get('complete') is True and isinstance(receipt.get('scope'), list)
            and isinstance(receipt.get('args'), list))
