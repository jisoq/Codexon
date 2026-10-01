# Resolve native dependencies from this Python environment and Windows only.
# An unrelated PATH entry can provide an incompatible DLL with the same name.
import os
import sys
from pathlib import Path
if sys.platform == 'win32':
    os.environ['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), sys.base_prefix,
        str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']))

# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[('cachemonitor/assets', 'cachemonitor/assets'), ('cachemonitor/qml', 'cachemonitor/qml')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PySide6.QtCharts', 'PySide6.QtGraphs',
              'PySide6.Qt3DCore', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
              'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DRender',
              'PySide6.QtDataVisualization', 'PySide6.QtQuick3D',
              'PySide6.QtVirtualKeyboard', 'PySide6.QtWebEngineCore',
              'PySide6.QtWebEngineQuick', 'PySide6.QtWebView'],
    noarchive=False,
    optimize=0,
)

if sys.platform == 'win32':
    roots = [Path(p).resolve() for p in (sys.prefix, sys.base_prefix, os.environ['SystemRoot'])]
    foreign = [source for _, source, kind in a.binaries if kind == 'BINARY'
               and not any(Path(source).resolve().is_relative_to(root) for root in roots)]
    if foreign:
        raise RuntimeError('Native dependencies escaped the build environment: ' + repr(foreign))

# PyInstaller's Qt hook collects optional QML modules that the application
# never imports. Keep GPL-only add-ons out of the redistributed bundle.
blocked_dlls = ('qt63d', 'qt6charts', 'qt6graphs', 'qt6datavisualization',
                'qt6quick3d', 'qt6quicktimeline', 'qt6virtualkeyboard',
                'qt6webengine', 'qt6webview', 'qt6location', 'qt6multimedia',
                'qt6pdf', 'qt6positioning', 'qt6remoteobjects', 'qt6scxml',
                'qt6sensors', 'qt6spatialaudio', 'qt6texttospeech',
                'qt6webchannel', 'qt6websockets', 'qt6quicktest',
                'qt6quickparticles', 'qt6quickvectorimage',
                'qt6labswavefrontmesh', 'qt6quickshapesdesignhelpers')
blocked_qml = ('/qt3d/', '/qtcharts/', '/qtgraphs/', '/qtquick3d/',
               '/qtquick/timeline/', '/qtquick/virtualkeyboard/',
               '/qtvirtualkeyboard/', '/qtwebengine/', '/qtwebview/',
               '/qtdatavisualization/', '/qtlocation/', '/qtmultimedia/',
               '/qtpdf/', '/qtpositioning/', '/qtremoteobjects/',
               '/qtscxml/', '/qtsensors/', '/qtspatialaudio/',
               '/qttexttospeech/', '/qtwebchannel/', '/qtwebsockets/',
               '/qtquick/particles/', '/qtquick/vectorimage/',
               '/qt5compat/', '/qttest/')
def keep_runtime(entry):
    name = entry[0].replace('\\', '/').lower()
    basename = name.rsplit('/', 1)[-1]
    return not (basename.startswith(blocked_dlls) or any(mark in '/' + name for mark in blocked_qml))
a.binaries = [entry for entry in a.binaries if keep_runtime(entry)]
a.datas = [entry for entry in a.datas if keep_runtime(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Codexon',
    debug=False,
    icon='icons/Codexon.ico',
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Codexon',
)
