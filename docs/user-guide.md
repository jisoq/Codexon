# User guide

## Local data and network use

Codexon reads local Codex session records and usage metadata. Its usage index stores the fields needed for tokens, cache, model, time, status, and session analysis. It does not need conversation text to calculate these views. The index still contains local paths and identifiers. The separate quota ledger and model-observation data retain observed history. The dashboard may display your project and task names.

When you enable the optional proxy, Codexon backs up the **entire prior** Codex `config.toml` in `%USERPROFILE%\.cachemonitor\model-observer\backups`. That backup can contain sensitive values from your configuration. The proxy relays Codex requests. Live limits are read through the installed Codex `app-server` account RPC; analysis of past usage is based on local records. Codexon is not an offline-only product.

## Session overlay and Codex updates

Work totals include user-directed tasks and their subagents. Internal `codex-auto-review` approval calls are excluded from model choices, work call counts, costs and cache analysis; diagnostics in Settings show their separate count. Original records and the quota ledger are retained. Fully priced work shows only its call count; a priced/total ratio appears only when work calls have an unknown cost.

Keep Codexon running and enable **Settings → Session overlay**. The overlay identifies one Codex task window and reads that process's current route before matching it to local usage records. It appears while that Codex window or its overlay controls are active. Incomplete or ambiguous records, multiple task windows and remote tasks do not substitute another task's usage.

Codex version numbers are not an allowlist. Updates that retain the route format continue to work; a changed format may require compatibility work. Task detection does not send model requests. Enable **Settings → General → Starts when you log in to Windows** to start Codexon in the tray at login; reopening Codex alone does not launch a stopped Codexon.

## Average output speed

The dashboard's usage summary and call details show **average output speed** in `tok/s`; the call table offers it as an optional column. The overlay shows the latest call's speed beside cache rate and cost. Select the speed to inspect that call's duration in the dashboard.

Speed is output tokens, including reasoning, divided by the observed time from request to completed response. It includes waiting and network time, not just streaming. Only completed calls with matching proxy timing and valid output counts are measured; missing or conflicting evidence is not zero. The dashboard summary divides eligible output totals by those same calls' total duration and shows measured coverage. Parallel call durations are summed, so this is not overall wall-clock throughput. Existing records with valid timing can be used; no model request is sent to measure speed.

## Update

Checking for updates does not immediately install or replace a connection. When a newer release is found, review the disconnection warning and finish active responses and server requests before choosing **Install**. Applying connection components also requires confirmation when the app is current. **Later** or closing the confirmation cancels the operation.

App updates can be checked and installed while the proxy is off. If no newer release is available, the updater says so; it also updates connection components that are enabled.

Use **Settings → About and troubleshooting → Check and install updates**. Codexon verifies the official installer, prepares a separate version directory and checks its runtime before switching launch paths. The same operation retires idle connections, waits for active responses and sent maintenance usage, then checks that the old process, listener and ownership locks are released. Completion requires three consecutive checks of the exact target version, deployment, role and readiness. No active request is force-cancelled or replayed. Failed startup restores the original role, command and autostart preference. Legacy workers without cooperative shutdown remain running with an explicit blocked reason. New connections may briefly fail during the switch. Previous payloads remain available for running components and rollback.

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

## Cache management

Settings → Cache management contains one master switch. Enabling it reveals the Cache management dashboard tab, where automatic maintenance and model-change confirmation can be controlled independently. Disabling it hides the tab, stops both features and revokes operating permission, while preserving individual choices, records and normal usage observation. Responses and usage for already sent requests are still collected. Re-enabling never renews an allowance. Model-change confirmation appears only when comparable evidence indicates a substantial increase in input processing cost, not for every model change.

The tab separates current status, independent requests and observed cost, maintenance decisions, connection health and recent activity. Expand cost evidence and diagnostic history for technical details. Missing writes remain unknown. Changes are observations, not proven causes; diagnostic success is not evidence of savings.

