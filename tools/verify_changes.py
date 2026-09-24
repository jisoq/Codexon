"""Select and run CacheMonitor checks from the last verified file state."""

from __future__ import annotations

import argparse
import ast
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

def test_files(*names):
    return tuple(f'tests/test_{name}.py' for name in names)


# Components select observable contracts. There is deliberately no always-run core.
GROUPS = {
    'data': test_files('core', 'index', 'data_contract', 'subagent_collection', 'request_tier_snapshots'),
    'analysis': test_files('comparison', 'overview', 'performance', 'data_contract', 'output_speed'),
    'cost': test_files('pricing', 'mode_costs', 'session_costs', 'data_contract'),
    'cache_connection': test_files('cache_codex_connection'),
    'cache': test_files('cache_health', 'cache_misses', 'cache_management','cache_product','cache_operating'),
    'cache_ui': test_files('cache_ui'),
    'speed': test_files('speed_health', 'output_speed'),
    'names': test_files('codex_names'),
    'modes': test_files('request_modes', 'request_tier_snapshots', 'mode_costs'),
    'quota_store': test_files('quota', 'quota_tracking', 'quota_tracking_integration',
                             'quota_integrity', 'quota_accumulation', 'quota_value_history'),
    'quota_math': test_files('quota_attribution', 'quota_cycles', 'quota_resets', 'banked_resets'),
    'quota_poll': test_files('quota_polling', 'overlay_collection_resilience'),
    'quota_ui': test_files('quota_page', 'quota_detail_card', 'quota_gap_axis', 'quota_share_theme'),
    'quota_chart': test_files('quota_gap_axis', 'quota_chart_performance', 'quota_page'),
    'dashboard': test_files('ui', 'comparison_workflow', 'dashboard_evidence', 'ui_value_fixes'),
    'details': test_files('ui_details', 'call_transport', 'model_ui'),
    'table': test_files('table_order', 'performance'),
    'shared_ui': test_files('quick_ui', 'display_scaling'),
    'theme': test_files('theme_tokens', 'quota_share_theme'),
    'overlay_data': test_files('overlay_call_data', 'overlay_model_summary', 'session_costs', 'speed_health'),
    'overlay_tracking': test_files('overlay', 'overlay_resilience', 'overlay_collection_resilience'),
    'overlay_controls': test_files('overlay_controls', 'overlay_layout_controller'),
    'overlay_navigation': test_files('overlay_navigation', 'speed_health') + (
        'tests/test_output_speed.py::test_rendered_summary_click_and_overlay_speed_navigation',),
    'overlay_render': test_files('overlay_presentation', 'completed_layout', 'overlay_shadow'),
    'overlay_appearance': test_files('overlay_appearance', 'theme_tokens'),
    'overlay_windows': test_files('overlay_windows'),
    'taskbar': test_files('taskbar', 'taskbar_clock'),
    'window': test_files('window_recovery', 'display_scaling'),
    'notifications': test_files('notifications', 'confirmed_notifications'),
    'evidence': test_files('model_evidence', 'proxy_observation', 'observation_delivery', 'call_transport'),
    'relay': test_files('model_proxy', 'proxy_http2', 'proxy_observation', 'observation_delivery'),
    'proxy_lifecycle': test_files('managed_proxy', 'proxy_update', 'proxy_supervisor', 'connection_recovery'),
    'observer': test_files('observer_control', 'observer_panel'),
    'install': test_files('install_activation', 'install_management', 'windows_startup'),
    'update': test_files('app_update'),
    'runtime': test_files('windows_startup') + (
        'tests/test_verify_changes.py::test_isolated_source_smoke_renders_parent_cost',),
    'translation': ('tests/test_public_release.py::test_english_token_labels_do_not_change_stored_values',),
    'payload': ('tests/test_public_release.py::test_public_payload_rejects_local_paths_and_unneeded_qt',),
    'selector': test_files('verify_changes'),
}

