# Native macOS products share application code, not each other's runtimes.
import os
import platform
import sys
from pathlib import Path

if sys.platform != 'darwin':
    raise SystemExit('Build macOS products on macOS.')

root = Path(SPECPATH)
target = os.environ.get('CODEXON_TARGET_ARCH', platform.machine())
signing = os.environ.get('CODEXON_SIGNING_IDENTITY') or None
icon = os.environ.get('CODEXON_MAC_ICON') or None

blocked = ('qt3d', 'qtcharts', 'qtgraphs', 'qtdatavisualization', 'qtquick3d',
           'qtquicktimeline', 'qtvirtualkeyboard', 'qtwebengine', 'qtwebview',
           'qtlocation', 'qtmultimedia', 'qtpdf', 'qtpositioning', 'qtremoteobjects',
           'qtscxml', 'qtsensors', 'qtspatialaudio', 'qttexttospeech', 'qtwebchannel',
           'qtwebsockets', 'qtquicktest', 'qtquickparticles', 'qtquickvectorimage',
           'qtlabswavefrontmesh', 'qtquickshapesdesignhelpers')
blocked_qml = ('/qt3d/', '/qtcharts/', '/qtgraphs/', '/qtquick3d/', '/qtquick/timeline/',
               '/qtquick/virtualkeyboard/', '/qtvirtualkeyboard/', '/qtwebengine/',
               '/qtwebview/', '/qtdatavisualization/', '/qtlocation/', '/qtmultimedia/',
               '/qtpdf/', '/qtpositioning/', '/qtremoteobjects/', '/qtscxml/', '/qtsensors/',
               '/qtspatialaudio/', '/qttexttospeech/', '/qtwebchannel/', '/qtwebsockets/',
               '/qtquick/particles/', '/qtquick/vectorimage/', '/qt5compat/', '/qttest/')

def keep(entry):
    name = entry[0].replace('\\', '/').lower()
    segments = name.split('/')
    return not (any(part.startswith(blocked) for part in segments)
                or any(mark in '/'+name for mark in blocked_qml))

def product(script, binary, label, identifier, *, desktop=False):
    exclusions = ['PyQt5', 'tkinter', '_tkinter', 'pytest']
    exclusions += ['PySide6.'+name for name in ('QtCharts','QtGraphs','Qt3DCore', 'Qt3DAnimation',
        'Qt3DExtras','Qt3DInput','Qt3DLogic','Qt3DRender','QtDataVisualization', 'QtQuick3D',
        'QtVirtualKeyboard','QtWebEngineCore','QtWebEngineQuick','QtWebView','QtMultimedia','QtPdf')]
    if not desktop:exclusions += ['PySide6', 'shiboken6', 'cryptography', 'httpx', 'aiohttp', 'ijson']
    data = [(str(root/'cachemonitor/assets/i18n'), 'cachemonitor/assets/i18n')]
    if desktop:data = [(str(root/'cachemonitor/assets'), 'cachemonitor/assets'),
                       (str(root/'cachemonitor/qml'), 'cachemonitor/qml')]
    analysis = Analysis([str(root/script)], pathex=[str(root)], binaries=[], datas=data,
                        hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
                        excludes=exclusions, noarchive=False, optimize=0)
    analysis.binaries = [entry for entry in analysis.binaries if keep(entry)]
    analysis.datas = [entry for entry in analysis.datas if keep(entry)]
    pyz = PYZ(analysis.pure)
    exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name=binary,
              debug=False, strip=False, upx=False, console=False, argv_emulation=not desktop,
              target_arch=target, codesign_identity=signing,
              entitlements_file=str(root/'installer/macos.entitlements') if desktop else None)
    collected = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name=binary)
    info = {'NSHighResolutionCapable':True, 'LSMinimumSystemVersion':'13.0',
            'CFBundleDisplayName':label, 'NSHumanReadableCopyright':'Copyright jisoq',
            'LSApplicationCategoryType':'public.app-category.developer-tools'}
    if binary == 'CodexonRecovery':
        info['CFBundleURLTypes'] = [{'CFBundleURLName':'Codexon connection recovery',
                                    'CFBundleURLSchemes':['codexon-recovery']}]
    return BUNDLE(collected, name=label+'.app', icon=icon, bundle_identifier=identifier, info_plist=info)

app = product('run.py', 'Codexon', 'Codexon', 'io.github.jisoq.codexon', desktop=True)
recovery = product('recovery_main.py', 'CodexonRecovery', 'Codexon Recovery', 'io.github.jisoq.codexon.recovery')
installer = product('mac_installer_main.py', 'CodexonInstaller', 'Install Codexon', 'io.github.jisoq.codexon.installer')
