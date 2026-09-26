# AI / LLM installation guide

**Choose the operating system first.** For macOS, follow [the macOS guide](docs/macos.md) ([한국어](docs/macos.ko.md)); use the native DMG, per-user app links and installation receipt. The Windows Setup, registry and PowerShell instructions below apply only to Windows. An explicitly authorized remote port/build/install task can use the macOS installer's documented noninteractive mode; do not wait for a second authorization the user has already provided.

This is the entry point for an AI assistant given the [Codexon repository](https://github.com/jisoq/Codexon) and a request to install or set up Codexon. **The user runs Setup; the AI verifies the installed application and its environment after the user reports completion.** Respond in the user's language.

Follow this conversation flow:

1. Read this guide, give the user the official release's Setup link, and ask them to run it and tell you when installation is finished.
2. **Stop and wait for the user's installation-complete message.** Do not download and execute Setup, launch it through a scheduled task, or substitute a portable ZIP or source build.
3. After that message, inspect installation, runtime, environment and settings yourself using the checks below. Report what is ready and any actual remaining issue.

Do not turn verification into a checklist for the user to perform. If your tools cannot access the target Windows desktop or inspect a required item, state that limitation and keep that item unverified. Never infer success from the user's completion message alone.

Codexon is a **Windows 10/11 x64 desktop companion for Codex**, not Codex itself. The per-user `Codexon-Setup.exe` includes Python, Qt and an independent offline recovery tool. No separate Python, Node.js, Git, API key or administrator access is needed for Codexon. Setup is the only supported end-user distribution. Source development is a separate path, used only when explicitly requested.

## 1. Ask the user to install, then wait

This guide assumes the public repository has a published stable Windows x64 release. Inspect `https://api.github.com/repos/jisoq/Codexon/releases/latest` or the [latest release page](https://github.com/jisoq/Codexon/releases/latest). Confirm that the stable release contains `Codexon-Setup.exe`, and link to that release's Setup asset or release page. If the asset is missing or inaccessible, report that issue instead of inventing a download link or falling back to another distribution.

Send a short message such as the following, translated into the user's language, with the verified link:

> Download **Codexon-Setup.exe** from this release and run it yourself. When installation finishes, tell me “installed.” I will then check the installation, execution environment and settings.

For a Korean-speaking user:

> 이 릴리즈에서 **Codexon-Setup.exe**를 받아 직접 설치해 주세요. 설치가 끝나면 “설치 완료”라고 알려주세요. 이후 설치 상태와 실행 환경·설정이 제대로 갖춰졌는지 제가 확인하겠습니다.

The user only needs to run Setup and report completion. Do not ask them to download `.sha256` files, verify hashes, run commands or collect diagnostic reports. End this response and wait; elapsed time or the presence of an installer file does not count as confirmation. If the user has already explicitly said installation is complete, proceed to section 2 without asking them to install again.

## 2. After confirmation, inspect installation and environment

Work in the target user's Windows desktop session. Check existing Codexon processes without terminating them. Setup may already have opened the dashboard; reuse it when it is the intended version. Verify the local environment before opening another GUI or changing settings.

Resolve the user's Codex home from the existing environment: `CODEX_HOME` if set, otherwise `$env:USERPROFILE\.codex`. The application also accepts repeated `--codex-home <absolute-path>` arguments for multiple local homes. Check that the installation's shortcuts and running app use the intended homes. Do not create a new empty home and present it as the user's existing data. Never replace `auth.json`, `config.toml`, or an existing account connection.

Read `HKCU\Software\Codexon` values `InstallRoot`, `AppPath`, and `RecoveryPath`. Verify the registration and executable paths from an ordinary desktop context independent of Codex; a successful registry read only inside Codex is insufficient because the views can differ. Use `AppPath` as `$exe`, its parent as `$appDir`, and `InstallRoot` as `$installDir`. Read `$appDir\build-manifest.json` as `$manifest`, and `$installDir\install-result.json` for the actual outcome, including pending connection work. Check that the registered files and Start-menu shortcuts exist and belong to that installation. Do not repair registration merely to mask a disagreement between contexts.

Check that the manifest identifies `Codexon`, architecture `x64`, executable `Codexon.exe`, the release the user installed, and a 40-character source commit. Verify the installed app and registered recovery executable against `sha256` and `recovery_sha256`. Preserve the bundled runtime, licenses, third-party notices and source offer. Keep verification details in local records; the user does not need to perform checksum steps. If a newer release appeared while the user was installing, distinguish it from a failed installation rather than silently installing it.

Setup installs under `%LOCALAPPDATA%\Programs\Codexon` by default, with separate version directories for the app and recovery tool. Existing settings, account files, indexes and observation history remain in their original locations. Installation does not enable optional monitoring or send model requests. If monitoring was already enabled, a connection update can remain queued until existing connections drain; report that state accurately.

Then complete sections 3 and 4. Check optional proxy status as described in section 5 even when it is off; do not enable it merely to pass verification. Section 6 is only for an explicit source-development request, and section 7 covers updates, recovery and the completion report.

## 3. Verify the runtime and actual interface

Use `$exe`, `$installDir`, and `$manifest` resolved from the confirmed installation in section 2. Use a unique local report directory. A frozen Windows GUI executable may not write to the terminal: wait for it with `Start-Process` and read its JSON report.

```powershell
$checkDir = Join-Path $installDir ('checks-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $checkDir | Out-Null
$runtimePath = Join-Path $checkDir 'runtime.json'
$runtimeProcess = Start-Process -FilePath $exe -ArgumentList @(
    '--verify-runtime', ('"{0}"' -f $runtimePath)
) -WindowStyle Hidden -Wait -PassThru
if ($runtimeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $runtimePath)) {
    throw 'Runtime verification failed; inspect the local report.'
}
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
if ($null -eq $runtime.errors -or @($runtime.errors).Count -ne 0 -or
    $runtime.version -ne $manifest.version) { throw 'Runtime report did not pass.' }
```

This verifies the bundled Python/Qt runtime without starting the GUI or services. Next, complete section 4 using your desktop interaction and screenshot tools: inspect the dashboard, open Settings, and verify the displayed version and controls. Runtime imports alone do not establish that the interface works.

## 4. Launch and make the application usable

1. Inspect the dashboard if Setup already opened it; otherwise open the Setup-created **Codexon** Start shortcut in the target user's desktop session. When launching the installed app programmatically from Codex, use an independent per-user scheduled task with the registered `$exe` and `$appDir`, preserving any custom `--codex-home <absolute-path>` arguments. Do not leave the persistent GUI attached to the Codex tool process. This is the interactive application the user requested, so show its window.
2. Check **Settings → About and troubleshooting** for the intended version. Only one normal GUI runs per Windows user: a second launch can activate the older GUI and exit. A successful process launch alone does not prove the new version opened. Close an old GUI through its tray Exit action when appropriate; do not kill the proxy or active Codex work.
3. Check that the dashboard reads the intended local sessions. With a new Codex account/home, an empty history is expected. Do not generate a model call just to populate it.
4. Inspect all settings sections, including language, login startup, overlay, widget, notifications, data collection, proxy, and About and troubleshooting. Verify saved choices against the actual behavior and the user's requested setup. Optional features being off is not an error, and “all settings verified” does not mean “all features enabled.” Preserve existing preferences unless the user requested a change. Use the UI for authorized adjustments rather than editing settings behind the running app; language changes apply on the next launch. For a requested overlay, verify its actual display while Codex is open.
5. Verify live usage limits if the existing Codex account is available. Codexon locates `codex.exe` on PATH, under `%LOCALAPPDATA%\OpenAI\Codex\bin\*`, or in the existing `%APPDATA%\npm\node_modules\@openai` installation. Live limits use its read-only `app-server` account RPC. Do not install Node.js or a second Codex CLI merely because Codexon's installer does not bundle `codex.exe`.
6. If Codex is missing, help install the official Codex application within the user's request and your available permissions; consult its current official installation instructions. If login is needed, let the user complete the interactive sign-in. Do not request passwords, account tokens, or exported `auth.json`. Existing history can still be analyzed without live limits.
7. Verify the Start shortcuts created by Setup, including **Codexon 연결 복구**, against the registered installation paths. Do not create duplicate shortcuts or replace the installer-managed launch path with a version-specific path. Preserve custom `--codex-home` arguments. Only for a source development setup, create a separate shortcut using the environment's `pythonw.exe` and absolute `run.py` path.

Report **installed and ready** only after verifying the real dashboard. Distinguish an application ready with local history from live limits awaiting login, an overlay not yet observable because Codex is closed, or optional proxy monitoring left off. Resolve everything your tools can verify; ask only for the remaining user interaction or essential choice.

The overlay uses the running Codex process and its current task route, without a version allowlist. Check one identifiable local task window; incomplete routes, multiple windows and remote tasks must not display another task's usage. Task detection sends no model requests. See the [user guide](docs/user-guide.md) for overlay behavior after Codex updates.

## 5. Optional request/response model monitoring

Ordinary installation does not require changing Codex's connection. If the user requests model mismatch monitoring, explain its concrete setup effects: the app backs up the prior `config.toml`, changes the Codex connection to a local proxy, and registers the independent managed proxy and scheduled recovery checks. The current activation flow also sends a **real model request** to test the connection; it can consume usage. Keep the installed folder while any proxy, updater or recovery component uses it.

Use **Settings → Proxy** for the activation workflow only when those effects are within the user's authorization. Do not enable it as an installation smoke test. In particular, neither `--enable-model-observer` nor `--test-model-observer` is a read-only installation check. If the request excludes model calls, leave activation pending and explain why.

After an authorized activation, restart Codex only when its current work can safely end. Never force-cancel a request, automatically resend one, or terminate Codex to finish setup. Confirm the proxy status, and verify comparison evidence from the user's next normal completed call. An active proxy does not prove a particular call was observed. Comparison uses recorded request/response model names; it does not expose hidden backend routing.

For a read-only status check, use the resolved home and a fresh report path:

```powershell
$statusPath = Join-Path $checkDir ('proxy-status-' + [guid]::NewGuid().ToString('N') + '.json')
$statusProcess = Start-Process -FilePath $exe -ArgumentList @(
    '--model-observer-status', '--control-report', ('"{0}"' -f $statusPath)
) -WindowStyle Hidden -Wait -PassThru
if ($statusProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $statusPath)) {
    throw 'Proxy status check failed.'
}
$status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
if ($status.error) { throw 'Proxy status reports an error; inspect it locally.' }
$status | Select-Object configured, running, phase, version_mismatch, restart_required
```

For a custom home, include `--codex-home` and its quoted path in this argument list. `configured = false` is normal when optional monitoring is off. For enabled monitoring, check `configured`, `running`, `phase`, and any `version_mismatch` or recovery state together. A required restart remains pending until Codex actually restarts. Keep complete reports private; they can contain local paths and identifiers. Do not probe the production proxy with test model traffic or use its port 8768 for development tests.

## 6. Source environment (development)

Use a fresh checkout and a dedicated virtual environment on Windows x64. The repository's build/CI baseline is **64-bit Python 3.12.10**. Git and that Python installation are source-path prerequisites only. Check existing tools first; install missing prerequisites from their official distribution within the requested setup scope. Do not silently substitute another Python minor version or loosen dependency pins when installation fails.

```powershell
$ErrorActionPreference = 'Stop'
$sourceDir = Join-Path $env:USERPROFILE ('source\Codexon-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Split-Path -Parent $sourceDir) -Force | Out-Null
git clone https://github.com/jisoq/Codexon.git $sourceDir
if ($LASTEXITCODE -ne 0) { throw 'Clone failed.' }
Set-Location -LiteralPath $sourceDir
py -3.12 -c "import platform, struct; assert platform.python_version() == '3.12.10' and struct.calcsize('P') == 8, 'Expected 64-bit Python 3.12.10'"
if ($LASTEXITCODE -ne 0) { throw 'Required Python build is unavailable.' }
py -3.12 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$python = Join-Path $sourceDir '.venv\Scripts\python.exe'
& $python -m pip install -r requirements-release.lock
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
& $python run.py --help
if ($LASTEXITCODE -ne 0) { throw 'Application entry point failed.' }
& $python tools/verify_changes.py --plan
if ($LASTEXITCODE -ne 0) { throw 'Verification planning failed.' }
& $python tools/verify_changes.py
if ($LASTEXITCODE -ne 0) { throw 'Repository verification failed; inspect artifacts/verification.' }
```

Use the environment's Python directly; activation and a machine-wide execution-policy change are unnecessary. A fresh checkout has no verification baseline, so the selector runs the full suite. Record `git rev-parse HEAD` with the result. For an existing checkout, read `AGENTS.md`, inspect local changes, and never reset or clean away the user's work.

Before the normal launch, check the runtime and real Qt Quick interactions with synthetic records:

```powershell
$sourceCheckDir = Join-Path $sourceDir ('artifacts\install-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sourceCheckDir | Out-Null
& $python run.py --verify-runtime (Join-Path $sourceCheckDir 'runtime.json')
if ($LASTEXITCODE -ne 0) { throw 'Source runtime verification failed.' }
& $python -c "import sys; from pathlib import Path; from tools.verify_changes import fixture_home; fixture_home(Path(sys.argv[1]))" $sourceCheckDir
if ($LASTEXITCODE -ne 0) { throw 'Synthetic fixture creation failed.' }
& $python run.py --codex-home (Join-Path $sourceCheckDir 'codex') --index-path (Join-Path $sourceCheckDir 'index.sqlite') --smoke (Join-Path $sourceCheckDir 'smoke.png') --smoke-depth core
if ($LASTEXITCODE -ne 0) { throw 'Isolated UI verification failed.' }
$sourceSmoke = Get-Content -LiteralPath (Join-Path $sourceCheckDir 'smoke.json') -Raw | ConvertFrom-Json
if ($null -eq $sourceSmoke.errors -or @($sourceSmoke.errors).Count -ne 0 -or
    $sourceSmoke.model_requests -ne 0 -or $sourceSmoke.live_quota_requests -ne 0 -or
    $sourceSmoke.session_rollup.descendants -lt 1) { throw 'Source UI report did not pass.' }
```

Inspect the generated PNGs. This check uses isolated settings, home, and index; it makes no model or live quota requests and does not manage the production proxy. Use the provided fixture helper: an empty directory alone does not supply the Codex databases expected by the smoke check. Then launch the interactive app with `& $python run.py` (or `pythonw.exe` for a persistent shortcut) and complete section 4 against the user's actual home.

Building a frozen executable is optional for source use. If requested, follow [CONTRIBUTING.md](CONTRIBUTING.md) and [verification](docs/verification.md), build from clean committed source into unique output/work folders, and run `tools/verify_changes.py --package-exe <built-Codexon.exe>`. That package check also uses synthetic parent/child sessions to check cost aggregation. Validate the installer payload with `tools/package_release.py --product <product-directory> --validate-only`. Do not publish a release merely to complete a local setup.

## 7. Updates, recovery, and completion report

Subsequent updates use **Settings → About and troubleshooting → Check and install updates**, the single update entry point for the app and proxy. Do not start an update as part of an installation check. For a requested migration from an existing legacy portable copy, ask the user to run Setup and wait for completion as in section 1, then verify the displayed app version and connection update status. Preserve the old directory while any GUI, proxy, updater, or rollback journal refers to it. Do not delete it just because the new dashboard opened. A new managed proxy runs its own lifecycle without a separate resident watcher; older proxies retain their existing supervisor until a safe update.

Verify that **Start → Codexon 연결 복구** opens the independent recovery tool, then close it without restoring settings as a test. It works without the dashboard, proxy or internet. While monitoring is enabled, scheduled checks run briefly at login and every minute; they notify only after repeated confirmed local faults and never reroute traffic or send model requests. Check their registration and configured paths when monitoring is enabled. A queued update can be cancelled through the same update control, but do not cancel it during inspection.

If a connection actually fails and recovery is within the user's request, use the independent Start-menu recovery tool. For automated explicit recovery, `CodexonRecovery.exe --restore --codex-home <home> --report <fresh-report.json>` uses the same implementation; `--status` only inspects. For a custom evidence directory or isolated port, also pass `--data-dir` and `--proxy-url`. Verify that configuration restoration succeeded; restart Codex only when safe. Report configuration restoration separately from actual Codex communication. Never delete the complete configuration or account home. Windows uninstall restores the managed route and defers removal while payload components are in use. See the [user guide](docs/user-guide.md) ([한국어](docs/user-guide.ko.md)).

Preserve `%LOCALAPPDATA%\CacheMonitor` (index and quota history), `%USERPROFILE%\.cachemonitor\model-observer` (model evidence, proxy state and configuration backups), the user's Codex homes, and existing Windows settings. The internal `CacheMonitor`/`cachemonitor` names are intentional compatibility paths; do not rename them to `Codexon` during installation.

Finish with a short result containing the installed version (or source commit), permanent launch path/shortcut, installation and runtime/UI results, and the verified state of local records, live limits, saved settings, overlay, optional model monitoring and recovery access. Clearly distinguish verified items, intentionally disabled features and items your tools could not verify. List only actual remaining blockers or user actions. Never report successful installation merely because files downloaded, a process started, or a command returned zero.
