# Contributing

Codexon is a Windows and macOS application built with Python 3.12 and PySide6/Qt Quick. Windows builds use Python 3.12.10; macOS builds use 3.12.13. Use a fresh clone and a dedicated environment. On Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-release.lock
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe tools/verify_changes.py --plan
.\.venv\Scripts\python.exe tools/run_ui_checks.py -- .\.venv\Scripts\python.exe tools/verify_changes.py
.\build.ps1 -DistPath dist-releases/<version> -WorkPath build/release-<version> -PythonPath .venv/Scripts/python.exe
```

The selector runs only checks related to changed behavior; small UI edits do not require backend or installation checks. Reuse existing coverage and add tests only for a material gap. Use `--full` for broad shared changes or release verification. Unmapped code needs an explicit check mapping before incremental verification can succeed.

On macOS, use the hashed macOS dependency lock and native packaging tools:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-macos.lock
.venv/bin/python run.py
.venv/bin/python tools/run_ui_checks.py -- .venv/bin/python tools/verify_changes.py --full
.venv/bin/python tools/verify_macos_desktop.py --output artifacts/macos-native.json
.venv/bin/python tools/mac_build.py --output dist-macos/<version> --work build/macos-<version>
.venv/bin/python tools/mac_verify.py --product dist-macos/<version>/package --output artifacts/macos-installation --allow-ad-hoc
```

The default macOS UI runner uses Qt offscreen. Native AppKit and launchd checks are separate: offscreen success does not establish foreground tracking, Accessibility access or notification delivery. Tests use `CODEXON_SERVICE_TEST_ROOT` for unique launchd labels and `CODEXON_DATA_DIR` for private temporary storage. Set `CODEXON_RUN_LAUNCHD_TESTS=1` only for tests allowed to register and clean up their own QA jobs.

The macOS build produces independent app, recovery and installer bundles. Local builds use ad-hoc signatures; public downloads require Developer ID signing and notarization. Pass `--signing-identity` and an existing `--notary-profile` to `tools/mac_build.py`; never put signing keys or Apple credentials in source or command arguments. Use `--allow-dirty` only for local development. The macOS CI checks Apple Silicon and Intel separately. See [macOS](docs/macos.md) for installation and update behavior.

Packaging inspects every native Mach-O payload for its architecture and minimum OS. Local builds record the actual minimum; a Homebrew Python built for newer macOS cannot produce a macOS 13-compatible package merely by changing `Info.plist`. Public builds reject dependencies above the macOS 13 baseline. Use the pinned python.org runtime in CI for distribution, and retain the native payload report with the build evidence.

Read [verification](docs/verification.md) before modifying the proxy or publishing a build. Tests use isolated Codex homes and ports. Do not attach real Codex records, account files, databases, or unredacted diagnostics to issues or pull requests. For a vulnerability, use [private reporting](SECURITY.md).

The product build also freezes `CodexonRecovery.exe` with its own Python/Tcl/Tk runtime and no Qt dependency. Build Setup using Inno Setup 6.7.3 or compatible:

```powershell
.\tools\Build-Installer.ps1 -ProductDir dist-releases/<version>/Codexon -OutputDir dist-releases/<version>/setup -Compiler '<Inno Setup>/ISCC.exe'
```

Use `-Isolated` and a separate output directory for installation QA. The QA installer uses a separate app ID and never activates the production proxy. Validate the payload with `python tools/package_release.py --product <product-directory> --validate-only`. Publish `Codexon-Setup.exe` and `Codexon-Setup.exe.sha256`; the app updater uses the checksum automatically. Do not publish a portable app ZIP. Keep all bundled third-party notices, source offers and relinking instructions. Signing credentials are not stored in this repository.

## Usage collection

`CollectorService` is the sole owner of source collection and `UsageIndex`. The GUI and cache worker consume `CollectionClient` snapshots. Keep one IPC format and one collection path; retired worker readers, direct scanners and their compatibility branches must be removed with their callers. Move still-relevant tests onto the supported path instead of retaining unused production code for old tests. Add compatibility behavior only for an explicitly supported migration requirement.

## Releases

PRs are optional. Push changes directly to `main`; push CI checks the affected behavior. To release, set a new `VERSION` in `cachemonitor/version.py`, push it, then run:

```powershell
gh workflow run windows.yml --ref main -f publish=true
```

This runs the full source, package, installation and proxy checks, builds the production installer from that same verified package, and publishes the installer, checksums and third-party sources. The release tag is `v<VERSION>` and points to the commit selected when the workflow started. Existing tags are not overwritten. Running the workflow without `publish=true` performs full verification only.

Release publication depends on successful CI, rather than requiring CI before a direct push to `main`. Keep force pushes and branch deletion disabled. See [verification](docs/verification.md) for the checks and evidence.

For a public macOS release, configure repository variable `MACOS_DEVELOPER_TEAM_ID` and secrets `MACOS_DEVELOPER_ID_P12_BASE64`, `MACOS_DEVELOPER_ID_P12_PASSWORD`, `MACOS_NOTARY_KEY_BASE64`, `MACOS_NOTARY_KEY_ID` and `MACOS_NOTARY_ISSUER_ID`. Store credential values only in GitHub Actions secrets. The workflow imports signing material into a temporary keychain and removes it after packaging. Then run:

```sh
gh workflow run macos-release.yml --ref main -f publish=true
```

Both native architectures must pass source, Cocoa, signed package, installation, rollback, independent recovery and frozen UI checks. The workflow notarizes and staples the apps and DMG, publishes checksums and matching third-party sources, and rejects dirty builds or existing assets. It can attach Mac assets to an existing Windows release only when its tag points to the exact verified commit. A new Mac-only release does not replace the latest Windows release. macOS update discovery selects the newest compatible Mac release, even when a newer Windows-only release exists. Without `publish=true`, signing and verification still run, but nothing is published. Unsigned CI builds from `macos.yml` remain development artifacts.

## Graph axes

All numerical graph axes must automatically fit the finite values currently displayed, with readable padding. Do not force a zero origin or a fixed percentage/currency range. Recompute after filters or data changes; ignore missing values rather than treating them as zero, and handle empty or constant series without a degenerate range. Axis labels, marks and inspection coordinates must use the same bounds. Normalized 100% composition strips and progress indicators retain their semantic denominator; they are not absolute-value graph axes.
