import ctypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cachemonitor.graphics_recovery import WindowsRestart, restart_arguments


def test_restart_preserves_paths_without_replaying_handoff(tmp_path):
    homes = [tmp_path/'한글 홈', tmp_path/'second home']
    arguments = restart_arguments(homes, index_path=tmp_path/'index.sqlite',
        evidence_path=tmp_path/'evidence.sqlite', quota_path=tmp_path/'quota.sqlite', frozen=True)
    expected = ['--software-rendering', '--managed-services']
    for home in homes:
        expected += ['--codex-home', str(home.resolve())]
    for flag, name in (('--index-path', 'index.sqlite'), ('--evidence-path', 'evidence.sqlite'),
                       ('--quota-path', 'quota.sqlite')):
        expected += [flag, str((tmp_path/name).resolve())]
    assert arguments == subprocess.list2cmdline(expected)
    assert '--hidden' not in arguments and '--replace-gui' not in arguments
    source = restart_arguments(homes, frozen=False)
    assert source.startswith(subprocess.list2cmdline([str(Path(__file__).resolve().parents[1]/'run.py')]))


def test_registration_failure_is_visible_and_does_not_claim_protection():
    registration = WindowsRestart('x'*1100, api=SimpleNamespace(
        RegisterApplicationRestart=lambda *_: -2147024809))
    assert not registration.arm()
    assert not registration.active and '0x80070057' in registration.error
    assert registration.disarm()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows restart API')
def test_native_restart_registration_roundtrip_and_unregister(tmp_path):
    api = ctypes.WinDLL('kernel32')
    api.GetCurrentProcess.restype = ctypes.c_void_p
    api.GetApplicationRestartSettings.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    api.GetApplicationRestartSettings.restype = ctypes.c_long
    def read():
        buffer = ctypes.create_unicode_buffer(1024)
        length = ctypes.c_uint32(len(buffer));flags = ctypes.c_uint32()
        result = api.GetApplicationRestartSettings(api.GetCurrentProcess(), buffer,
                                                   ctypes.byref(length), ctypes.byref(flags))
        return result, buffer.value, flags.value
    registration = WindowsRestart(restart_arguments([tmp_path/'한글 home'], frozen=True))
    try:
        assert registration.arm(), registration.error
        result, command, flags = read()
        assert result == 0 and command == registration.arguments and flags == 12
        assert registration.disarm()
        assert read()[0] & 0xffffffff == 0x80070490  # ERROR_NOT_FOUND
    finally:
        registration.disarm()


@pytest.mark.parametrize('handoff', [False, True])
def test_exit_disarms_before_scene_teardown(tmp_path, handoff):
    import time
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app = QApplication.instance() or QApplication([])
    events = []
    previous = getattr(app, '_windows_restart', None)
    registration = SimpleNamespace(disarm=lambda: events.append('disarm'), arm=lambda: events.append('arm'))
    app._windows_restart = registration
    window = Dashboard([], start_worker=False, settings=QSettings(str(tmp_path/'ui.ini'), QSettings.IniFormat))
    try:
        original_finish = window.finish_quit
        def finish():
            events.append('teardown')
            original_finish()
        window.finish_quit = finish
        window.begin_quit(handoff=handoff)
        deadline=time.monotonic()+5
        while 'teardown' not in events and time.monotonic()<deadline:
            app.processEvents()
        assert events == ['disarm', 'teardown']
    finally:
        app._windows_restart = previous
        window.quitting = True;window.release_scene();window.close();window.deleteLater();app.processEvents()


@pytest.mark.parametrize('status',['failed','cancelled'])
def test_unfinished_shutdown_rearms_restart(monkeypatch,status):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.tray import TrayWindow, QMessageBox
    from cachemonitor.shutdown_state import ExitResult
    app = QApplication.instance() or QApplication([])
    events = []
    previous = getattr(app, '_windows_restart', None)
    app._windows_restart = SimpleNamespace(arm=lambda: events.append('arm'))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *_: events.append('warning'))
    window = SimpleNamespace(_closing=True, shutdown_operation=SimpleNamespace(
        outcome=ExitResult(status,error='failed' if status=='failed' else ''),
        display_stopping=False,deleteLater=lambda:None),
        shutdown_dialog=SimpleNamespace(accept=lambda: None, deleteLater=lambda: None))
    window.set_exit_pending=lambda value:setattr(window,'_closing',value)
    try:
        TrayWindow.shutdown_finished(window)
        assert not window._closing and events == (['arm','warning'] if status=='failed' else ['arm'])
    finally:
        app._windows_restart = previous


def test_qml_cache_retains_recent_bytecode_and_foreign_files(tmp_path):
    from cachemonitor.quick_runtime import trim_qml_cache
    cache=tmp_path/'qmlcache';cache.mkdir()
    old=cache/('a'*40+'.qmlc');recent=cache/('b'*40+'.qmlc')
    old.write_bytes(b'old');recent.write_bytes(b'recent')
    os.utime(old,(100,100));os.utime(recent,(200,200))
    foreign=cache/'settings.json';foreign.write_bytes(b'preserve')
    pending=cache/('c'*40+'.qmlc.tmp');pending.write_bytes(b'pending writer')
    trim_qml_cache(cache,budget=6)
    assert not old.exists() and recent.read_bytes()==b'recent'
    assert foreign.read_bytes()==b'preserve' and pending.read_bytes()==b'pending writer'
    trim_qml_cache(cache,budget=6)
    assert recent.read_bytes()==b'recent'


