# The recovery runtime is self-contained. It never loads the desktop Qt bundle.
a = Analysis(['recovery_main.py'], pathex=[], binaries=[], datas=[], hiddenimports=[],
             excludes=['PySide6', 'shiboken6', 'pytest', 'aiohttp', 'httpx', 'ijson', 'cryptography'],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='CodexonRecovery',
          icon='icons/Codexon.ico', console=False, debug=False, strip=False, upx=False)
