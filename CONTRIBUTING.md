# Contributing

Codexon is a Windows application built with Python 3.12.10 and PySide6/Qt Quick. Use a fresh clone and a dedicated environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-release.lock
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe tools/verify_changes.py --plan
.\.venv\Scripts\python.exe tools/verify_changes.py --full
.\build.ps1 -DistPath dist-releases/<version> -WorkPath build/release-<version> -PythonPath .venv/Scripts/python.exe
```

Read [verification](docs/verification.md) before modifying the proxy or publishing a build. Tests use isolated Codex homes and ports. Do not attach real Codex records, account files, databases, or unredacted diagnostics to issues or pull requests. For a vulnerability, use [private reporting](SECURITY.md).

The product build also freezes `CodexonRecovery.exe` with its own Python/Tcl/Tk runtime and no Qt dependency. Build Setup using Inno Setup 6.7.3 or compatible:

```powershell
.\tools\Build-Installer.ps1 -ProductDir dist-releases/<version>/Codexon -OutputDir dist-releases/<version>/setup -Compiler '<Inno Setup>/ISCC.exe'
```

Use `-Isolated` and a separate output directory for installation QA. The QA installer uses a separate app ID and never activates the production proxy. Validate the payload with `python tools/package_release.py --product <product-directory> --validate-only`. Publish `Codexon-Setup.exe` and `Codexon-Setup.exe.sha256`; the app updater uses the checksum automatically. Do not publish a portable app ZIP. Keep all bundled third-party notices, source offers and relinking instructions. Signing credentials are not stored in this repository.
