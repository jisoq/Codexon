# User guide

## Local data and network use

Codexon reads local Codex session records and usage metadata. Its usage index stores the fields needed for tokens, cache, model, time, status, and session analysis. It does not need conversation text to calculate these views. The index still contains local paths and identifiers. The separate quota ledger and model-observation data retain observed history. The dashboard may display your project and task names.

When you enable the optional proxy, Codexon backs up the **entire prior** Codex `config.toml` in `%USERPROFILE%\.cachemonitor\model-observer\backups`. That backup can contain sensitive values from your configuration. The proxy relays Codex requests. Live limits are read through the installed Codex `app-server` account RPC; analysis of past usage is based on local records. Codexon is not an offline-only product.

## Session overlay and Codex updates

Keep Codexon running and enable **Settings → Session overlay**. The overlay identifies one Codex task window and reads that process's current route before matching it to local usage records. It appears while that Codex window or its overlay controls are active. Incomplete or ambiguous records, multiple task windows and remote tasks do not substitute another task's usage.

Codex version numbers are not an allowlist. Updates that retain the route format continue to work; a changed format may require compatibility work. Task detection does not send model requests. Enable **Settings → General → Starts when you log in to Windows** to start Codexon in the tray at login; reopening Codex alone does not launch a stopped Codexon.

## Average output speed

The dashboard's usage summary and call details show **average output speed** in `tok/s`; the call table offers it as an optional column. The overlay shows the latest call's speed beside cache rate and cost. Select the speed to inspect that call's duration in the dashboard.

Speed is output tokens, including reasoning, divided by the observed time from request to completed response. It includes waiting and network time, not just streaming. Only completed calls with matching proxy timing and valid output counts are measured; missing or conflicting evidence is not zero. The dashboard summary divides eligible output totals by those same calls' total duration and shows measured coverage. Parallel call durations are summed, so this is not overall wall-clock throughput. Existing records with valid timing can be used; no model request is sent to measure speed.

## Update

Use **Settings → About and troubleshooting → Check and install updates**. Codexon verifies the official installer, prepares a separate version directory and checks its runtime before switching launch paths. The same operation schedules the proxy update after existing connections finish, including idle WebSockets. No active request is force-cancelled or replayed. Finish work and close Codex when a connection update is waiting. A failed new proxy is rolled back; new connections may briefly fail during the switch. Previous payloads remain available for running components and rollback.

Users of legacy portable copies can migrate by running Setup once. If the previous GUI does not support automatic handoff, exit Codexon through its tray and open the installed version from Start. Existing settings and history remain in their original locations.


Explicit `--codex-home` selections are saved in `%LOCALAPPDATA%\CacheMonitor\launch.json`. Multiple homes and paths containing spaces are retained after updates and Start-menu launches, even with login startup disabled. Specify `--codex-home` again to change the selection.

## Remove

Use **Windows Settings → Apps → Installed apps → Codexon → Uninstall**. The uninstaller restores the managed connection setting first and defers deletion while app or connection components are in use. Exit Codexon and let existing connections finish before retrying. For an older portable copy, disable its proxy and login startup before removing its unused program folder.

Application data under `%LOCALAPPDATA%\CacheMonitor` is separate. `usage-index.sqlite` is a rebuildable index; `quota-cycles.sqlite` holds observed quota history. Model evidence, proxy state and configuration backups are under `%USERPROFILE%\.cachemonitor\model-observer`. Keep these folders if you might need past usage or a configuration rollback. Removing the executable does not remove this data.

## Recover direct connection without the GUI

Open **Codexon Connection Recovery** from Start and select **Restore direct connection**. The tool runs without the desktop app, Qt runtime, internet access or an AI response. **About and troubleshooting → Open Codex connection recovery** opens the same tool.

After configuration restoration, finish current work and fully restart Codex. Restoring a setting does not prove that model communication has resumed. Manual recovery stays available even when automatic diagnosis reports a responding proxy. Other server settings and authentication are preserved. An unreadable configuration is not overwritten with guessed values.

While proxy use is enabled, a Windows scheduled task checks local state briefly at login and every minute, including while the desktop app is closed. Two confirmed fault observations produce one notification that opens the same recovery tool. Uncertain delays do not trigger alerts or routing changes. Turning proxy use off removes scheduled checks. If recovery fails, preserve configuration and backups; never delete the whole `config.toml` or `.codex` folder.

## Safe bug reports

Report the Codexon, Windows, and Codex versions, display scale, steps to reproduce, and the expected and actual behavior. Use a synthetic session if a screenshot is needed. Do not upload your complete `.codex` directory, databases, backup `config.toml`, `--snapshot` output, or unredacted `--control-report`. Redact project names, task names, paths, IDs, and secrets from the minimum excerpt needed. For a vulnerability, use [GitHub private vulnerability reporting](https://github.com/jisoq/Codexon/security/advisories/new).