Passive source observation uses `--cache-observe-only` with a separate `--observation-index`, evidence database and observe-only hooks. It forwards normal user requests and keeps eligible request context only in bounded memory; it cannot send maintenance requests or approve operation, even if automatic maintenance was previously enabled. Run this observer through the Windows `CacheObservation` scheduled task, outside the Codex app's process tree, and check its matching-home health before changing the connection URL. A child process launched directly from a Codex task is not a durable service. Finish work and restart Codex after changing its URL; closing Codex must not stop the independent observer. Source observation requires its Python environment and source directory to remain available. Restoring direct connection preserves observation history; it requires another Codex restart to apply to existing connections.

Observation does not require choosing HTTP or changing models. It prices the current model and context separately from permission to execute maintenance. The operating grant restricts the exact account, home, model, effort and Standard tier; an estimate does not authorize execution. For the exact ChatGPT Codex endpoint, independent maintenance uses HTTP even if the original conversation uses WebSocket. A complete reconstructed context is required; missing or ambiguous response chains are not usable. Original conversation linkage, model, effort, instructions and tool definitions remain unchanged. Wire conversion verified with an isolated server is not proof of actual cross-transport cache reuse or net savings. An eligible natural response can supply a cost scenario without maintenance or return history; missing return history can still prevent execution. There is no fixed waiting period and collecting history never grants permission.

Maintenance and model-change confirmation default to off. Connect Codex hooks explicitly, trust the exact command in Codex, and start a new task. Registration preserves other hooks and backs up the old hook file. The confirmation belongs to one live session/turn/model invocation: approve releases it once; cancel, close and 60-second timeout block it. Missing UI bypasses confirmation. A lost UI connection passes only after checking for cancellation; an unreadable decision store after presentation blocks that invocation rather than reversing a possible cancellation. No instructions are added to conversation context. This guards the next request only, not the model picker, and requires the execution path to run the trusted hook.

Automatic maintenance additionally needs the managed proxy. Only when enabled, bounded request contexts and headers are kept in memory, never on disk. Independent requests preserve original tools, model, effort and conversation linkage; tool_choice=none disables tool execution. User ingress invalidates reservations before upload. Sent independent requests drain within their timeout for usage accounting and never interrupt the user request. Uncertain delivery is never retried. Restart and suspend discard schedules and await fresh work.

The policy uses up to 60 days of same-session submission gaps, including non-returns, with chronological training and forward checking. Submissions are not verified human returns. Independent natural samples and known input/read/output/pricing can bootstrap it without prior maintenance. No positive forward estimate selects no maintenance. Unknown write composition is bounded with pricing scenarios, not filled into usage records. Estimated benefit is capped by the currently preservable context. Maintenance cost includes the whole capped output, including reasoning, and is an upper scenario conditional on reuse of the previous read amount; it is not a guaranteed bill or subscription-quota formula. Actual missing or excessive cost stops further rounds.

Only the continuous regime since the latest observed model, effort, service-tier, compaction or context-size reduction is compared. Earlier incompatible regimes cannot stall a later valid regime. A gap ending in a known transition remains in its source regime with zero benefit and nonzero maintenance cost. Zero reads are zero benefit, not missing data. Non-returns, submissions without usage, and unknown essential costs within the current regime remain included. Missing values alone never create a new regime or erase unfavorable history.

The 30-minute design lifetime is anchored to request start, reserving a 30-second transport deadline and one second of scheduler slack. Completion time does not renew the deadline. Each round starts from the original snapshot, never the prior ACK. Only a lower bound on reused original-input tokens is recorded. Partial hits do not prove full-context renewal.

Execution with an output cap requires verified provider support. The tested ChatGPT Codex HTTP request rejected max_output_tokens; independent HTTP maintenance in the limited operating mode omits it. This does not establish support or non-support on untested transports. An ACK instruction is not a token bound, and no paid probe is performed automatically.

