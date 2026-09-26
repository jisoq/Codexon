# Codexon on macOS

[한국어](macos.ko.md) · [README](../README.md)

Codexon uses the same local records, calculations, request protocols and cache controls on Windows and macOS. The macOS desktop integration uses AppKit, Quartz and launchd. Packages target macOS 13 or later, with separate Apple Silicon (`arm64`) and Intel (`x86_64`) builds.

## Install and open

Use the matching DMG from a release that contains macOS assets. Open **Install Codexon** inside the image. It installs **Codexon.app** and **Codexon Recovery.app** in your user Applications folder without administrator privileges. Python, Qt and native bridge dependencies are included. Do not run the installed app from the mounted image or replace its version directories manually.

Public downloads require Developer ID signing and Apple notarization. A locally built package is explicitly marked as an ad-hoc test build, can be installed on the build Mac, and cannot use in-app online updates. Its minimum macOS version is calculated from the bundled Python and native libraries and recorded in the app and build report. Recent Homebrew Python builds can require macOS 26 or later. Public packaging fails if a native dependency exceeds the macOS 13 baseline. The build script supports signed, notarized packages when the developer's credentials are available. The presence of macOS source code does not mean the latest Windows release already includes a Mac download.

Launch **Codexon** from Applications. It reads existing Codex records and the existing Codex account connection. Closing the dashboard keeps the menu bar item running. Use **Quit** to stop the app and cooperatively finish its background work. **Settings → General → Start at login** registers a per-user LaunchAgent.

## Features and macOS behavior

| Feature | macOS behavior |
| --- | --- |
| Session, call, model and child-agent analysis | Shared calculation and collection implementation |
| Usage limits and reset history | Read-only installed Codex app-server RPC; Codex owns credentials, including Keychain credentials |
| Tray and taskbar widget | One native menu bar item with quota text and dashboard/menu actions |
| Current-session overlay | Nonactivating panel follows a confidently identified local Codex window |
| Manual session selection | Choose a session in the overlay menu or Settings to pin a standalone panel, including when Codex is hidden or remote |
| Displays and Retina | Logical point coordinates, display selection for the standalone panel, fallback to the primary display when disconnected |
| Spaces and full-screen windows | Panel uses native collection behavior; it does not change another app's windows or Spaces |
| Notifications | Native macOS notifications after explicit permission; in-app event history remains available without permission |
| Optional model monitoring | Shared HTTP, SSE, WebSocket and HTTP/2 relay with native process and port ownership checks |
| Optional cache management | Shared hooks, observation, operating consent, cancellation and usage accounting |
| Background collection and recovery checks | Per-user launchd jobs; the collector remains the sole owner of source collection |
| Installation and updates | Sealed version directories, atomic app links, runtime validation and rollback records |
| Independent recovery | AppKit application with its own runtime and no Qt dependency |

Automatic overlay selection fails closed when the task identity is missing or ambiguous. It never substitutes another session's usage. Select a session manually when automatic tracking is unavailable. A manually selected session is clearly marked as pinned. Menu bar positioning is controlled by macOS; Codexon does not inject a widget into another process.

## Permissions

**Settings → Session overlay → Open Accessibility settings** opens the relevant system pane. Accessibility access improves window-change observation and enables forwarding scroll input to the Codex window. Basic window discovery and the standalone panel work without requesting that access. Codexon does not request Screen Recording access to capture another app's pixels.

**Settings → Notifications → Allow notifications** requests macOS notification permission only after that button is pressed. A denied permission can be changed in System Settings. Source runs without an installed application identity keep in-app history instead of requesting native notification access.

## Codex connection and data

The app finds Codex on PATH, in common CLI locations and in installed Codex/ChatGPT app resources. A custom executable can be selected with `CODEXON_CODEX_PATH`. The default account home is `CODEX_HOME` or `~/.codex`; repeated `--codex-home` arguments support multiple local homes. Live quotas use the first home. Remote tasks without local records cannot be reconstructed from a window title.

Data is stored under `~/Library/Application Support/Codexon`. This includes the usage index, quota history, launch preferences, service receipts and `model-observer` data. UI preferences use `~/Library/Preferences/com.cachemonitor.CacheMonitor.plist`. Existing Windows storage paths are preserved on Windows. POSIX project paths keep their case, including on case-sensitive volumes.

Proxy monitoring and automatic cache maintenance remain optional. Turning monitoring on changes the managed Codex connection and makes one real verification call; it is not an installation test. Cache maintenance additionally requires its separate operating consent. See [the user guide](user-guide.md#cache-management) for limits and stopping conditions. Do not enable either feature solely to verify an installation.

## Updates, recovery and removal

Use **Settings → About and troubleshooting** for updates. Update discovery selects the newest compatible Mac release even when a newer Windows-only release exists. Downloaded macOS updates require the expected release checksum and the same Developer ID as the installed app. A failed preflight keeps the previous launch path. Active relay processes keep their current executable while replacement waits for a safe boundary. Cache hooks use the stable installed app path so their trusted command string survives updates.

Open **Codexon Recovery** from Applications when the dashboard or connection is unavailable. It can inspect and restore the app-managed connection without Qt or internet. It preserves unrelated configuration and account files. Restart Codex only after current work can end safely.

Use the recovery tool's removal action for a managed installation. It checks running components, removes owned service registrations and app links, and preserves usage records and account data. Removing only an Applications link does not perform managed cleanup. Retained version directories are kept while they may be needed for running processes or rollback.

## Development and verification

Follow [CONTRIBUTING](../CONTRIBUTING.md) for the pinned environment and build commands. `tools/mac_build.py` produces three app bundles, an architecture-specific DMG, a checksum and a build report. `--allow-dirty` is for local QA only; public builds require committed source.

`macos.yml` verifies development packages on both architectures. Public distribution uses `macos-release.yml` after signing and notarization credentials have been configured. It gates publication on source, installation, recovery and frozen UI checks, notarizes the apps and DMG, and publishes only when explicitly requested.

For an explicitly authorized automated installation, run the installer's executable with `--yes --source <folder-containing-both-apps> --report <new-report.json>`. Local ad-hoc builds additionally require `--allow-ad-hoc`. `--isolated-install --install-root <temporary-root> --applications <temporary-applications> --no-launch` performs installation QA. These options do not grant macOS permissions or enable proxy/cache features.

`tools/mac_verify.py` tests actual frozen bundles, installation/reinstallation, rollback, independent recovery and record-preserving removal. `tools/verify_macos_desktop.py` tests native panels and menu actions without changing another app. Offscreen Qt tests cover shared rendering and interactions. Actual visible Codex-window tracking, multiple physical monitors, notifications and log-in after a reboot require their respective OS conditions; report those checks separately from synthetic tests.