# First match wins: QML and shared helpers must not fall through to a broad UI gate.
RULES = (
    ('tools/cache_runtime.py', ('cache',)),
    ('cachemonitor/cache_operating.py', ('cache','cache_ui','cache_connection')),
    ('cachemonitor/cache_panel.py', ('cache_ui','cache_connection')),
    ('cachemonitor/cache_hooks.py', ('cache','cache_ui','cache_connection')),
    ('cachemonitor/cache_control.py', ('cache','cache_ui','cache_connection')),
    ('cachemonitor/cache_integration.py', ('cache','data','cost','quota_store')),
    ('cachemonitor/cache_scheduler.py', ('cache','cache_connection','proxy_lifecycle')),
    ('cachemonitor/cache_execution.py', ('cache', 'cache_connection')),
    ('cachemonitor/cache_capture.py', ('cache', 'relay', 'cache_connection')),
    ('cachemonitor/session_costs.py', ('cost', 'overlay_data')),
    ('cachemonitor/core.py', ('data', 'analysis', 'cost', 'quota_store', 'modes', 'cache', 'speed')),
    ('cachemonitor/index.py', ('data', 'evidence', 'quota_store')),
    ('cachemonitor/analytics.py', ('analysis', 'cost', 'cache', 'speed', 'quota_math')),
    ('cachemonitor/analysis_engine.py', ('analysis', 'cost', 'cache', 'overlay_data')),
    ('cachemonitor/analysis_worker.py', ('analysis', 'quota_poll', 'overlay_tracking', 'notifications')),
    ('cachemonitor/analysis_delivery.py', ('analysis', 'overlay_tracking')),
    ('cachemonitor/pricing.py', ('cost', 'quota_store', 'quota_math')),
    ('cachemonitor/request_modes.py', ('modes', 'quota_store')),
    ('cachemonitor/cache_*.py', ('cache', 'dashboard')),
    ('cachemonitor/speed_health.py', ('speed',)),
    ('cachemonitor/codex_names.py', ('names', 'overlay_tracking')),
    ('cachemonitor/quota_chart.py', ('quota_chart',)),
    ('cachemonitor/quota_share.py', ('quota_ui', 'quota_math')),
    ('cachemonitor/quota_attribution.py', ('quota_math', 'quota_store')),
    ('cachemonitor/quota_cycles.py', ('quota_math', 'quota_store')),
    ('cachemonitor/banked_resets.py', ('quota_math', 'quota_ui')),
    ('cachemonitor/quota_view.py', ('quota_ui',)),
    ('cachemonitor/quota_panel.py', ('quota_ui',)),
    ('cachemonitor/quota_service.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_polling.py', ('quota_poll',)),
    ('cachemonitor/quota_live.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_diagnostics.py', ('quota_math',)),
    ('cachemonitor/quota_reader.py', ('quota_store', 'quota_math')),
    ('cachemonitor/quota_tracking*.py', ('quota_store', 'quota_math')),
    ('cachemonitor/quota.py', ('quota_store', 'quota_math')),
    ('cachemonitor/dashboard.py', ('dashboard', 'details', 'cost', 'speed')),
    ('cachemonitor/ui_details.py', ('details',)),
    ('cachemonitor/table_model.py', ('table',)),
    ('cachemonitor/lazy_table.py', ('table',)),
    ('cachemonitor/overlay_data.py', ('overlay_data', 'speed', 'evidence')),
    ('cachemonitor/overlay_navigation.py', ('overlay_navigation',)),
    ('cachemonitor/overlay_chrome.py', ('overlay_controls', 'overlay_navigation')),
    ('cachemonitor/overlay_view.py', ('overlay_render', 'overlay_navigation')),
    ('cachemonitor/overlay_appearance.py', ('overlay_appearance',)),
    ('cachemonitor/overlay_windows.py', ('overlay_windows', 'overlay_controls')),
    ('cachemonitor/overlay_shadow.py', ('overlay_render',)),
    ('cachemonitor/overlay_tracking.py', ('overlay_tracking',)),
    ('cachemonitor/overlay.py', ('overlay_tracking', 'overlay_controls', 'overlay_navigation')),
    ('cachemonitor/qml/OverlayLinks.qml', ('overlay_navigation',)),
    ('cachemonitor/qml/OverlayDetail.qml', ('overlay_render', 'overlay_navigation')),
    ('cachemonitor/qml/OverlayControls.qml', ('overlay_controls',)),
    ('cachemonitor/qml/OverlayScene.qml', ('overlay_render',)),
    ('cachemonitor/qml/QuotaDetail.qml', ('quota_ui',)),
    ('cachemonitor/qml/QuotaDetails.qml', ('quota_ui',)),
    ('cachemonitor/qml/Taskbar.qml', ('taskbar',)),
    ('cachemonitor/qml/DataTable.qml', ('table', 'dashboard')),
    ('cachemonitor/qml/DateField.qml', ('shared_ui', 'dashboard')),
    ('cachemonitor/qml/Ui*.qml', ('shared_ui',)),
    ('cachemonitor/qml/Node*.qml', ('shared_ui',)),
    ('cachemonitor/qml/Main.qml', ('shared_ui', 'window')),
    ('cachemonitor/qml/PaintedScene.qml', ('shared_ui', 'overlay_render', 'quota_ui')),
    ('cachemonitor/quick_runtime.py', ('shared_ui', 'overlay_controls', 'quota_ui')),
    ('cachemonitor/controls.py', ('shared_ui',)),
    ('cachemonitor/presentation.py', ('shared_ui', 'dashboard', 'overlay_render')),
    ('cachemonitor/charts.py', ('shared_ui', 'quota_chart')),
    ('cachemonitor/theme.py', ('theme',)),
    ('cachemonitor/token_colors.py', ('theme',)),
    ('cachemonitor/settings_page.py', ('dashboard', 'theme', 'observer')),
    ('cachemonitor/taskbar*.py', ('taskbar',)),
    ('cachemonitor/screens.py', ('window', 'overlay_controls')),
    ('cachemonitor/notifications.py', ('notifications',)),
    ('cachemonitor/change_highlight.py', ('shared_ui', 'overlay_render')),
    ('cachemonitor/fonts.py', ('shared_ui', 'overlay_render')),
    ('cachemonitor/icons.py', ('shared_ui',)),
    ('cachemonitor/brand_icon.py', ('runtime',)),
    ('icons/*.ico', ('runtime', 'payload')),
    ('cachemonitor/assets/brand/*', ('runtime',)),
    ('cachemonitor/assets/fonts/*', ('shared_ui', 'overlay_render')),
    ('cachemonitor/i18n.py', ('translation', 'shared_ui')),
    ('cachemonitor/translation_catalog.py', ('translation',)),
    ('cachemonitor/assets/i18n/*.json', ('translation',)),
    ('cachemonitor/model_evidence.py', ('evidence', 'data')),
    ('cachemonitor/evidence_writer.py', ('evidence',)),
    ('cachemonitor/model_proxy.py', ('relay', 'cache_connection')),
    ('cachemonitor/proxy_http.py', ('relay',)),
    ('cachemonitor/proxy_observation.py', ('relay', 'evidence')),
    ('cachemonitor/proxy_update.py', ('proxy_lifecycle',)),
    ('cachemonitor/proxy_supervisor.py', ('proxy_lifecycle',)),
    ('cachemonitor/managed_proxy.py', ('proxy_lifecycle',)),
    ('cachemonitor/connection_recovery.py', ('proxy_lifecycle', 'install')),
    ('cachemonitor/observer_panel.py', ('observer',)),
    ('cachemonitor/observer_control.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/observer_state.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/observer_task.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/install*.py', ('install',)),
    ('cachemonitor/app_update.py', ('update', 'install')),
    ('cachemonitor/update_panel.py', ('update',)),
    ('cachemonitor/app.py', ('runtime', 'install', 'observer')),
    ('cachemonitor/version.py', ('runtime', 'proxy_lifecycle', 'update')),
    ('cachemonitor/__init__.py', ('runtime',)),
    ('cachemonitor/tray.py', ('runtime', 'window')),
    ('cachemonitor/launch_context.py', ('install', 'runtime')),
    ('cachemonitor/app_restart.py', ('runtime', 'install', 'payload')),
    ('cachemonitor/windows_integration.py', ('install', 'taskbar')),
    ('cachemonitor/shell_shortcut.py', ('install',)),
    ('cachemonitor/runtime_check.py', ('runtime',)),
    ('cachemonitor/quick_smoke.py', ('runtime',)),
    ('cachemonitor/quick_qa.py', ('runtime', 'shared_ui', 'overlay_controls')),
    ('cachemonitor/qa.py', ('runtime',)),
    ('tools/verify_changes.py', ('selector',)),
    ('.github/workflows/windows.yml', ('selector',)),
    ('tools/run_ui_checks.py', ('runtime', 'overlay_controls')),
    ('tools/demo_speed_overlay.py', ('speed',)),
    ('tools/*proxy*.py', ('relay', 'proxy_lifecycle')),
    ('tools/*overlay*.py', ('overlay_controls', 'overlay_render')),
    ('tools/*quota*.py', ('quota_store', 'quota_math')),
    ('tools/audit_analytics.py', ('analysis',)),
    ('tools/verify_record_pairs.py', ('data',)),
    ('tools/benchmark_interactions.py', ('table', 'quota_chart')),
    ('tools/verify_settings.py', ('dashboard',)),
    ('tools/verify_design.py', ('shared_ui',)),
    ('tools/*display_scaling.py', ('window',)),
    ('tools/font_render_probe.py', ('shared_ui',)),
    ('tools/verify_recovery.py', ('install',)),
    ('tools/verify_installation.py', ('install',)),
    ('tools/verify_gui_handoff.py', ('install',)),
    ('tools/installer_identity.py', ('install',)),
    ('tools/prepare_bad_runtime.py', ('install',)),
    ('tools/collect_notices.py', ('payload',)),
    ('tools/package_release.py', ('payload',)),
    ('tools/prepare_sources.py', ('payload',)),
    ('tools/Build-*.ps1', ('install', 'runtime', 'payload')),
    ('tools/configure-app-task.ps1', ('install',)),
    ('recovery_main.py', ('install',)),
    ('run.py', ('runtime',)),
    ('start.ps1', ('runtime',)),
    ('installer/*', ('install', 'payload')),
    ('build*.ps1', ('runtime', 'install', 'payload')),
    ('*.spec', ('runtime', 'payload')),
)

