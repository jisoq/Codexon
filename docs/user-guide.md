# User guide

Amounts estimate subscription value using Standard base token rates and a 2.5x Fast usage multiplier, without API long-context surcharges. Rate tables and comparison choices use the app-supported model list; historical models retain their records and calculated values.

## Local data and network use

Codexon reads local Codex session records and usage metadata. Its usage index stores the fields needed for tokens, cache, model, time, status, and session analysis. It does not need conversation text to calculate these views. The index still contains local paths and identifiers. The separate quota ledger and model-observation data retain observed history. The dashboard may display your project and task names.

When you enable the optional proxy, Codexon backs up the **entire prior** Codex `config.toml` in `%USERPROFILE%\.cachemonitor\model-observer\backups`. That backup can contain sensitive values from your configuration. The proxy relays Codex requests. Live limits are read through the installed Codex `app-server` account RPC; analysis of past usage is based on local records. Codexon is not an offline-only product.

## Session overlay and Codex updates

Work totals include user-directed tasks and their subagents. Internal `codex-auto-review` approval calls are excluded from model choices, work call counts, costs and cache analysis; diagnostics in Settings show their separate count. Original records and the quota ledger are retained. Fully priced work shows only its call count; a priced/total ratio appears only when work calls have an unknown cost.

Keep Codexon running and enable **Settings → Display → Session overlay**. The overlay identifies one Codex task window and reads that process's current route before matching it to local usage records. It appears while that Codex window or its overlay controls are active. Incomplete or ambiguous records, multiple task windows and remote tasks do not substitute another task's usage.

Codex version numbers are not an allowlist. Updates that retain the route format continue to work; a changed format may require compatibility work. Task detection does not send model requests. Enable **Settings → General → Starts when you log in to Windows** to start Codexon in the tray at login; reopening Codex alone does not launch a stopped Codexon.

## Average output speed

The dashboard's usage summary and call details show **average output speed** in `tok/s`; the call table offers it as an optional column. The overlay shows the latest call's speed beside cache rate and cost. Select the speed to inspect that call's duration in the dashboard.

Speed is output tokens, including reasoning, divided by the observed time from request to completed response. It includes waiting and network time, not just streaming. Only completed calls with matching proxy timing and valid output counts are measured; missing or conflicting evidence is not zero. The dashboard summary divides eligible output totals by those same calls' total duration and shows measured coverage. Parallel call durations are summed, so this is not overall wall-clock throughput. Existing records with valid timing can be used; no model request is sent to measure speed.

## Performance trends

The **Performance trends** tab uses all collected work history, including archived sessions and subagents. Internal approval reviews and identified cache-maintenance calls are excluded. Choose one model to compare its Standard and Fast modes on the same time and value axes. Standard uses solid lines and circles; Fast uses dashed lines and triangles. **All**, **Standard** and **Fast** switch the visible modes. Every performance chart starts its value axis at zero and shares the selected time range.

With one model selected, charts use moving time windows. Zooming adjusts the common window width for both modes: a 24-hour view uses 60-minute windows, six hours uses 15 minutes, and one hour uses five minutes. The current width appears above the charts. The legend stays visible on the left, with chart guidance below it. Date selection and the timeline appear together. Edge windows use only records inside the visible range. A shaded band shows the central 50% of samples when at least 10 are available. These bands describe observations, not confidence intervals. Gaps in nearby eligible observations break the line and band. Views of one hour or less also show individual call observations; dense observations retain endpoints and extremes per screen column, while calculations use every eligible call.

The main line shows arithmetic mean cost and input/reasoning tokens, median call duration, equally weighted session cache rates, or time-weighted output speed. Median duration requires five eligible calls; its details also show the mean and P90 when available. Speed keeps the existing formula: total eligible output tokens divided by the total duration of those same calls. Its band describes individual call speeds. Cache rates first combine each session's eligible input and cached tokens inside each window and model/mode combination, then average the session rates equally. Call counts use non-overlapping interval totals without bands. Missing measurements are excluded rather than converted to zero. Header sample counts count each visible call or session once, even though moving windows overlap.

Hovering or clicking aligns a time guide across charts and updates the side panel. With one model selected, it compares both visible modes at the same calculated timestamp, including their representative values, means, medians and sample counts. The selected mode also shows the exact calculation interval, missing samples, distribution range and calculation totals. Floating tooltips are disabled. Reasoning-effort tabs share a value axis for comparison.

