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
