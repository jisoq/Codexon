# Corresponding Qt and PySide6 source

The Windows binary dynamically loads Qt/PySide6 6.11.2. The matching release provides `Codexon-third-party-source-6.11.2.zip`, containing the exact-version Qt Base, Qt Declarative, Qt SVG, Qt Shader Tools, and PySide6 source archives with upstream SHA-256 values. This source asset is mirrored under the release maintainer's control alongside the binary ZIP.

To run the application with compatible modified LGPL libraries, extract the Windows binary ZIP, keep a copy of its original `_internal/PySide6` folder, and replace the relevant Qt DLLs and QML plugin DLLs in that folder with Windows x64 builds from the corresponding source, using the same 6.11.2 ABI. Run `Codexon.exe --verify-runtime <absolute-output-path>` to check loading. Run the isolated packaged smoke described in [verification](docs/verification.md) to test the UI. Codexon adds no code-signature or device restriction that prevents use of modified libraries.

Qt and PySide6 have module-specific license terms. Read `LICENSES/LGPL-3.0.txt`, `LICENSES/GPL-3.0.txt`, and the package notices for the exact binary release.
