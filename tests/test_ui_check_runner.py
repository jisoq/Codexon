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
        [sys.executable, str(ROOT / 'tools/run_ui_checks.py'), '--scale', '1.25', '--',
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


@pytest.mark.parametrize('mode,expected', [('orphan', 1), ('hang', 124)])
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
        import sys, time
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
    finally:
        if scope_file.exists():
            # Keep a failing regression run safe even if automatic registration
            # breaks. This fallback runs only after inspecting runner evidence.
            fallback = tmp_path/'fallback'; fallback.mkdir()
            (fallback/'owner-scheduler.json').write_text(json.dumps({'root': scope_file.read_text()}))
            clear_scopes(fallback)
