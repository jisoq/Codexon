# The recovery runtime is self-contained. It never loads the desktop Qt bundle.
a = Analysis(['recovery_main.py'], pathex=[], binaries=[], datas=[('cachemonitor/assets/i18n', 'cachemonitor/assets/i18n')], hiddenimports=[],
             excludes=['PySide6', 'shiboken6', 'pytest', 'aiohttp', 'httpx', 'ijson', 'cryptography'],
             noarchive=False, optimize=0)
if not any(name == 'tkinter' for name, *_ in a.pure) or not any(
        name.replace('\\', '/').endswith('/init.tcl') for name, *_ in a.datas):
    raise RuntimeError('Recovery requires a working Tcl/Tk runtime; do not package a GUI-less recovery tool.')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='CodexonRecovery',
          icon='icons/Codexon.ico', console=False, debug=False, strip=False, upx=False)
