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
