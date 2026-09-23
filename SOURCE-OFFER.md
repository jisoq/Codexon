# Corresponding Qt and PySide6 source

The Windows installation dynamically loads Qt/PySide6 6.11.2. Each release must include `Codexon-third-party-source-6.11.2.zip` and its `.sha256` file alongside `Codexon-Setup.exe` and its checksum. The source ZIP contains the exact-version Qt Base, Qt Declarative, Qt SVG, Qt Shader Tools, and PySide6 source archives with upstream SHA-256 values, mirrored under the release maintainer's control.

To use compatible modified LGPL libraries:

1. Install with Setup. Read `AppPath` from `HKCU\Software\Codexon` to locate the active `Codexon.exe`; each version has its own directory below the installation root's `versions` directory.
2. Copy that executable's entire directory to a separate test folder. Preserve its original `_internal/PySide6` directory as a backup.
3. In the test copy, replace the relevant Qt DLLs and QML plugin DLLs under `_internal/PySide6` with Windows x64 builds from the corresponding source, using the same 6.11.2 ABI.
4. Run the copied `Codexon.exe --verify-runtime <absolute-output-path>` to check loading. Follow the [verification guide](https://github.com/jisoq/Codexon/blob/main/docs/verification.md) for a packaged UI check with synthetic records.
5. To use the libraries in the installed version, exit Codexon and finish active proxy connections before applying the same replacements there. Preserve the original folder for restoration. A future Setup update creates a new version directory, so apply your compatible libraries again to that version.

Codexon adds no code-signature or device restriction that prevents use of modified libraries. Do not modify a running installation during verification.

Qt and PySide6 have module-specific license terms. Read `LICENSES/LGPL-3.0.txt`, `LICENSES/GPL-3.0.txt`, and the package notices for the exact binary release.
