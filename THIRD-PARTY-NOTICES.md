# Third-party notices

Codexon source code is licensed under the custom [Codexon Attribution License 1.0](LICENSE), which requires preservation of the original author and repository attribution. Its Windows distribution also contains independently licensed software. The final packaged inventories are `BUNDLED-PYTHON.md` and `BUNDLED-QT.md`, included beside this file in the ZIP.

| Component | Distributed role | License and source obligations |
| --- | --- | --- |
| Python 3.12.10 | Embedded runtime | Python Software Foundation License; include its notice and corresponding source information. |
| Tcl/Tk 8.6 | Independent connection recovery UI | Tcl/Tk notices are included under `LICENSES/recovery/`. The recovery executable embeds its own Python and Tcl/Tk runtime and does not load Qt. |
| PySide6, shiboken6, Qt 6.11.2 | UI runtime | Module-specific LGPL-3.0 or GPL-3.0/commercial terms. `LICENSES/` contains license texts; `SOURCE-OFFER.md` explains the matching source release asset and how to replace LGPL libraries. |
| Pretendard JP | UI font | SIL Open Font License 1.1; `cachemonitor/assets/fonts/OFL.txt` is included in source and distribution. |
| Other bundled Python packages and native libraries | Runtime dependencies | The frozen module graph generates `BUNDLED-PYTHON.md` and copies the installed license files under `LICENSES/python/`. |

The PyInstaller build must exclude unused GPL-only Qt modules such as Qt Charts, Qt Graphs, Qt Quick 3D, and Qt Virtual Keyboard. If any are actually required, the distribution terms must be reviewed before release. Codexon's license does not override a bundled library's license.

Cache-management design acknowledges [CacheKeeper](https://github.com/grapefruit0205/cachekeeper), commit `9f0fda397cb559b60bf9eb96d72b1fd721ed7c72`, MIT, Copyright (c) 2026 Junseok Pak. Codexon's implementation adapts its audit/automatic-maintenance/guard objectives to Codex telemetry and execution; it does not copy Claude quota coefficients or cache lifetimes. The MIT notice is retained in `LICENSES/CacheKeeper-MIT.txt`.