# These changes need frozen executable/install checks in addition to source tests.
PACKAGE_PATTERNS = (
    'requirements*', 'build*.ps1', '*.spec', 'installer/*', 'recovery_main.py', 'icons/*.ico',
    'run.py', 'start.ps1', 'tools/Build-*.ps1', 'tools/*install*.py', 'tools/prepare_bad_runtime.py',
    'tools/package_release.py', 'tools/collect_notices.py', 'tools/prepare_sources.py',
    'tools/verify_recovery.py', 'tools/verify_gui_handoff.py', 'tools/configure-app-task.ps1',
    'cachemonitor/install*.py', 'cachemonitor/app.py', 'cachemonitor/app_update.py', 'cachemonitor/app_restart.py',
    'cachemonitor/version.py', 'cachemonitor/runtime_check.py', 'cachemonitor/launch_context.py',
    'cachemonitor/windows_integration.py', 'cachemonitor/shell_shortcut.py',
)
PROXY_PATTERNS = (
    'cachemonitor/model_proxy.py', 'cachemonitor/proxy*.py', 'cachemonitor/managed_proxy.py',
    'cachemonitor/observer_control.py', 'cachemonitor/observer_state.py', 'cachemonitor/observer_task.py',
    'cachemonitor/connection_recovery.py', 'cachemonitor/evidence_writer.py',
    'cachemonitor/version.py', 'tools/*proxy*.py', 'requirements*', 'build*.ps1', '*.spec',
    'tools/Build-*.ps1', 'cachemonitor/install*.py', 'installer/*',
)
FULL_PATTERNS = ('tests/conftest.py', 'pytest.ini', 'pyproject.toml', 'requirements*')
DOC_PATTERNS = ('docs/*', 'releases/*', '*.md', '*.txt', 'LICENSE*', '.gitignore',
                '.gitattributes', '.github/ISSUE_TEMPLATE/*')


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


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


