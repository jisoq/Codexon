"""Select and run CacheMonitor checks from the last verified file state."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORTS = ROOT / 'artifacts' / 'verification'
BASELINE = REPORTS / 'last-success.json'

CORE = (
    'tests/test_data_contract.py::test_same_home_response_id_dedup_and_no_parent_cost_inheritance',
    'tests/test_data_contract.py::test_partial_sums_weighted_cache_and_whole_lookup_stay_shared',
    'tests/test_index.py::test_incremental_partial_line_and_truncation',
    'tests/test_index.py::test_index_cannot_write_to_source_or_unrelated_database',
    'tests/test_observation_delivery.py::test_writer_lock_failure_does_not_retry_request_and_recovers',
    'tests/test_observer_control.py::test_stale_enabled_flag_and_background_poll_never_reroute',
    'tests/test_proxy_update.py::test_failed_new_worker_rolls_back',
    'tests/test_session_costs.py::test_nested_costs_empty_parent_partial_prices_and_home_boundary',
)

GROUPS = {
    'data': ('tests/test_core.py', 'tests/test_index.py', 'tests/test_data_contract.py',
             'tests/test_observation_delivery.py'),
    'cost': ('tests/test_session_costs.py', 'tests/test_subagent_collection.py',
             'tests/test_data_contract.py', 'tests/test_overlay_navigation.py'),
    'pricing_quota': ('tests/test_pricing.py', 'tests/test_mode_costs.py',
                      'tests/test_quota_tracking_integration.py', 'tests/test_quota_integrity.py',
                      'tests/test_quota_page.py', 'tests/test_quota_resets.py'),
    'quota_service': ('tests/test_quota_polling.py', 'tests/test_quota_tracking.py',
                      'tests/test_overlay_collection_resilience.py'),
    'ui': ('tests/test_ui.py', 'tests/test_quick_ui.py'),
    'overlay': ('tests/test_overlay.py', 'tests/test_overlay_navigation.py',
                'tests/test_overlay_resilience.py', 'tests/test_overlay_windows.py'),
    'proxy': ('tests/test_proxy_update.py', 'tests/test_observer_panel.py',
              'tests/test_observer_control.py', 'tests/test_proxy_supervisor.py',
              'tests/test_model_proxy.py'),
    'package': ('tests/test_windows_startup.py', 'tests/test_quick_ui.py',
                'tests/test_observer_panel.py',
                'tests/test_verify_changes.py::test_isolated_source_smoke_renders_parent_cost'),
}

RULES = (
    ('cachemonitor/session_costs.py', 'cost'),
    ('cachemonitor/core.py', 'data'),
    ('cachemonitor/index.py', 'data'),
    ('cachemonitor/analytics.py', 'data'),
    ('cachemonitor/analysis_engine.py', 'data'),
    ('cachemonitor/analysis_worker.py', 'data'),
    ('cachemonitor/analysis_worker.py', 'quota_service'),
    ('cachemonitor/analysis_delivery.py', 'data'),
    ('cachemonitor/pricing.py', 'pricing_quota'),
    ('cachemonitor/request_modes.py', 'pricing_quota'),
    ('cachemonitor/quota*.py', 'pricing_quota'),
    ('cachemonitor/quota_service.py', 'quota_service'),
    ('cachemonitor/quota_polling.py', 'quota_service'),
    ('cachemonitor/dashboard.py', 'ui'),
    ('cachemonitor/dashboard.py', 'cost'),
    ('cachemonitor/overlay_data.py', 'cost'),
    ('cachemonitor/overlay*.py', 'overlay'),
    ('cachemonitor/qml/*', 'ui'),
    ('cachemonitor/theme.py', 'ui'),
    ('cachemonitor/token_colors.py', 'ui'),
    ('cachemonitor/token_colors.py', 'overlay'),
    ('cachemonitor/quick_smoke.py', 'package'),
    ('cachemonitor/app.py', 'package'),
    ('cachemonitor/version.py', 'proxy'),
    ('cachemonitor/version.py', 'package'),
    ('cachemonitor/model_proxy.py', 'proxy'),
    ('cachemonitor/proxy*.py', 'proxy'),
    ('cachemonitor/observer*.py', 'proxy'),
    ('build*.ps1', 'package'),
    ('Codexon.spec', 'package'),
    ('requirements*.txt', 'package'),
    ('tools/verify_proxy_update.py', 'proxy'),
    ('tools/verify_changes.py', 'selector'),
)


def git_bytes(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=True)
    return result.stdout


def file_hashes():
    paths = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
             for value in git_bytes('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
             if value}
    result = {}
    for path in sorted(paths):
        target = ROOT / path
        if target.is_file() and not target.is_symlink():
            result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    return result


def changed_files(current, baseline=None, base_ref=None):
    if base_ref:
        changed = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
                   for value in git_bytes('diff', '--name-only', '-z', base_ref, '--').split(b'\0') if value}
        untracked = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
                     for value in git_bytes('ls-files', '-z', '--others', '--exclude-standard').split(b'\0') if value}
        return changed | untracked
    if baseline is None:
        return None
    before = baseline.get('files', {})
    return {path for path in before.keys() | current.keys() if before.get(path) != current.get(path)}


def select_tests(changed):
    """Return exact checks and why they were selected; unknown code widens scope."""
    if changed is None:
        return {'full': True, 'tests': ('tests',), 'reasons': {'tests':'검증 기준점 없음'},
                'groups': ('full',), 'package_impact': True, 'proxy_impact': True}
    selected = set()
    reasons = {}
    groups = set()
    full = False
    runtime_change = False
    for path in sorted(changed):
        if path in ('tests/conftest.py', 'pytest.ini'):
            full = True
            continue
        if path.startswith('tests/'):
            if path.endswith('.py') and Path(path).name.startswith('test_'):
                selected.add(path);reasons.setdefault(path, []).append(f'검사 변경: {path}')
                runtime_change = True
            else:
                full = True
            continue
        matched = {group for pattern,group in RULES if fnmatch.fnmatchcase(path,pattern)}
        if path.startswith(('docs/', 'releases/')) or path in ('AGENTS.md','README.md','README-Lite.md','.gitignore'):
            continue
        if matched:
            groups.update(matched)
            runtime_change = True
            continue
        if path.startswith(('cachemonitor/', 'tools/')) or path.endswith(('.py','.qml','.ps1','.spec','.txt')):
            full = True
    if full:
        return {'full': True, 'tests': ('tests',), 'reasons': {'tests':'공통 설정 또는 미분류 코드 변경'},
                'groups': ('full',), 'package_impact': True, 'proxy_impact': True}
    if runtime_change:
        for test in CORE:
            selected.add(test)
            reasons.setdefault(test, []).append('항상 확인하는 핵심 경로')
    if 'selector' in groups:
        selected.add('tests/test_verify_changes.py')
        reasons.setdefault('tests/test_verify_changes.py', []).append('선택 도구 변경')
    for group in groups - {'selector'}:
        for test in GROUPS[group]:
            selected.add(test)
            reasons.setdefault(test, []).append(f'{group} 영역 변경')
    return {'full': False, 'tests': tuple(sorted(selected)), 'reasons': reasons,
            'groups': tuple(sorted(groups)),
            'package_impact': 'package' in groups,
            'proxy_impact': 'proxy' in groups}


def fixture_home(folder):
    home = folder / 'codex';home.mkdir()
    when = datetime.now(timezone.utc) - timedelta(seconds=30)
    parent = '11111111-1111-1111-1111-111111111111'
    child = '22222222-2222-2222-2222-222222222222'
    with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
        db.execute('create table threads(id text, rollout_path text, updated_at integer, name text, '
                   'title text, model text, model_provider text, cwd text)')
        for tid,title,parent_id in ((parent,'배포 검증 작업',None),(child,'하위 검증 작업',parent)):
            path = home / f'{tid}.jsonl'
            def event(kind, seconds, payload):
                return dict(type=kind,timestamp=(when+timedelta(seconds=seconds)).isoformat(),payload=payload)
            header = dict(id=tid,cwd='C:/cachemonitor-qa',source=(
                {'subagent':{'thread_spawn':{'parent_thread_id':parent_id,'depth':1,'agent_path':'/root/check'}}}
                if parent_id else 'cli'))
            if parent_id:header['parent_thread_id']=parent_id
            records = [event('session_meta',0,header),
                       event('turn_context',1,dict(turn_id='verify',model='gpt-6-astra',effort='high',service_tier='Standard')),
                       event('event_msg',2,dict(type='task_started',turn_id='verify')),
                       event('token_usage_record',3,dict(thread_id=tid,turn_id='verify',response_id=tid+'-response',
                           usage=dict(input_tokens=1000,cached_input_tokens=200,cache_write_input_tokens=0,
                                      output_tokens=100))),
                       event('event_msg',4,dict(type='task_complete',turn_id='verify'))]
            path.write_text(''.join(json.dumps(row)+'\n' for row in records),encoding='utf-8')
            db.execute('insert into threads values(?,?,?,?,?,?,?,?)',
                       (tid,str(path),int(when.timestamp())+4,title,title,'gpt-6-astra','openai','C:/cachemonitor-qa'))
    with closing(sqlite3.connect(home / 'logs_2.sqlite')) as db, db:
        db.execute('create table logs(id integer primary key, ts integer, ts_nanos integer, '
                   'target text, thread_id text, process_uuid text, feedback_log_body text)')
    return home


def package_check(executable, report_dir):
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    runtime = report_dir / 'runtime.json'
    check = subprocess.run([str(executable), '--verify-runtime', str(runtime)],
                           capture_output=True, text=True, timeout=60)
    value = json.loads(runtime.read_text(encoding='utf-8')) if runtime.exists() else {}
    from cachemonitor.version import VERSION, PROXY_VERSION
    if (check.returncode or value.get('errors') != [] or value.get('version') != VERSION
            or value.get('proxy_version') != PROXY_VERSION):
        raise RuntimeError('배포 실행 파일 런타임 확인 실패: '+check.stderr[-1000:])
    with tempfile.TemporaryDirectory(prefix='cachemonitor-package-qa-') as temp:
        folder = Path(temp);home = fixture_home(folder)
        image = report_dir / 'smoke.png'
        check = subprocess.run([str(executable), '--codex-home', str(home),
                                '--index-path', str(folder/'usage-index.sqlite'),
                                '--smoke', str(image), '--smoke-depth', 'core'],
                               capture_output=True, text=True, timeout=180)
        report = json.loads(image.with_suffix('.json').read_text(encoding='utf-8')) if image.with_suffix('.json').exists() else {}
        rollup = report.get('session_rollup') or {}
        if check.returncode or report.get('errors') != [] or rollup.get('descendants',0) < 1:
            raise RuntimeError('격리 패키지 화면 검사 실패: '+str(report.get('errors') or check.stderr[-1000:]))
    return dict(runtime=runtime.name, smoke=image.with_suffix('.json').name,
                duration_seconds=report.get('duration_seconds'), session_rollup=rollup)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Show checks without running them')
    parser.add_argument('--base', help='Git ref to compare when intentionally establishing a new baseline')
    parser.add_argument('--full', action='store_true', help='Run the whole test suite')
    parser.add_argument('--package-exe', type=Path, help='Check a built executable with isolated records')
    args = parser.parse_args(argv)
    current = file_hashes()
    baseline = json.loads(BASELINE.read_text(encoding='utf-8')) if BASELINE.is_file() else None
    changed = changed_files(current, baseline, args.base)
    plan = select_tests(changed)
    if args.full:
        plan = {**plan, 'full':True, 'tests':('tests',), 'groups':('full',),
                'reasons':{'tests':'사용자가 전체 검사를 지정함'}}
    public = dict(changed=sorted(changed) if changed is not None else None,
                  groups=plan['groups'], tests=plan['tests'], reasons=plan['reasons'],
                  package_impact=plan['package_impact'], proxy_impact=plan['proxy_impact'],
                  package_exe=str(args.package_exe) if args.package_exe else None)
    if args.plan:
        print(json.dumps(public,ensure_ascii=False,indent=2))
        return 0
    REPORTS.mkdir(parents=True,exist_ok=True)
    folder = REPORTS / datetime.now().strftime('%Y%m%d-%H%M%S')
    folder.mkdir(exist_ok=False)
    report = dict(public,logs=[],package=None)
    if plan['tests']:
        started = time.monotonic()
        command = [sys.executable, '-B', '-m', 'pytest', '-q', '--tb=short', '--durations=10', *plan['tests']]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        log = folder / 'pytest.log'
        log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        report['logs'].append(str(log))
        report['pytest'] = dict(exit_code=result.returncode, seconds=round(time.monotonic()-started,2),
                                summary=result.stdout.strip().splitlines()[-1] if result.stdout.strip() else '')
        if result.returncode:
            (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(dict(result='failed',**report['pytest'],log=str(log)),ensure_ascii=False))
            return result.returncode
    if args.package_exe:
        try:
            report['package'] = package_check(args.package_exe,folder)
        except Exception as error:
            report['package_error'] = str(error)
            (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(dict(result='package_failed',error=str(error),report=str(folder/'report.json')),ensure_ascii=False))
            return 1
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    BASELINE.write_text(json.dumps(dict(version=1,files=current,report=str(folder/'report.json')),
                                   ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(result='passed',changed=len(changed) if changed is not None else None,
                          groups=plan['groups'],pytest=report.get('pytest'),package=report['package'],
                          report=str(folder/'report.json')),ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