def test_software_renderer_uses_cpu_even_with_inherited_d3d_override(tmp_path):
    code = '''
from cachemonitor.graphics_recovery import use_software_renderer
use_software_renderer()
from PySide6.QtWidgets import QApplication
from PySide6.QtQuick import QSGRendererInterface
from cachemonitor.quick_runtime import QuickHost
from cachemonitor.presentation import Text
app = QApplication([])
host = QuickHost();host.resize(240,100)
host.set_scene(Text('CPU renderer'), 'Main.qml');host.show()
app.processEvents()
assert host.quick.quickWindow().rendererInterface().graphicsApi() == QSGRendererInterface.Software
assert not host.quick.grabFramebuffer().isNull()
assert not host.qml_errors
host.release_scene();host.close()
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, QT_QUICK_BACKEND='rhi', QSG_RHI_BACKEND='d3d11', QT_WIDGETS_RHI='1'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_software_text_matches_unclipped_glyph_runs():
    code = r"""
import time
from pathlib import Path
from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QPainter, QTextLayout, QTextOption
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import QQuickItem, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QApplication
from cachemonitor.fonts import (configure_application_font, configure_font_rendering,
                                configure_high_dpi, load_bundled_fonts)
from cachemonitor.graphics_recovery import use_software_renderer
from cachemonitor.theme import shared_theme
configure_font_rendering();configure_high_dpi();use_software_renderer()
app = QApplication([])
load_bundled_fonts()
app.setStyle('Fusion')
configure_application_font(app)
host = QQuickWidget()
host.resize(540, 110)
host.engine().rootContext().setContextProperty('appTheme', shared_theme())
frames = []
host.quickWindow().afterRendering.connect(lambda: frames.append(True))
results = []
cases = [('UiButton', '2026.10.24.3'), ('UiButton', '2026.10.25.1'),
         ('Text', '2026.10.24.3'), ('TextInput', '2026.10.25.1'),
         ('TextEdit', '2026.10.24.3'), ('UiInput', '2026.10.25.1')]
for kind, text in cases:
    if kind == 'UiButton':
        properties = 'width:160;height:36;role:"quiet"'
    else:
        properties = 'width:320;height:40;font.family:"Pretendard JP";font.pixelSize:14'
        if kind != 'Text':
            properties += ';readOnly:true;cursorVisible:false'
        if kind == 'UiInput':
            properties += ';background:null'
    qml = ('import QtQuick\nRectangle { width:540;height:110;color:"white"; '
           + kind + ' { x:36;y:36;text:"' + text + '";' + properties + ' } }')
    component = QQmlComponent(host.engine())
    component.setData(qml.encode(), QUrl.fromLocalFile(str(Path('cachemonitor/qml/GlyphProbe.qml').resolve())))
    root = component.create()
    assert root is not None, component.errors()
    host.setContent(QUrl(), component, root)
    captions = [item for item in root.findChildren(QQuickItem)
                if item.property('text') == text and item.metaObject().indexOfProperty('contentWidth') >= 0]
    assert len(captions) == 1, (kind, captions)
    caption = captions[0]
    frame_count = len(frames)
    host.show();host.quickWindow().update();host.update();host.repaint()
    deadline = time.monotonic() + 5
    while len(frames) <= frame_count:
        assert time.monotonic() < deadline, 'No scene rendering completed'
        app.processEvents()
    image = host.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888)
    assert not image.isNull()
    ratio = image.devicePixelRatio()
    assert ratio == 1.5, ratio
    assert host.devicePixelRatioF() == ratio
    assert host.quickWindow().rendererInterface().graphicsApi() == QSGRendererInterface.Software
    assert caption.property('truncated') is not True, (kind, text)
    left = caption.property('leftPadding')
    right = caption.property('rightPadding')
    content_width = caption.property('contentWidth')
    assert content_width <= caption.width() - left - right, (kind, text, content_width)
    origin = caption.mapToScene(QPointF(0, 0))
    offset_x = left
    if host.engine().newQObject(caption).property('horizontalAlignment').toInt() == Qt.AlignHCenter:
        offset_x += (caption.width() - left - right - content_width) / 2
    layout = QTextLayout(text, caption.property('font'))
    option = QTextOption()
    option.setUseDesignMetrics(True)
    layout.setTextOption(option)
    layout.beginLayout()
    line = layout.createLine()
    line.setLineWidth(500)
    layout.endLayout()
    reference = QImage(image.size(), image.format())
    reference.setDevicePixelRatio(ratio)
    reference.fill(QColor('white'))
    painter = QPainter(reference)
    painter.setPen(caption.property('color'))
    for run in layout.glyphRuns():
        painter.drawGlyphRun(QPointF(origin.x() + offset_x,
                                    origin.y() + caption.property('baselineOffset') - line.ascent()), run)
    painter.end()
    actual = bytes(image.bits())
    expected = bytes(reference.bits())
    assert min(expected) < 255, (kind, text, 'Empty reference')
    differences = [(actual[i:i+4], expected[i:i+4]) for i in range(0, len(actual), 4)
                   if actual[i:i+4] != expected[i:i+4]]
    missing = sum(any(a > e for a, e in zip(pixel, ref)) for pixel, ref in differences)
    extra = sum(any(a < e for a, e in zip(pixel, ref)) for pixel, ref in differences)
    results.append((kind, text, missing, extra))
host.close()
for kind, text, missing, extra in results:
    print(f'{kind} {text}: missing={missing} extra={extra}', flush=True)
assert all(missing == extra == 0 for _, _, missing, extra in results), 'Glyph ink differs from the unclipped reference'
"""
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=Path(__file__).resolve().parents[1],
                            env=dict(os.environ, QT_SCALE_FACTOR='1', QT_SCREEN_SCALE_FACTORS='1.5'),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
