# Storage and long-term retention

The retention requirement is to preserve all usage records and per-call detail.
Space optimization must not sample observations, round timestamps, discard old
calls, or replace detailed history with totals. The underlying Codex recordings
are external inputs, not files this application may clean up.

## Writers and ownership

| Data | Writer and trigger | Retention and growth |
| --- | --- | --- |
| Usage index: events, file offsets, metadata | Collector scans changed source files; normal poll interval is 3 seconds | One sanitized event per source position. Grows with recorded events. Replaced source content is reconciled rather than appended as another revision. |
| Imported usage archive | Explicit migration from retired sources | One preserved record per response. Required when the original source is no longer available. |
| Model evidence | Proxy records request lifecycle observations | Stores identifiers, model/tier fields and timings, not request/response bodies. Grows with requests and their observed lifecycle stages. |
| Quota observations and accounting | Quota service and analysis worker | Preserve every observation timestamp, account boundary, request cost and exclusion. Polling normally uses 15 seconds during activity and 60 seconds when idle, with a 5-second minimum after relevant changes. |
| Historical price calculations | Accounting updates when recorded inputs or price policy change | Preserve prior calculations and their policy identifiers. This is not a per-poll snapshot. Growth depends on calls and actual repricing. |
| Current task/request activity | Collector-to-accounting synchronization | One current row per task/request. The unused append-only `tracking_activity_versions` journal is retired. Its readers were absent from the application. |
| Collection transport database | Collector publishes changed sessions and activity | Compressed session and request payloads, plus one current snapshot. Durable data supports restart and upgrade recovery; live IPC avoids repeated full disk reads. |
| Pending quota writes | Storage failure recovery | At most 4,096 ordinary items and 8 MiB of encoded data, with 32 control reservations. Database ceiling is 16 MiB. A full backlog pauses new reads instead of losing history. |
| Quota/analysis diagnostics | Failures and slow cycles | Three files of up to 256 KiB per diagnostic stream. Slow-cycle records are limited to one per minute. |
| Runtime/control JSON and locks | Startup, shutdown, status, failure and recovery | Current state is replaced atomically. Heartbeat writes provide liveness, so a fixed file size does not mean zero I/O. Obsolete per-instance control files are small, but should be counted separately from usage history. |
| Preferences and connection backups | Explicit settings/connection changes | Current preferences replace prior state. Configuration backups remain recoverable and may contain sensitive settings. They are not log files eligible for arbitrary eviction. |
| Downloaded installers | User-approved application updates | Inspect at most 256 generated download directories before a new download. Retain one recent inactive copy plus the incoming installer. Running/referenced installers and unknown files are protected. Existing transfer limit remains 2,000,000,000 bytes per installer; protected exceptions can exceed the normal two-copy allowance. |
| Installed application/recovery payloads | Installation and service handoff | Keep the current payload and any payload still referenced by a process, task, startup entry, hook or pending transition. Existing readiness-based cleanup removes obsolete payloads. |
| Installer receipts/uninstall metadata | Installations | Grows with installations, not with calls. Uninstall metadata must remain intact for removal and rollback. |
| Setup staging | Native installer bootstrap | Successful completion removes the copied installer and helper. Small receipts remain; interrupted runs can leave a larger copy for investigation. |
| QML disk cache | Qt compiles UI files | Rebuildable cache, driven by UI/build versions rather than usage records. It is not evidence or historical usage. |
| Historical maintenance backups | Explicit recovery and diagnostic tools | Separate from ongoing application writers. Preserve their contents; lossless filesystem compression can reduce allocated space without changing paths or restore formats. |

Default database locations are resolved by `usage_paths.index_location`,
`quota_cycles.ledger_path`, and `model_evidence.default_path`. Saved overrides in
`launch_context.connection_paths` take precedence. Inventory actual resolved
paths, including packaged-app redirection, before maintaining files.

## Measurement on 2026-10-14

The audit covered the user's `.cachemonitor`, `LocalAppData/CacheMonitor`, and
installed `Programs/Codexon` directories. Source checkouts, build/test artifacts,
and Codex's own session recordings were excluded. File lengths totaled about
4.06 GiB before cleanup. Length and allocated disk space differ after filesystem
compression. One MiB is 1,048,576 bytes; one GiB is 1,073,741,824 bytes.