**Add model** adds other models, and **All models** restores the full selection. Multiple-model views retain calendar trends and the anomaly strip. **Daily**, **Weekly** and **Monthly** group records at local midnight, Monday, or the first day of the month. This unit stays fixed when zooming; duration uses its previous arithmetic mean in this view. Unselected records remain faintly visible. The side panel compares complete adjacent intervals and shows the central 80% for at least 10 samples. The anomaly strip compares up to 50 previous matching samples and requires at least 10. Deviations must exceed both 3.5 times the scaled median absolute deviation and 20% of the baseline median; cache rates instead require 10 percentage points. These are statistical differences, not confirmed causes or failures.

## Update

The installed app checks for new releases while it is running, including in the tray. Checks run every six hours with up to ten minutes of random delay. An initial or overdue check waits 15–45 seconds after launch. The next check time survives restarts. Source runs and installation QA do not check automatically.

When a new release is available, **Update available** appears above **Settings** and opens the update section. Codexon requests a system notification once per version, subject to the notification setting. The in-app button remains even if Windows hides the notification. Turn off **Settings → About → Check for updates automatically** to cancel automatic checks. Manual checks remain available, but respect server rate limits. Automatic checks only read release metadata; they do not download installers or inspect or restart the proxy.

Checking for updates does not immediately install or replace a connection. When a newer release is found, review the disconnection warning and finish active responses and server requests before choosing **Install**. Applying connection components also requires confirmation when the app is current. **Later** or closing the confirmation cancels the operation.

App updates can be checked and installed while the proxy is off. If no newer release is available, the updater says so; it also updates connection components that are enabled.

Use **Settings → About → Check for updates**. Codexon verifies the official installer, prepares a separate version directory and checks its runtime before switching launch paths. The same operation retires idle connections, waits for active responses and sent maintenance usage, then checks that the old process, listener and ownership locks are released. Completion requires three consecutive checks of the exact target version, deployment, role and readiness. No active request is force-cancelled or replayed. Failed startup restores the original role, command and autostart preference. Legacy workers without cooperative shutdown remain running with an explicit blocked reason. New connections may briefly fail during the switch. Previous payloads remain available for running components and rollback.

Users of legacy portable copies can migrate by running Setup once. If the previous GUI does not support automatic handoff, exit Codexon through its tray and open the installed version from Start. Existing settings and history remain in their original locations.


Explicit `--codex-home` selections are saved in `%LOCALAPPDATA%\CacheMonitor\launch.json`. Multiple homes and paths containing spaces are retained after updates and Start-menu launches, even with login startup disabled. Specify `--codex-home` again to change the selection.

## Remove

Use **Windows Settings → Apps → Installed apps → Codexon → Uninstall**. The uninstaller restores the managed connection setting first and defers deletion while app or connection components are in use. Exit Codexon and let existing connections finish before retrying. For an older portable copy, disable its proxy and login startup before removing its unused program folder.

Application data under `%LOCALAPPDATA%\CacheMonitor` is separate. `usage-index.sqlite` is a rebuildable index; `quota-cycles.sqlite` holds observed quota history. Model evidence, proxy state and configuration backups are under `%USERPROFILE%\.cachemonitor\model-observer`. Keep these folders if you might need past usage or a configuration rollback. Removing the executable does not remove this data.

## Recover direct connection without the GUI

Open **Codexon Connection Recovery** from Start and select **Restore direct connection**. The tool runs without the desktop app, Qt runtime, internet access or an AI response. **About and troubleshooting → Open Codex connection recovery** opens the same tool.

After configuration restoration, finish current work and fully restart Codex. Restoring a setting does not prove that model communication has resumed. Manual recovery stays available even when automatic diagnosis reports a responding proxy. Other server settings and authentication are preserved. An unreadable configuration is not overwritten with guessed values.

While the app is running, it checks local connection state every 15 seconds. Three matching confirmed faults observed at least 15 seconds apart produce one alert. Uncertain delays do not terminate connections. Normal app exit stops related services and leaves no independent periodic checker. After an app crash, reopen the app or use Start menu connection recovery. If recovery fails, preserve configuration and backups; never delete the whole `config.toml` or `.codex` folder.

## Safe bug reports

Report the Codexon, Windows, and Codex versions, display scale, steps to reproduce, and the expected and actual behavior. Use a synthetic session if a screenshot is needed. Do not upload your complete `.codex` directory, databases, backup `config.toml`, `--snapshot` output, or unredacted `--control-report`. Redact project names, task names, paths, IDs, and secrets from the minimum excerpt needed. For a vulnerability, use [GitHub private vulnerability reporting](https://github.com/jisoq/Codexon/security/advisories/new).
