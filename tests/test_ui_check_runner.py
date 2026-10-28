"""The UI runner must actually isolate windows and child processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('exit_code', [0, 37])
def test_runner_isolates_qt_and_descendants_and_preserves_process_io(tmp_path, exit_code):
    probe = tmp_path / '한글 경로 window probe.py'
    probe.write_text(textwrap.dedent('''
        import ctypes
        from ctypes import wintypes as W
        import json, os, subprocess, sys

        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        user.GetProcessWindowStation.restype = W.HANDLE
        user.GetThreadDesktop.argtypes = [W.DWORD]
        user.GetThreadDesktop.restype = W.HANDLE
        user.GetUserObjectInformationW.argtypes = [
            W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
        kernel.GetCurrentThreadId.restype = W.DWORD
        def name(handle):
            result = ctypes.create_unicode_buffer(256)
            needed = W.DWORD()
            assert user.GetUserObjectInformationW(handle, 2, result, ctypes.sizeof(result),
                                                 ctypes.byref(needed))
            return result.value
        station = name(user.GetProcessWindowStation())
        desktop = name(user.GetThreadDesktop(kernel.GetCurrentThreadId()))
        assert station.lower() == 'winsta0', station
        assert desktop.startswith('CodexonQA-'), desktop
        if sys.argv[1] == '--leaf':
            print(json.dumps(dict(station=station, desktop=desktop)))
            sys.exit(0)

        from PySide6.QtWidgets import QApplication, QLineEdit
        app = QApplication([])
        editor = QLineEdit(sys.argv[1])
        editor.show()
        app.processEvents()
        user.IsWindowVisible.argtypes = [W.HWND]
        user.IsWindowVisible.restype = W.BOOL
        assert user.IsWindowVisible(int(editor.winId()))
        child = subprocess.run([sys.executable, __file__, '--leaf'],
                               capture_output=True, text=True, check=True)
        print(json.dumps(dict(station=station, desktop=desktop, child=json.loads(child.stdout),
                              stdin=sys.stdin.read(),
                              scale=os.environ.get('QT_SCALE_FACTOR'),
                              env=os.environ['CODEXON_RUNNER_TEST'], cwd=os.getcwd())))
        print('runner stderr preserved', file=sys.stderr)
        sys.exit(int(sys.argv[2]))
    '''), encoding='utf-8')
    payload = '한글 인수 "quoted" \\path with spaces\\'
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               CODEXON_RUNNER_TEST='환경 전달')
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tools/run_ui_checks.py'), '--scale', '1.25',
         '--diagnostics', str(tmp_path / 'evidence'), '--',
         sys.executable, str(probe), payload, str(exit_code)],
        input='stdin 전달\n', capture_output=True, text=True, encoding='utf-8',
        cwd=tmp_path, env=env, timeout=30)
    assert result.returncode == exit_code, result.stderr
    observed = json.loads(result.stdout)
    assert observed['station'].lower() == 'winsta0'
    assert observed['desktop'].startswith('CodexonQA-')
    assert observed['child'] == {key: observed[key] for key in ('station', 'desktop')}
    assert observed['stdin'] == 'stdin 전달\n'
    assert observed['scale'] == '1.25'
    assert observed['env'] == '환경 전달'
    assert Path(observed['cwd']) == tmp_path
    assert 'runner stderr preserved' in result.stderr


def test_runner_allows_finite_cleanup_after_parent_exits(tmp_path):
    probe = tmp_path / 'cleanup_probe.py'
    completed = tmp_path / 'cleanup-completed'
    probe.write_text(textwrap.dedent('''
        import os, subprocess, sys
        child = subprocess.Popen([sys.executable, '-c',
            'import sys, time; from pathlib import Path; '
            'sys.stdin.buffer.read(); time.sleep(1); Path(sys.argv[1]).touch()',
            sys.argv[1]], stdin=subprocess.PIPE)
        os._exit(0)
    '''), encoding='utf-8')
    diagnostics = tmp_path / 'evidence'
    result = subprocess.run([sys.executable, str(ROOT / 'tools/run_ui_checks.py'),
        '--timeout', '5', '--diagnostics', str(diagnostics), '--',
        sys.executable, str(probe), str(completed)],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert completed.exists()
    evidence = json.loads((diagnostics / 'result.json').read_text())
    assert evidence['reason'] == 'completed'
    assert evidence['process_exit_code'] == 0
    assert evidence['live_pids'] == evidence['remaining_pids'] == []


@pytest.mark.parametrize('mode,expected', [('normal', 0), ('hang', 124), ('orphan', 1)])
def test_deadline_records_cause_and_cleans_only_owned_tree(tmp_path, mode, expected):
    """Exercise the real Windows Job Object, including an inherited stdout handle."""
    import ctypes
    from ctypes import wintypes as W
    probe = tmp_path / 'probe.py'
    probe.write_text(textwrap.dedent('''
        import os, subprocess, sys, time
        from pathlib import Path
        mode, output = sys.argv[1:]
        if mode == 'normal':
            from tools.check_processes import register_root
            # The supervisor also mentions this scope in --diagnostics. It
            # must never be classified as a leftover test process.
            register_root(Path(output).parent)
            sys.exit(0)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        Path(output).write_text(str(child.pid))
        if mode == 'hang':
            time.sleep(120)
    '''), encoding='utf-8')
    diagnostics = tmp_path / 'evidence'
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'tools/run_ui_checks.py'),
            '--timeout', '3', '--diagnostics', str(diagnostics), '--',
            sys.executable, str(probe), mode, str(tmp_path / 'child.pid')],
            capture_output=True, text=True, timeout=20)
        assert result.returncode == expected, result.stderr
        evidence = json.loads((diagnostics / 'result.json').read_text())
        assert evidence['reason'] == dict(normal='completed', hang='deadline', orphan='descendants_remain')[mode]
        assert evidence['remaining_pids'] == []
        if mode == 'normal':
            assert evidence['process_exit_code'] == 0
            assert evidence['external']['processes'] == []
        assert evidence['seconds'] < 10
        assert unrelated.poll() is None
        if mode != 'normal':
            pid = int((tmp_path / 'child.pid').read_text())
            assert pid in evidence['live_pids']
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.OpenProcess.argtypes, api.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
            api.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
            api.CloseHandle.argtypes = [W.HANDLE]
            handle = api.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    assert api.WaitForSingleObject(handle, 0) == 0
                finally:
                    api.CloseHandle(handle)
        if mode == 'hang':
            assert 'probe.py' in ''.join(p.read_text() for p in diagnostics.glob('stacks-*.log'))
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_pytest_phase_is_visible_before_a_hung_test_exits(tmp_path):
    probe = tmp_path / 'test_stall.py'
    probe.write_text('import time\ndef test_wait():\n    time.sleep(120)\n')
    evidence = tmp_path / 'evidence'
    log = tmp_path / 'pytest.log'
    with log.open('w') as output:
        result = subprocess.run([sys.executable, str(ROOT / 'tools/run_ui_checks.py'),
            '--timeout', '4', '--diagnostics', str(evidence), '--', sys.executable,
            '-m', 'pytest', '-p', 'tools.check_progress', '-o', 'addopts=',
            '--rootdir', str(tmp_path), '--confcutdir', str(tmp_path), str(probe)],
            stdout=output, stderr=subprocess.STDOUT, timeout=20)
    assert result.returncode == 124, log.read_text()
    active = json.loads((evidence / 'result.json').read_text())['active_test']
    assert active['phase'] == 'call' and active['test'].endswith('test_wait')
    assert active['started'] in log.read_text()
    assert 'setup' in log.read_text() and 'call' in log.read_text()


@pytest.mark.parametrize('mode,expected', [('orphan', 1), ('hang', 124), ('diagnostic', 1)])
def test_scheduler_process_is_tracked_by_its_fresh_scope(tmp_path, mode, expected):
    from cachemonitor.observer_task import ObserverTask
    from tools.check_processes import clear_scopes
    ready = tmp_path / 'ready'
    scope_file = tmp_path / 'scope.txt'
    leaf_source = ('import time, subprocess, sys\nfrom pathlib import Path\n'
                   "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                   f'Path({str(ready)!r}).write_text(str(child.pid))\ntime.sleep(120)\n')
    probe = tmp_path / 'scheduler_probe.py'
    probe.write_text(textwrap.dedent(f'''
        import os, sys, time
        from pathlib import Path
        from cachemonitor.observer_task import ObserverTask
        def test_scheduled_process(tmp_path):
            scope = tmp_path / 'external'; scope.mkdir()
            Path({str(scope_file)!r}).write_text(str(scope))
            leaf = scope / 'leaf.py'; leaf.write_text({leaf_source!r})
            task = ObserverTask(str(scope), role='RunnerQA')
            task.start([sys.executable, str(leaf)])
            deadline = time.monotonic() + 10
            while not Path({str(ready)!r}).exists():
                if time.monotonic() >= deadline: raise RuntimeError('Scheduled probe did not start')
                time.sleep(.05)
            if {mode!r} == 'diagnostic':
                evidence = Path(os.environ['CODEXON_CHECK_DIAGNOSTICS'])
                (evidence/'processes.json').mkdir()
                (evidence/'external-processes.json').mkdir()
            if {mode!r} == 'hang': time.sleep(120)
    '''))
    evidence = tmp_path / 'evidence'
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'tools/run_ui_checks.py'),
            '--timeout', '20', '--diagnostics', str(evidence), '--', sys.executable,
            '-m', 'pytest', '-p', 'tools.check_progress', '-o', 'addopts=',
            '--rootdir', str(tmp_path), '--confcutdir', str(tmp_path), str(probe)],
            capture_output=True, text=True, timeout=40)
        assert result.returncode == expected, result.stdout + result.stderr
        task = ObserverTask(scope_file.read_text(), role='RunnerQA')
        report = json.loads((evidence / 'result.json').read_text())
        assert report['reason'] == ('deadline' if mode == 'hang' else 'external_descendants_remain')
        assert report['external']['processes']
        assert int(ready.read_text()) in {process['pid'] for process in report['external']['processes']}
        assert task.name in {task['name'] for task in report['external']['tasks']}
        assert not task.inspect()['registered']
        if mode == 'diagnostic':
            assert report['diagnostics']['first_error']['path'].endswith('processes.json')
    finally:
        if scope_file.exists():
            # Keep a failing regression run safe even if automatic registration
            # breaks. This fallback runs only after inspecting runner evidence.
            fallback = tmp_path/'fallback'; fallback.mkdir()
            (fallback/'owner-scheduler.json').write_text(json.dumps({'root': scope_file.read_text()}))
            clear_scopes(fallback)



def test_pytest_progress_stdout_failure_remains_a_diagnostic_failure(tmp_path):
    from tools.check_diagnostics import CheckOutput
    probe = tmp_path/'test_probe.py'
    probe.write_text('def test_probe(): pass\n')
    source = tmp_path/'closed_progress.py'
    source.write_text('import os, pytest\nfrom tools import check_diagnostics as diagnostics\n'
        'os.close(diagnostics._stdout)\ndiagnostics._stdout = None\n'
        f'raise SystemExit(pytest.main(["-q", "-p", "tools.check_progress", "-o", "addopts=", '
        f'"--rootdir", {str(tmp_path)!r}, "--confcutdir", {str(tmp_path)!r}, {str(probe)!r}]))\n')
    evidence = tmp_path/'evidence'
    result = subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--diagnostics', str(evidence), '--', sys.executable, str(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=20)
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert result.returncode == 86, result.stdout + result.stderr
    assert observed.first_error['operation'] == 'emit active test'
    assert 'FileNotFoundError' not in result.stdout + result.stderr
    report = json.loads((evidence/'result.json').read_text())
    assert report['product_exit_code'] is None and report['remaining_pids'] == []


@pytest.mark.parametrize('mode,expected_exit,product_exit', [('product', 7, 7), ('diagnostic', 86, None)])
def test_product_failure_survives_failed_worker_final_evidence(tmp_path, mode, expected_exit, product_exit):
    from tools.check_diagnostics import CheckOutput
    probe = tmp_path/'probe.py'
    probe.write_text('mode = ' + repr(mode) + '\n' + textwrap.dedent('''
        import os, sys
        from pathlib import Path
        state = Path(os.environ['CODEXON_CHECK_DIAGNOSTICS']) / f'worker-state-{os.getpid()}.json'
        state.unlink(); state.mkdir()
        if mode == 'diagnostic':
            from tools.check_diagnostics import write_json
            write_json(state, {})
        sys.exit(7)
    '''))
    evidence = tmp_path/'evidence'
    result = subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--diagnostics', str(evidence), '--', sys.executable, str(probe)],
        capture_output=True, text=True, timeout=20)
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert observed.product_exit_code == product_exit
    assert observed.first_error['path'].endswith('.json')
    report = json.loads((evidence/'result.json').read_text(encoding='utf-8'))
    assert report['process_exit_code'] == expected_exit and report['remaining_pids'] == []
    assert report['product_exit_code'] == product_exit
    assert report['diagnostics']['first_error'] is not None


def test_worker_product_exception_traceback_survives_failed_final_evidence(tmp_path):
    from tools.check_diagnostics import CheckOutput
    probe = tmp_path/'probe.py'
    probe.write_text(textwrap.dedent('''
        import os
        from pathlib import Path
        state = Path(os.environ['CODEXON_CHECK_DIAGNOSTICS']) / f'worker-state-{os.getpid()}.json'
        state.unlink(); state.mkdir()
        raise OSError('product defect')
    '''))
    evidence = tmp_path/'evidence'; evidence.mkdir()
    result = subprocess.run([sys.executable, str(ROOT/'tools/check_worker.py'), str(probe)],
        env={**os.environ, 'CODEXON_CHECK_DIAGNOSTICS':str(evidence), 'CODEXON_CHECK_TIMEOUT':'20'},
        capture_output=True, text=True, timeout=20)
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert result.returncode == observed.product_exit_code == 1, result.stdout + result.stderr
    assert 'Traceback (most recent call last)' in result.stderr
    assert "raise OSError('product defect')" in result.stderr
    assert 'OSError: product defect' in result.stderr
    assert observed.first_error['path'].endswith('.json')


def test_noop_progress_uses_complete_stdout_events_in_direct_and_parent_runners(tmp_path):
    from tools.check_diagnostics import CheckOutput
    from tools.verify_changes import run_logged
    probe = tmp_path/'test_noop.py'
    probe.write_text('import pytest\n@pytest.mark.parametrize("number", range(256))\ndef test_noop(number): pass\n')
    for parent in (False, True):
        evidence = tmp_path/('parent' if parent else 'direct')
        command = [sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
            '--diagnostics', str(evidence), '--', sys.executable, '-m', 'pytest', '-q',
            '-p', 'tools.check_progress', '-o', 'addopts=', '--rootdir', str(tmp_path),
            '--confcutdir', str(tmp_path), str(probe)]
        if parent:
            result = run_logged(command, tmp_path/'parent.log')
            assert result.product_exit_code == 0 and result.diagnostics['first_error'] is None
            output = result.stdout
        else:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, CODEXON_CHECK_EVENT_LOG=str(tmp_path/'inherited-events.log')))
            assert not (tmp_path/'inherited-events.log').exists()
            output = result.stdout
        assert result.returncode == 0, output
        observed = CheckOutput(); observed.feed(output, final=True)
        progress = [line.split('[codexon-check] ', 1)[1] for line in output.splitlines()
                    if '[codexon-check] {"active_test":' in line]
        assert progress and len(progress) == len(set(progress))
        report = json.loads((evidence/'result.json').read_text())
        assert report['active_test'] == observed.active_test
        assert observed.active_test['phase'] == 'finished'
        assert observed.active_test['test'].endswith('test_noop[255]')
        assert not (evidence/'active-test.json').exists()
        assert not (evidence/'active-test.tmp').exists()
        assert report['remaining_pids'] == []


@pytest.mark.parametrize('cleanup_error', [False, True])
def test_direct_runner_deadline_survives_unread_stdout_pipe(tmp_path, cleanup_error):
    probe = tmp_path/'blocked_output.py'
    marker = tmp_path/'worker.pid'
    probe.write_text(
        'import os, sys\nfrom pathlib import Path\n'
        'from tools.check_diagnostics import emit\n'
        'Path(sys.argv[1]).write_text(str(os.getpid()))\n'
        "emit(dict(active_test=dict(test='blocked-output', phase='call', pid=os.getpid())))\n"
        "os.write(1, b'x'*(4*1024*1024))\n"
        "raise RuntimeError('unread stdout accepted the entire probe')\n")
    diagnostics = tmp_path/'evidence'
    command = [sys.executable, str(ROOT/'tools/run_ui_checks.py'),
        '--no-desktop', '--timeout', '3', '--diagnostics', str(diagnostics), '--',
        sys.executable, str(probe), str(marker)]
    if cleanup_error:
        command = [sys.executable, str(cleanup_denial_wrapper(tmp_path)), *command[1:]]
    with (tmp_path/'stderr.log').open('wb') as errors:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=errors)
        try:
            code = process.wait(timeout=12)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            output, _ = process.communicate(timeout=5)
    assert code == 124, (code, (tmp_path/'stderr.log').read_text())
    report = json.loads((diagnostics/'result.json').read_text(encoding='utf-8'))
    assert report['reason'] == 'deadline' and report['remaining_pids'] == []
    assert int(marker.read_text()) in report['live_pids']
    assert report['active_test']['test'] == 'blocked-output'
    assert b'[codexon-check]' in output
    error = report['diagnostics']['first_error']
    assert (error is not None) == cleanup_error
    if cleanup_error:
        assert error['operation'] == 'remove subprocess capture'


def cleanup_denial_wrapper(tmp_path):
    wrapper = tmp_path/'cleanup_failure.py'
    wrapper.write_text(
        f'import sys\nsys.path.insert(0, {str(ROOT)!r})\n'
        'from tools import check_diagnostics as diagnostics\n'
        'from tools import run_ui_checks\n'
        'cleanup = diagnostics.tempfile.TemporaryDirectory.cleanup\n'
        'def denied(self):\n'
        '    cleanup(self)\n'
        "    raise PermissionError('injected capture cleanup denial')\n"
        'diagnostics.tempfile.TemporaryDirectory.cleanup = denied\n'
        'sys.argv = sys.argv[1:]\nraise SystemExit(run_ui_checks.main())\n')
    return wrapper


def test_direct_runner_capture_cleanup_failure_cannot_pass(tmp_path):
    from tools.check_diagnostics import CheckOutput
    diagnostics = tmp_path/'evidence'
    command = ['run_ui_checks.py', '--no-desktop', '--diagnostics', str(diagnostics),
               '--', sys.executable, '-c', 'pass']
    wrapper = cleanup_denial_wrapper(tmp_path)
    result = subprocess.run([sys.executable, str(wrapper), *command], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 86, result.stdout + result.stderr
    report = json.loads((diagnostics/'result.json').read_text(encoding='utf-8'))
    assert report['product_exit_code'] == 0 and report['remaining_pids'] == []
    assert report['diagnostics']['first_error']['operation'] == 'remove subprocess capture'
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert observed.product_exit_code == 0
    assert observed.first_error == report['diagnostics']['first_error']


def test_direct_runner_final_result_write_failure_cannot_pass(tmp_path):
    from tools.check_diagnostics import CheckOutput
    probe = tmp_path/'result_failure.py'
    probe.write_text('import os\nfrom pathlib import Path\n'
        "(Path(os.environ['CODEXON_CHECK_DIAGNOSTICS'])/'result.json').mkdir()\n")
    result = subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--diagnostics', str(tmp_path/'evidence'), '--', sys.executable, str(probe)],
        cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 86, result.stdout + result.stderr
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert observed.product_exit_code == 0
    assert observed.first_error['operation'] == 'write JSON'
    assert observed.first_error['path'].endswith('result.json')


def test_nested_runner_restores_its_host_worker_event_route(tmp_path):
    source = tmp_path/'nested_runner.py'
    inner = tmp_path/'inner'
    inner_code = ("from tools.check_diagnostics import emit; "
        "emit(dict(active_test=dict(test='inner-only',phase='call')),strict=True)")
    command = ['run_ui_checks.py', '--no-desktop', '--diagnostics', str(inner),
               '--', sys.executable, '-c', inner_code]
    source.write_text(
        'import os, sys\nfrom pathlib import Path\n'
        'from tools import check_diagnostics as diagnostics\nfrom tools import run_ui_checks\n'
        'emit = diagnostics.emit\nowned = Path(diagnostics._event_log)\n'
        "emit(dict(active_test=dict(test='outer-before',phase='call')),strict=True)\n"
        f'sys.argv = {command!r}\nassert run_ui_checks.main() == 0\n'
        "emit(dict(active_test=dict(test='outer-after',phase='finished')),strict=True)\n"
        "events = owned.read_text(encoding='utf-8')\n"
        "assert 'inner-only' not in events\nassert 'outer-after' in events\n")
    outer = tmp_path/'outer'
    result = subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--diagnostics', str(outer), '--', sys.executable, str(source)],
        cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((outer/'result.json').read_text(encoding='utf-8'))
    assert report['active_test']['test'] == 'outer-after'
    assert report['remaining_pids'] == []


def test_supervisor_capture_failure_retains_unknown_product_outcome(tmp_path):
    from tools.check_diagnostics import CheckOutput
    diagnostics = tmp_path/'evidence'
    wrapper = tmp_path/'capture_failure.py'
    command = ['run_ui_checks.py', '--no-desktop', '--diagnostics', str(diagnostics),
               '--', sys.executable, '-c', 'pass']
    wrapper.write_text(
        f'import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom pathlib import Path\n'
        'from tools import run_ui_checks\noriginal = Path.open\n'
        'def denied(self, *args, **kwargs):\n'
        "    if self.name == 'stdout.log' and args and args[0] == 'rb':\n"
        "        raise PermissionError('injected capture reader denial')\n"
        '    return original(self, *args, **kwargs)\nPath.open = denied\n'
        f'sys.argv = {command!r}\nraise SystemExit(run_ui_checks.main())\n')
    result = subprocess.run([sys.executable, str(wrapper)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 86, result.stdout + result.stderr
    observed = CheckOutput(); observed.feed(result.stdout, final=True)
    assert observed.has_outcome and observed.product_exit_code is None
    assert observed.first_error['operation'] == 'open runner capture reader'


def test_worker_default_emit_keeps_a_complete_frame_under_stdout_backpressure(tmp_path):
    import ctypes
    from ctypes import wintypes as W
    import msvcrt
    import time
    from tools.check_diagnostics import CheckOutput
    event = dict(active_test=dict(test='large-'+'x'*32768, phase='call'))
    probe = tmp_path/'large_event.py'
    probe.write_text('from tools.check_diagnostics import emit\n'
                     f'emit({event!r})\n')
    evidence = tmp_path/'evidence'
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.PeekNamedPipe.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD,
        ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD), ctypes.POINTER(W.DWORD)]
    api.PeekNamedPipe.restype = W.BOOL
    process = subprocess.Popen([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--timeout', '10', '--diagnostics', str(evidence), '--', sys.executable, str(probe)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+8
        available = W.DWORD()
        while not available.value:
            assert api.PeekNamedPipe(msvcrt.get_osfhandle(process.stdout.fileno()), None,
                                     0, None, ctypes.byref(available), None)
            assert time.monotonic() < deadline
            if not available.value:
                time.sleep(.01)
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    observed = CheckOutput(); observed.feed(output.decode('utf-8'), final=True)
    report = json.loads((evidence/'result.json').read_text(encoding='utf-8'))
    assert observed.active_test == event['active_test'] == report['active_test']
    assert report['remaining_pids'] == []



@pytest.mark.parametrize('target', [
    'tests/test_verify_changes.py::test_first_diagnostic_survives_large_output_and_product_failure',
    'tests/test_ui_check_runner.py::test_worker_product_exception_traceback_survives_failed_final_evidence',
])
def test_independent_child_diagnostics_stay_with_their_capture_owner(tmp_path, target):
    evidence = tmp_path/'evidence'
    result = subprocess.run([sys.executable, str(ROOT/'tools/run_ui_checks.py'), '--no-desktop',
        '--kind', 'core', '--timeout', '20', '--diagnostics', str(evidence), '--', sys.executable,
        '-m', 'pytest', '-q', '-p', 'tools.check_progress', '-o', 'addopts=', target],
        cwd=ROOT, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((evidence/'result.json').read_text(encoding='utf-8'))
    assert report['product_exit_code'] == 0 and report['diagnostics']['first_error'] is None
    assert report['remaining_pids'] == []