def test_dependents(changed, root):
    """Include consumers of shared test fixtures, including imports inside functions."""
    dependencies = {}
    for path in (root / 'tests').rglob('*.py'):
        imports = set()
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.add(node.module or '')
                if node.module == 'tests':
                    imports.update('tests.' + alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        dependencies[path.relative_to(root).as_posix()] = {
            (name.replace('.', '/') + '.py' if name.startswith('tests.') else
             'tests/' + name.replace('.', '/') + '.py') for name in imports}
    affected = set(changed)
    while True:
        consumers = {path for path, imports in dependencies.items() if imports & affected}
        if consumers <= affected:
            break
        affected.update(consumers)
    return {path for path in affected if Path(path).name.startswith('test_')
            and (root / path).is_file()}


def select_tests(changed, *, root=None):
    """Map changes to contracts; unmapped code is an actionable selection error."""
    root = root or ROOT
    first_run = changed is None
    paths = set(changed or ())
    full = first_run or any(matches(path, FULL_PATTERNS) for path in paths)
    proxy = first_run or any(matches(path, PROXY_PATTERNS) for path in paths)
    package = proxy or first_run or any(matches(path, PACKAGE_PATTERNS) for path in paths)
    selected, groups, unmapped = set(), set(), set()
    reasons = {}

    def add(test, reason):
        selected.add(test)
        reasons.setdefault(test, []).append(reason)

    test_changes = {path for path in paths if path.startswith('tests/') and path.endswith('.py')}
    if test_changes:
        for test in test_dependents(test_changes, root):
            add(test, '검사 또는 공유 fixture 변경')
    for path in sorted(paths):
        if path in test_changes or matches(path, FULL_PATTERNS):
            continue
        matched = next((scopes for pattern, scopes in RULES if fnmatch.fnmatchcase(path, pattern)), None)
        if matched is not None:
            groups.update(matched)
            for group in matched:
                for test in GROUPS[group]:
                    add(test, path)
        elif not matches(path, DOC_PATTERNS):
            unmapped.add(path)
    # A file selection subsumes any specific node selections from other changes.
    selected = {test for test in selected if '::' not in test or test.split('::')[0] not in selected}
    if full:
        selected = {'tests'}
        reasons = {'tests': ['검증 기준점 없음' if first_run else '공통 검사 설정 또는 의존성 변경']}
        groups.add('full')
    return dict(full=full, tests=tuple(sorted(selected)),
                reasons={test: reasons[test] for test in sorted(selected)},
                groups=tuple(sorted(groups)), unmapped=tuple(sorted(unmapped)),
                package_impact=package, proxy_impact=proxy)


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
    parser.add_argument('--package-only', action='store_true', help='Check only the executable after source checks')
    args = parser.parse_args(argv)
    if args.package_only and (not args.package_exe or args.full):
        parser.error('--package-only requires --package-exe and cannot be combined with --full')
    current = file_hashes()
    baseline = json.loads(BASELINE.read_text(encoding='utf-8')) if BASELINE.is_file() else None
    changed = changed_files(current, baseline, args.base)
    plan = select_tests(changed)
    if args.full:
        plan = {**plan, 'full':True, 'tests':('tests',), 'groups':('full',),
                'reasons':{'tests':['사용자가 전체 검사를 지정함']}, 'unmapped':()}
    if args.package_only:
        plan = {**plan, 'full':False, 'tests':(), 'groups':(), 'reasons':{}, 'unmapped':()}
    public = dict(changed=sorted(changed) if changed is not None else None,
                  full=plan['full'], unmapped=plan['unmapped'],
                  groups=plan['groups'], tests=plan['tests'], reasons=plan['reasons'],
                  package_impact=plan['package_impact'], proxy_impact=plan['proxy_impact'],
                  package_exe=str(args.package_exe) if args.package_exe else None)
    if args.plan:
        print(json.dumps(public,ensure_ascii=False,indent=2))
        return 2 if plan['unmapped'] else 0
    if plan['unmapped']:
        print(json.dumps(dict(result='unmapped', paths=plan['unmapped'],
                             action='tools/verify_changes.py의 RULES에 검사 연결을 추가하거나 --full을 지정하세요.'), ensure_ascii=False))
        return 2
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
    if not args.package_only:
        BASELINE.write_text(json.dumps(dict(version=1,files=current,report=str(folder/'report.json')),
                                       ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(result='passed',changed=len(changed) if changed is not None else None,
                          groups=plan['groups'],pytest=report.get('pytest'),package=report['package'],
                          report=str(folder/'report.json')),ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