Enabling the setting or collecting more history cannot unlock ChatGPT execution. Its limited operating mode requires explicit consent to the displayed account, home, model, effort, period, total call allowance and observed-cost stop. Observation alone creates no consent or allowance. Natural output and reasoning usage can supply an initial cost scenario without prior maintenance; it does not bound the next response. Neither mock responses nor successful maintenance prove long-term cache benefit or positive net savings.

Call-count and observed-spend budgets are risk controls, not guaranteed cost caps for an in-flight request. One response may exceed the observed-cost stop; it prevents subsequent calls. Transport timeouts and disconnects do not establish server-side cancellation or billing cessation. This mode is never an automatic fallback. Unknown usage, errors and abnormal cost stop later calls across restarts. Public API background cancellation changes storage/linkage conditions and has not been established on this ChatGPT path. Neither generate:false nor prewarm is treated as proven maintenance. See the public API [reasoning limits](https://developers.openai.com/api/docs/guides/reasoning) and [background cancellation](https://developers.openai.com/api/docs/guides/background).

Disabling maintenance, repeated invalidation and orderly shutdown cancel future scheduling while draining an already-sent response until its original deadline. Natural requests proceed independently. Recovered usage is counted once; disconnected, timed-out or forcibly terminated work remains unknown and is never replayed.

The current context's valid cost estimate stays visible across automatic maintenance, reservation, completion and stopping. A new context or expiry requires a new estimate. A model-change submission blocked by cancellation, dismissal or timeout is neither a natural return nor missing model usage. The idle period and cost exposure across the cancellation are retained; ambiguous transmission and missing usage still remain unknown. Managed relays and standalone cache workers share lifetime ownership of the same journal before recovering interrupted requests.

Maintenance is a separate child session included once in overall usage, costs, parent rollups and quota accounting. Lost responses stay unknown. Later user reads are observed separately from causal savings. API-equivalent scenarios are neither billing nor measured quota savings. Auxiliary analysis shows recorded compaction input changes and subagent usage; quality loss, rereading and result integration require separate assessment. Neither compaction settings nor user model choices are changed automatically.

The source `--cache-worker` continuously analyzes natural requests, while `--cache-observe-only` remains unable to send independent requests. A separately authorized diagnostic uses the same executor, pre-upload permit and usage ledger without requiring profitable return history. It retains the original model, effort, context and tool definitions, with tools disabled. Before sending, observed cost plus the refreshed estimate must fit the explicitly authorized stop criterion. Diagnostic usage is included in totals but excluded from natural returns and maintenance effects. `tools/cache_runtime.py` manages one-time, nonrenewing 60-minute / two-call diagnostic grants and explicit termination. No failed or ambiguous request is retried.

For a source worker, start the GUI with matching `--index-path`, `--evidence-path`, and `--cache-control`. Run the worker independently through Windows Task Scheduler. Apply the new URL only to new connections and preserve ongoing conversations. Existing settings and history remain intact. Ending a diagnostic grant leaves passive collection and policy analysis running.

**Quit** stops issuing new cache requests, waits for active responses, sent maintenance usage and record writes, then closes the worker, collector and analysis process. The exit progress window remains visible while waiting. Consent and remaining allowances are preserved; the next app launch resumes the same role. Closing to the tray keeps the app running. App updates and language restarts hand over services. Quit restores the app-owned route to direct access; an already running Codex client that cached the previous address needs a restart to adopt that route.

After the new app and required background components are ready and previous processes have exited, Codexon removes old application and recovery folders identified by installation history, along with obsolete owned scheduled tasks. Items still in use or referenced by existing hooks are retained and checked again on the next app launch. Hook trust approval is not bypassed. Downloaded Setup files and the downloads folder are never deleted automatically.


Choosing **Exit** checks open proxy connections, including idle connections, and lists matching session names. **Safe exit** waits for responses; **Force exit** interrupts connections and cache requests, then saves records and stops services. You can switch to force exit while waiting. Unrecovered cache usage remains unknown. Older connection workers need an update to support force exit. Closing the dashboard with X still hides it to the tray.