The quota database held approximately 1.01 million unused activity journal rows
for about 25,500 distinct requests and 3,800 tasks. On a consistent SQLite backup,
retiring that table and compacting changed the file from 470,773,760 to
243,355,648 bytes, a 48.3% reduction. SHA-256 fingerprints over ordered rows
confirmed that all 654,827 rows in the other 28 application tables were unchanged.
The SQLite integrity check also passed. This comparison includes reclaiming
fragmented/free pages and is not an estimate of one cycle's size.

Three historical backup areas contained 1,853,515,016 logical bytes. NTFS
compression reduced their reported stored size to 793,237,137 bytes. All 132
file content hashes remained identical. This was a one-time maintenance action,
not an automatic compression policy for live databases. Compression can add CPU
work when restoring backups; live query performance was not changed by it.

After applying the verified local build, the live quota file compacted from
471,293,952 to 243,924,992 bytes. Compaction and its integrity check completed in
1.97 seconds in this one run. Eleven obsolete installer-cache directories were
removed, reducing that cache from 1,117,782,455 to 182,911,387 bytes; two recent
copies remained. The three audited application directories then reported
2,135,339,385 stored bytes (about 1.99 GiB). Active collection can change these
point-in-time figures. A separate 120,966,191-byte recovery ZIP is retained in
the development workspace, outside the audited application directories.

A 30-second active-use observation sampled four live databases every 5 seconds.
All four changed transaction versions; only the collection database increased
its combined main/WAL file length, by 16 KiB. These are sampled transaction
changes and net file growth, not write-call counts or physical SSD writes.

## Capacity planning

Retaining every record cannot provide a constant total storage ceiling. Separate
usage-dependent history from bounded caches and per-update artifacts. Never
introduce a history TTL to satisfy a cache budget.

For a workload illustration, seven complete local dates, October 7-13, contained
29,184 index events and 37,332 model-evidence observations. The contemporary
databases used approximately 1,334 bytes per index event and 310 bytes per model
observation, including their current indexes and database overhead. Multiplying
each mean daily count by its bytes/row and 365 gives approximately 1.89 GiB/year
for the usage index and 0.56 GiB/year for model evidence. These are separate
scenario projections, not a total application forecast or a measured annual
growth rate. Index events ranged from 13 to 19,193 per day, so the workload is
highly uneven. Imports, version changes and fragmentation also affect these
averages. Quota history, transport snapshots and repricing history are additional.

The observed compact quota JSON averages approximately 855 bytes. Continuous
24-hour polling at 60-second idle intervals would add about 0.42 GiB/year of
JSON alone; at 15-second active intervals it would add about 1.67 GiB/year.
The 5-second minimum would reach about 5.02 GiB/year if sustained continuously.
These are cadence scenarios for one monitored stream, excluding table/index
overhead and other accounting records, not measurements of actual uptime.

Quota JSON now omits formatting whitespace while preserving all fields and
timestamps. In the observed data, compact serialization reduced encoded JSON
from 33,495,998 to 31,388,223 bytes. This is a payload measurement, not a promise
of equal filesystem savings; existing rows are not rewritten on every startup.

## Long-term alternative: lossless cold history

Monthly cold archives are a design alternative, not a currently implemented
retention or query policy. A suitable implementation would:

1. Keep recent detailed records online and move closed, immutable ranges into
   compressed SQLite archives. Preserve every row, identifier, timestamp, price
   policy and cross-boundary reference.
2. Verify row counts, content hashes and SQLite integrity before committing an
   archive manifest or removing the corresponding online rows. A failed or
   interrupted archive leaves its source usable.
3. Limit each uncompressed archive chunk to 256 MiB and the extracted read cache
   to two chunks (512 MiB). Process archives sequentially for all-history queries.
   These limits bound temporary work, not the user's retained history.
4. Load older detail on demand and retain a searchable catalog. Full-history
   totals must include archived records rather than silently restricting scope.
5. Report additional first-access/decompression latency separately from storage
   savings. Archive creation needs explicit low-space handling and a resumable
   checkpoint before it is suitable for background operation.

This preserves accuracy and record completeness while reducing cold-storage
cost. It requires coordinated changes to the index, evidence join, quota reader
and history queries; compressing or deleting one database independently is not
an implementation of that architecture.
