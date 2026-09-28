# Contributing

Codexon is a Windows application built with Python 3.12.10 and PySide6/Qt Quick. Use a fresh clone and a dedicated environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-release.lock
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe tools/verify_changes.py --plan
.\.venv\Scripts\python.exe tools/run_ui_checks.py -- .\.venv\Scripts\python.exe tools/verify_changes.py
.\build.ps1 -DistPath dist-releases/<version> -WorkPath build/release-<version> -PythonPath .venv/Scripts/python.exe
```

The selector runs only checks related to changed behavior; small UI edits do not require backend or installation checks. Reuse existing coverage and add tests only for a material gap. Use `--full` for broad shared changes or release verification. Unmapped code needs an explicit check mapping before incremental verification can succeed.

Read [verification](docs/verification.md) before modifying the proxy or publishing a build. Tests use isolated Codex homes and ports. Do not attach real Codex records, account files, databases, or unredacted diagnostics to issues or pull requests. For a vulnerability, use [private reporting](SECURITY.md).

The product build also freezes `CodexonRecovery.exe` with its own Python/Tcl/Tk runtime and no Qt dependency. Build Setup using Inno Setup 6.7.3 or compatible:

```powershell
.\tools\Build-Installer.ps1 -ProductDir dist-releases/<version>/Codexon -OutputDir dist-releases/<version>/setup -Compiler '<Inno Setup>/ISCC.exe'
```

Use `-Isolated` and a separate output directory for installation QA. The QA installer uses a separate app ID and never activates the production proxy. Validate the payload with `python tools/package_release.py --product <product-directory> --validate-only`. Publish `Codexon-Setup.exe` and `Codexon-Setup.exe.sha256`; the app updater uses the checksum automatically. Do not publish a portable app ZIP. Keep all bundled third-party notices, source offers and relinking instructions. Signing credentials are not stored in this repository.

## Usage collection

`CollectorService` is the sole owner of source collection and `UsageIndex`. The GUI consumes read-only `CollectionClient` snapshots; the cache worker never starts or polls the collector. `AppServices` alone owns ordinary service startup, adoption, bounded restart, and shutdown. Quota services receive local quota observations from those same snapshots. Keep one IPC format and one collection path; retired worker readers, direct scanners and their compatibility branches must be removed with their callers. Move still-relevant tests onto the supported path instead of retaining unused production code for old tests. Add compatibility behavior only for an explicitly supported migration requirement.

## Releases

PRs are optional. Push changes directly to `main`; push CI checks the affected behavior. To release, set a new `VERSION` in `cachemonitor/version.py`, push it, then run:

```powershell
gh workflow run windows.yml --ref main -f publish=true
```

This runs the full source, package, installation and proxy checks, builds the production installer from that same verified package, and publishes the installer, checksums and third-party sources. The release tag is `v<VERSION>` and points to the commit selected when the workflow started. Existing tags are not overwritten. Running the workflow without `publish=true` performs full verification only.

Release publication depends on successful CI, rather than requiring CI before a direct push to `main`. Keep force pushes and branch deletion disabled. See [verification](docs/verification.md) for the checks and evidence.

## Graph axes

All numerical graph axes must automatically fit the finite values currently displayed, with readable padding. Do not force a zero origin or a fixed percentage/currency range. Recompute after filters or data changes; ignore missing values rather than treating them as zero, and handle empty or constant series without a degenerate range. Axis labels, marks and inspection coordinates must use the same bounds. Normalized 100% composition strips and progress indicators retain their semantic denominator; they are not absolute-value graph axes.

## Validation and operational evidence

Classify CI failures from logs as product defects, check defects, or workflow configuration defects. Reproduce and fix the affected execution path before rerunning CI; retries or longer timeouts alone do not establish a fix. In PowerShell, explicitly type conditional argument arrays before splatting, for example `[string[]]$checkArgs = if ($full) { @('--full') } else { @('--base', $base) }`. Verify both single-argument and multiple-argument branches for check planning and execution.

Keep operational notes general and reproducible. Use placeholders and isolated synthetic data rather than personal paths, account identifiers, credentials, conversation content, process identifiers, or raw production logs.

Use the installation, migration, recovery, and native Windows checks in [verification](docs/verification.md). Validate the actual previous artifact's format and runtime IPC paths rather than inferring them from its version. Do not skip supported formats or weaken migration criteria because paths differ. Distinguish the new release being changed from previous releases used only for comparison; cross-machine migration must be handled by the product and installer, not by manual repairs on one computer.

Recovery, installation, and GUI startup must use the same saved connection selection. Preserve custom homes, ports, observation-only roles, explicit disabled states, custom launch arguments, and valid hooks. Validate both ordinary restart and restart after processes disappear. Check independent recovery without the main application, Qt, or network execution modules present.

Do not infer native Windows installation or startup state solely from HKCU or AppData observed inside an MSIX application. Corroborate it through a read-only native Windows scheduled task and installation completion records; elevation inside the same application is insufficient. Check both task exit status and output, then remove diagnostic task registrations. For local replacement and downloaded updates alike, compare installation registration, completion records, shared pointers, startup entries, shortcuts, scheduled tasks, and hook paths before declaring migration complete.

Connection refusal is not sufficient evidence of a remote outage. Correlate actual transport destination and timestamp with listeners; quoted errors in conversation or diagnostic output are not transport failures. Configuration restoration, local service readiness, and reconnection of already-open conversations are separate success criteria.

After verifying the new installation and connection handoff, use the installer's cleanup path for unused previous installations. Confirm cleanup results and deferred retries for locked files. Apply the same process to local builds and downloaded updates, and avoid accumulating temporary build, packaging, and test artifacts.

## Release notes

Follow the language, structure, and tone of existing public release notes. Describe concrete user-visible capabilities and changed behavior, grouping related changes into one experience instead of listing commits or files. Include internal work only when it directly affects users, and claim only effects and measurements verified in the shipped release.

Give each item a short, specific title and explanation. State compatibility changes, discontinued support, known limitations, and required update actions clearly. Preserve the existing installation and reference-link format while checking that all versions and links match the release.
