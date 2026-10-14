# Storage and long-term retention

Existing usage records and per-call detail must remain intact. Lossless storage
changes must preserve every recorded field and timestamp. The current resource
audit also permits explicitly budgeted changes to future collection cadence or
precision, after measuring their accounting impact. That permission does not
authorize deleting or sampling previously stored observations. The underlying
Codex recordings are external inputs, not files this application may clean up.

The broader CPU, memory, I/O and network investigation is tracked in
[the resource audit](resource-audit.md). Its remaining acceptance gates are
explicit; the earlier storage cleanup is not completion of that investigation.

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

## Retained event storage

The usage index stores source paths and session owners once and references
their identifiers from each event. Eligible JSON payloads use lossless zlib
compression when it saves at least 10%; other payloads retain their original
content. Decoding preserves the exact JSON text and limits expanded payloads
to 2 MiB. Ordering, source-position replacement and quota import remain intact.

Existing events convert in small transactions while both formats remain
readable. The old rows disappear only when their replacements commit. A
restart resumes conversion, and low disk space or a pinned journal reader
defers optional maintenance. Audit and repair readers can open either format
read-only. Earlier application binaries cannot open the converted schema.

Conversion makes freed pages reusable but does not automatically shrink the
database file. The measured 66.1% full-index file reduction includes a separate
VACUUM on an isolated copy. Conversion and compaction both write data and need
temporary space; their costs are listed in [the resource audit](resource-audit.md).
These source changes have not replaced the installed executable or production index.

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

For a workload illustration before the event-format change, seven complete
local dates, October 7-13, contained
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

The earlier compact-JSON change reduced encoded payloads from 33,495,998 to
31,388,223 bytes on its input. New observations now additionally share compressed
account/window/credit metadata and refer to matching requested/received columns.
All fields, exact timestamps and numeric types remain recoverable. Legacy JSON
is still read directly; small or oversized payloads remain in that format when
normalization would not save space or would exceed the 256 KiB encoding budget.
Template reuse is limited to 64 entries within a 1 MiB accounted cache budget.

For the same 1,000 actual observations, integrated ledger growth falls from
2,109,440 to 987,136 bytes. Actual file writes including the final checkpoint
fall from 75,184,504 to 71,707,064 bytes, a separate 4.6% reduction. CPU and elapsed
ranges overlap across two samples per version. Complete field hashes and
termination tests preserve the original data and committed new observations.
Including durable backlog receipt and acknowledgment, writes fall from
97,681,784 to 94,204,344 bytes per 1,000 observations in alternating repeats.
Short windows can cost more while introducing their first shared context.
The format-only five-store comparison writes 478,016 / 486,256 bytes idle and
2,365,040 / 2,373,280 bytes active with raw / shared quota payloads. Its final
checkpoints add 131,072 / 139,264 and 462,848 / 471,040 bytes respectively.
Each pair starts with one new context. The additional 16,432 bytes, including
checkpoint, belong entirely to that context's first table and index pages.
The active fixture preserves source modification times so initial indexing order
and page layout match. These are controlled writer measurements; full application
I/O includes other processes, IPC and network activity.

The actual median-day cohort also contains 302 local observations, 355 imported
history observations and 68 archived corrections, beyond its 725 live polls.
Replaying those populations reduces ledger growth from 2,138,112 to 1,191,936
bytes, preserving all fields. Combined with the measured request/attempt
coefficients, that usage component projects 2,940,412 bytes/day or about
1.07 GB/year. This is a scenario using the same workload every day, not a
measured daily growth rate or a complete installation footprint. Actual rate or
calculation-policy changes can revisit all retained calls and must be budgeted separately.
See [resource audit](resource-audit.md) for populations, the heavy-use case,
three-year projections and excluded owners.

Price verification dates and display metadata now preserve the existing
calculation when rates, multipliers, aliases and policy are unchanged. The full
new price snapshot is still recorded, and each call retains its original price
identifier. No previous calculation or observation is deleted. In the controlled
10,000-call case, a verification-only update adds 8,192 bytes instead of
2,924,544 bytes and creates no per-call archive versions. Actual calculation
changes retain the original repricing and archival behavior. Missing or
incomplete historical snapshots also reprice conservatively. Equivalence
lookups are bounded to 64 entries and 64 KiB of accounted storage per ledger.

Existing rows now convert in resumable transactions at initialization and after
collection synchronization. Each pass scans at most 512 records, prepares at
most 1 MiB of accounted row data and uses a 25 ms preparation deadline. It
yields under low space, writer contention or a pinned oversized WAL. Oversized,
invalid or unprofitable legacy payloads remain intact. Equivalent row replacement
preserves sequence numbers and does not invalidate historical report caches.

On the full fixed quota copy, conversion releases 35,471,360 bytes of reusable
database pages without removing any record. It takes 2.465 seconds across 137
batches in the exclusive measurement and writes 296,489,400 bytes once. The
longest batch is 33.97 ms. Termination at four transaction boundaries preserves
all committed records, keeps report revisions unchanged and resumes successfully.

The main file stays approximately 246.7 MB while SQLite can reuse its freed
pages. Separate offline VACUUM reduces the copy to 208,510,976 bytes, but writes
another 627,041,424 bytes. That filesystem-shrink operation is not automatic.
Older binaries cannot decode the new BLOB payloads, so coordinated upgrade and
rollback must respect the activation boundary. Activation rollback happens
before the replacement GUI opens these databases. A real installed-binary to
current-source GUI handoff preserves all records on a small two-home fixture;
final frozen-package and managed-service checks remain. Retired usage imports
now accept both partially and fully converted event stores without changing
their existing events. An arbitrary older binary cannot read the converted
stores. The installed executable and actual history remain unchanged. The
cadence estimates above describe legacy raw JSON, not the new total growth.

## Long-term alternative: lossless cold history

Whole-file cold archives were evaluated on a fixed 246,681,600-byte quota copy.
Zlib level 1 produced 58,471,870 bytes and restored the exact original file in
0.607 seconds. Restoration wrote 246,681,600 temporary bytes before a detail
query could run. An already available indexed query took about 0.005 seconds.
These are controlled single runs, not cold-disk or whole-application timings.

Correcting one old request in an already extracted chunk took 24,680 write
bytes for the database edit. Preserving the old archive, staging its replacement,
recompressing, validating and publishing it raised total writes to 305,179,694
bytes and elapsed time to 2.322 seconds. Killing an unpublished compressor left
the old catalog unchanged; the truncated candidate was rejected. All experiments
used private copies, and no production records were archived or removed.

Decision: do not enable whole-file or monthly archives as the runtime policy.
Current reports still replay all history, so compression alone adds restoration
and rewrite work without fixing that CPU cost. The next design must read closed
detail on demand and keep full-history totals correct. Independently readable
chunks, late corrections, cross-period references and all participating readers
must be evaluated together before selecting an archive format.

The trial kept chunks below 256 MiB and two extracted copies below 512 MiB,
but correction staging raised decoded/staged storage to 740,110,336 bytes.
Archives, candidates and small receipts brought the complete trial to
913,756,385 bytes. A read-cache limit therefore does not bound publication space.
Total preserved history can keep growing; active memory and temporary work
need separate limits. See [resource audit](resource-audit.md) for measurements.
