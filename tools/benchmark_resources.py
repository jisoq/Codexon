"""Measure isolated GUI allocation or a read-only quota report on fixed input."""
import argparse
import ctypes
from ctypes import wintypes as W
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def resources():
    from cachemonitor.performance_probe import private_bytes
    class Thread(ctypes.Structure):
        _fields_ = [(name, W.DWORD) for name in ('size', 'usage', 'tid', 'pid')] + [
            ('base', W.LONG), ('delta', W.LONG), ('flags', W.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.Thread32First.argtypes = kernel.Thread32Next.argtypes = [W.HANDLE, ctypes.POINTER(Thread)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.GetProcessIoCounters.argtypes = [W.HANDLE, ctypes.POINTER(IO)]
    kernel.GetProcessHandleCount.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    handle = kernel.CreateToolhelp32Snapshot(4, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = Thread(); entry.size = ctypes.sizeof(entry)
    count = 0
    try:
        found = kernel.Thread32First(handle, ctypes.byref(entry))
        while found:
            count += entry.pid == os.getpid()
            found = kernel.Thread32Next(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    io = IO(); handles = W.DWORD(); process = kernel.GetCurrentProcess()
    if not kernel.GetProcessIoCounters(process, ctypes.byref(io)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(private_bytes=private_bytes(os.getpid()), threads=count, handles=handles.value,
                read_bytes=io.read_bytes, write_bytes=io.write_bytes, cpu_seconds=time.process_time())


# Keep canonical values independent of Python container sharing and pickle IDs.
def canonical(value):
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def quota(args):
    from cachemonitor.quota_cycles import QuotaLedger
    from cachemonitor.quota_view import prepare_quota_view
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger.db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    ledger.db.row_factory = sqlite3.Row
    ledger.db.execute('pragma query_only=on')
    ledger.db.execute(f'pragma cache_size=-{getattr(QuotaLedger,"CACHE_KIB",2000)}')
    home = ledger.db.execute('select home from tracking_config order by home limit 1').fetchone()[0]
    samples = []; previous = None
    try:
        for _ in range(args.samples):
            before = resources(); started = time.perf_counter()
            report = ledger.report(home, now=args.now)
            report_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            report['view'] = prepare_quota_view(report)
            view_ms = (time.perf_counter() - started) * 1000
            after = resources()
            previous = report
            gc.collect()
            samples.append(dict(report_ms=report_ms, view_ms=view_ms,
                                cpu_seconds=after['cpu_seconds'] - before['cpu_seconds'],
                                read_bytes=after['read_bytes'] - before['read_bytes'],
                                overlap_private_bytes=after['private_bytes'],
                                retained_private_bytes=resources()['private_bytes']))
        return dict(kind='quota', now=args.now, samples=samples,
                    history_count=len(previous['history']), cycle_count=len(previous['cycles']),
                    report_digest=fingerprint({k: v for k, v in previous.items() if k != 'view'}),
                    view_digest=fingerprint(previous['view']))
    finally:
        ledger.close()


def gui(args):
    from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
    configure_font_rendering(); configure_high_dpi()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QSettings
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.overlay import OverlayController
    app = QApplication([]); load_bundled_fonts()
    app.setProperty('cachemonitorDisableShellIntegration', True)
    def settle():
        deadline = time.monotonic() + .8
        while time.monotonic() < deadline:
            app.processEvents(); time.sleep(.005)
    stages = {'application': resources()}
    with tempfile.TemporaryDirectory(prefix='codexon-resource-check-') as folder:
        settings = QSettings(str(Path(folder)/'settings.ini'), QSettings.IniFormat)
        settings.setValue('overlay/enabled', False)
        window = Dashboard([], start_worker=False, settings=settings, live_limits=False,
                           index_path=str(Path(folder)/'index.sqlite'),
                           quota_path=str(Path(folder)/'quota.sqlite'), collection_autostart=False)
        overlay = OverlayController(settings, native_enabled=False)
        try:
            settle(); stages['hidden'] = resources()
            window.show(); settle(); stages['dashboard'] = resources()
            # Render each companion without attaching to another process or Explorer.
            for companion in (overlay.widget, overlay.shadow, *overlay.chrome):
                companion.show()
            settle(); stages['all_scenes'] = resources()
            return dict(kind='gui', renderer=str(window.quick.quickWindow().rendererInterface().graphicsApi()),
                        dpr=window.devicePixelRatioF(), stages=stages,
                        qml_errors=sum(len(w.qml_errors) for w in
                                       (window, window.taskbar_quota, overlay.widget, overlay.shadow, *overlay.chrome)))
        finally:
            overlay.stop(); window.quit_app(); settle()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('quota', 'gui'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--now', type=float)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.kind == 'quota' and (args.database is None or args.now is None):
        parser.error('quota requires a fixed database and --now')
    report = quota(args) if args.kind == 'quota' else gui(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
