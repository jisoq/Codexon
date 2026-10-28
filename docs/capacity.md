# Capacity policy for usage data

A growing source catalog previously made a selected overlay fail before its
session could be prepared. The catalog header had a 1 MiB admission limit, and
later stages also required complete populations to fit fixed memory allowances.
Raising the first number would leave the later refusals intact.

The policy separates retained history, bounded work, optional caches, and
product semantics. Existing usage records, exact accounting, and source detail
remain authoritative. Cache pressure does not authorize history deletion or a
history expiry policy. [Storage and long-term retention](storage.md) describes
the data owners.

## Removed and reshaped admissions

The capacity review classified 435 records, including duplicate consumers,
work sizes, and display policies. These are not 435 independent hard caps or an
inventory of every operating-system and dependency limit. The following table
groups the changes by their owning stage.

| Stage and owner | Previous restriction | Current responsibility |
| --- | --- | --- |
| HTTP observation, [proxy_observation.py](../cachemonitor/proxy_observation.py) | 128 MiB cumulative decoded HTTP response and a 16-byte format prefix. | Continue across SSE events without a response-lifetime byte total. Recognize whitespace, BOM, and format markers incrementally. Per-event allocation guards remain. |
| Replay association, [model_proxy.py](../cachemonitor/model_proxy.py) and [proxy_websocket.py](../cachemonitor/proxy_websocket.py) | Evict completed response records and IDs after 2,048 entries. | Retain exact replay knowledge for the relay lifetime. Release it when the relay ends. A continuously busy relay has no proven finite response-count ceiling. |
| Proxy admission and native parsing, [proxy_http.py](../cachemonitor/proxy_http.py) and [native_backend.py](../cachemonitor/native_backend.py) | Duplicate connector, keepalive, and native input checks. Numeric cache capacity for a zero-argument loader. | Keep admission with the existing connection and parser owners. Explicit connector zero values prevent a library default from reintroducing another gate. Memoize the loader's single possible key without an eviction number. |
| Route selection, [overlay_tracking.py](../cachemonitor/overlay_tracking.py) | Exactly 2 MiB lines had different outcomes when the newline arrived in another read. | Apply the same line boundary across read alignment. The 2 MiB line limit and preceding-day lookback remain provisional policies. |
| Source discovery, [index.py](../cachemonitor/index.py) | A 2 MiB first-header probe could omit a descendant while reporting complete discovery. | Project identity and lineage incrementally. An unfinished header remains pending. A truncated probe cannot prove absence. |
| Collection metadata, [usage_collection.py](../cachemonitor/usage_collection.py) and [collection_stream.py](../cachemonitor/collection_stream.py) | Whole-catalog 1 MiB header gate and durable status's 4 MiB compressed and 8 MiB plain header gates. | Page membership with a publication-bound cursor. Stream only status fields for status readers. Oversized optional 16 KiB proofs become unavailable without refusing the canonical body. |
| Prepared storage, [prepared_store.py](../cachemonitor/prepared_store.py) | 1 GiB prepared-file lifetime admission, 32 MiB complete catalog admission, and redundant 16 KiB recovered-parent admission. | Store membership and conversion work in file-backed SQLite tables. Continue in bounded pages. Preserve source generations, lineage, and exact order. |
| Query owners, [prepared_analysis.py](../cachemonitor/prepared_analysis.py) | The complete owner population had to fit 64 MiB. | Use 64 MiB as an optional metadata cache. Reload evicted entries from the pinned prepared generation. |
| Quota synchronization, [prepared_ledger.py](../cachemonitor/prepared_ledger.py) and [quota_prepared_sync.py](../cachemonitor/quota_prepared_sync.py) | 64 MiB complete owner admission and 32 MiB cumulative certificate admission. | Stage population and coverage plans in file-backed temporary tables. Apply bounded cohorts under `loading=1`. Commit facts, request boundaries, and certificates atomically. Publish complete coverage after reconciliation. Durable exact-owner obligations survive shutdown and are acknowledged only after source-certificate confirmation. |
| Quota recovery, [quota_backlog.py](../cachemonitor/quota_backlog.py) | Reject an individual observation above 64 KiB. A nearly full backlog could strand an admitted in-flight response. | Treat 64 KiB as a fetch reservation estimate. Preserve one admitted in-flight response through the existing unsaved slot. Pause new reads at backlog capacity and resume after replay. |
| Comparison, [comparison_usage.py](../cachemonitor/comparison_usage.py) | An optional 32 MiB aggregate cache could refuse an otherwise valid query. | Compute the exact result without retaining that cache when it does not fit. Retain actual working and required-result guards. |
| Independent cleanup | Duplicate 5,000-row evidence fetch default, unused 24-call arithmetic, duplicate ten-error dashboard buffer, and duplicate checkpoint pre-hash admission. | Keep the poll work owner, visible Trend policy, `ErrorState`, and source-packet admission. Removing duplicate validation does not enlarge the source-packet contract. |

## Retained resource and work defaults

Most retained exact defaults lack calibrated numeric proof for current workloads.
Their resource ownership is supported by the implementation. That does not prove
that the number is optimal or sufficient for every supported workload.

Object-size accounting is not process resident memory, or RSS. Encoded bytes,
compressed bytes, row weights, item counts, SQLite cache targets, and object
estimates measure different things. They cannot be added into a process-wide
memory guarantee. A test with a reduced threshold proves boundary behavior, not
the adequacy of the production default.

| Family and source owner | Retained defaults | Reason and limitation |
| --- | --- | --- |
| Observation and WebSocket messages, [proxy_observation.py](../cachemonitor/proxy_observation.py) and [model_proxy.py](../cachemonitor/model_proxy.py) | 128 MiB per JSON document, SSE event, or received WebSocket message. 256 characters per selected scalar. 64 KiB binary decode and upload chunks. | Whole messages and individual parser values still consume resources. Chunks continue to EOF. No upstream maximum or current numeric calibration establishes these exact values. |
| Observation writer, [evidence_writer.py](../cachemonitor/evidence_writer.py) | 1,024 queued items, plus a possible dequeued item. One SQLite writer. | Preserve nonblocking forwarding and explicit observation-gap evidence on overflow. This contract permits missed new observations under overload. Queue count is not a byte ceiling or a throughput guarantee. |
| Connection ownership, [proxy_websocket.py](../cachemonitor/proxy_websocket.py), [proxy_http.py](../cachemonitor/proxy_http.py), and [proxy_listener.py](../cachemonitor/proxy_listener.py) | 100 WebSocket reservations and a separate 100 HTTP leases. Each admission owner allows 16 waiters. 32 safe idle WebSockets, pressure trigger 90 and target 80. Listener backlog request 128. | Bound active owners, waiting tasks, and idle retention separately. One connection per leased HTTP transport preserves cancellation ownership. These do not form one global 100-connection ceiling. |
| Source reads and discovery, [index.py](../cachemonitor/index.py) | 16 MiB file-scan slices, 64 MiB prefix-priority work, 8 MiB temporary path accounting, and 256-record rebuild yields. | Bound one pass and continue unfinished work. Startup quota seeding still samples eight files and 1 MiB tails. That heuristic does not define history retention. |
| Collection envelopes, [usage_collection.py](../cachemonitor/usage_collection.py) | 4,096 examined positions per catalog page. 4 MiB metadata-tree estimate and a separate 4 MiB encoded frame. 16 KiB optional owner envelopes and 16 MiB shared optional metadata. | Preserve bounded messages and optional acceleration. Source completeness and completed cursor traversal are separate facts. |
| Collection compatibility decoding, [collection_stream.py](../cachemonitor/collection_stream.py) | 16 owners, 4 MiB compressed bodies, 8 MiB plain JSON, and 64 MiB estimated objects per full-decoding response. Streaming uses 64 KiB chunks. | These values protect different representations. Larger owners use streaming. Post-decode estimates do not prove a peak-allocation bound. |
| Stream parser shape, [collection_stream.py](../cachemonitor/collection_stream.py) | 4 MiB string tokens, 8,192-byte literals, 64 JSON containers, and 32 MiB estimated retained members. | Bound individual values and nesting. Many small history rows can continue beyond these quantities. Exact supported-field maxima remain unestablished. |
| Owner preparation, [prepared_ingest.py](../cachemonitor/prepared_ingest.py) and [prepared_ledger.py](../cachemonitor/prepared_ledger.py) | 256 MiB owner source, 512 MiB required ancestry, and 256 MiB owner facts. | These guards own actual materialized objects. Paging the catalog or reducing a cohort does not split one oversized owner or ancestry. |
| Prepared work, [prepared_store.py](../cachemonitor/prepared_store.py) | 256 MiB staging. Pages of 512 rows, 4 MiB encoded bytes, and 32 MiB estimated objects. Fact writes of 256 rows, 256 KiB encoded bytes, and 4 MiB estimated objects. | Bound staging, reads, and transactions independently. Complete population size is separate from the size of an individual required value. |
| SQL reads, [prepared_store.py](../cachemonitor/prepared_store.py) and [prepared_analysis.py](../cachemonitor/prepared_analysis.py) | Ordinary reads use 2 seconds and 2,000,000 SQLite VM steps. Analytical reads use 60 seconds and 20,000,000 steps. | VM work was already counted per SQL cursor. `Snapshot.pages` renews only elapsed allowance between advancing bounded reads. Generation pinning, cancellation, and cursor progress remain. These are not CPU instruction or latency guarantees. |
| Prepared writes and WAL, [prepared_store.py](../cachemonitor/prepared_store.py) | 128 MiB WAL allowance with 32 MiB write reserve. | Admission leaves 96 MiB before reserved write room. Pinned readers can retain WAL pages. Removing lifetime database admission does not remove disk failures or WAL pressure. |
| Streaming read WAL, [collection_stream.py](../cachemonitor/collection_stream.py) | 64 MiB growth since lease start and 256 MiB absolute WAL pressure. | Distinguish an already large journal from growth caused during a pinned lease. Periodic checks can overshoot their thresholds. |
| Quota backlog, [quota_backlog.py](../cachemonitor/quota_backlog.py) | 4,096 ordinary items, 8 MiB encoded payloads, 32 control reservations, 4,096 pages of 4,096 bytes, and one unsaved response up to 8 MiB. | The main-file ceiling is 16 MiB. Reserved controls and the unsaved response are separate from ordinary admission. The 2 MiB live-WAL guard is also separate. Outage capacity is uncalibrated. |
| Required queries and publication, [prepared_query.py](../cachemonitor/prepared_query.py), [prepared_runtime.py](../cachemonitor/prepared_runtime.py), and [prepared_delivery.py](../cachemonitor/prepared_delivery.py) | 4 MiB request accounting, 64 MiB mailbox messages, 256 MiB publication state, and result-specific allowances. | Bound objects that callers actually require. Per-message continuation must preserve the complete result. Optional cache refusal cannot stand in for result admission. |
| Incremental speed, [prepared_speed.py](../cachemonitor/prepared_speed.py) | 4,096 scopes, 80,000 samples or memberships, and 64 MiB metadata. | Bound an optimization with an exact full-classifier fallback. These units differ from 80,000 owner ranges in prepared speed queries. Historical call-count measurements do not calibrate either current shape. |
| Checkpoints and append, [speed_checkpoint.py](../cachemonitor/speed_checkpoint.py) and [collection_append.py](../cachemonitor/collection_append.py) | 64 MiB complete checkpoints, 64 KiB chunks, 100,000 sections, and eight namespaces. Append retains 128 tail rows and 16 MiB shared accounting. | Preserve serialization ownership and exact fallback. A small chunk does not prove that one record or a complete checkpoint fits. |
| Reconstructible caches and packed rows, [analysis_engine.py](../cachemonitor/analysis_engine.py) and [quota_cycles.py](../cachemonitor/quota_cycles.py) | Cache-specific entry, row-weight, and byte allowances. Packed rows target 512 rows, 2 MiB input accounting, 64 KiB compressed blocks, and two decoded blocks. | Eviction recomputes exact values. Packed-row targets are soft for indivisible rows. Two blocks are a count, not a decoded-byte ceiling. SQLite negative `cache_size` values are KiB targets, not RSS ceilings. |
| Durable projections and maintenance, [quota_cycles.py](../cachemonitor/quota_cycles.py) | Quota payload conversion scans 512 rows with 1 MiB accounting and a 25 ms preparation deadline. Template reuse allows 64 entries within 1 MiB. Price-equivalence lookup allows 64 entries within 64 KiB. | Keep original payloads or authoritative calculation when optional conversion or caching cannot proceed. A successful historical corpus proves feasibility for that corpus, not optimal sizing. |
| Mismatch capture, [mismatch_history.py](../cachemonitor/mismatch_history.py) | 4 MiB pending capture and two scan leases. | Preserve nonblocking capture, explicit `capture_gap`, and rescan behavior. Rescanning cannot reconstruct every transient historical snapshot. |

## Semantic values and finite domains

Statistical windows define the result being computed. They are not optional
memory caches. The existing cache-health windows include 5, 50, and 30 baseline
rows, 120 peers, seven days, three slow samples, and two recovery samples.
Keeping those values preserves current classification behavior. It does not
establish their statistical accuracy. See [cache_health.py](../cachemonitor/cache_health.py)
and [speed_health.py](../cachemonitor/speed_health.py).

Display choices, chart buckets, title heuristics, notification floors, route
lookback, and diagnostic retention also have distinct meanings. They do not
permit deletion of underlying usage or quota history. Diagnostic examples
include 32 recent relay summaries, 64 phase events, and the last 24 traceback
frames. Rotating analysis diagnostics retain a current 256 KiB file and two
backups. An oversized record can exceed a nominal rotation size.

Some counts follow directly from a finite domain. Artwork has two themes.
Tray icons have two themes times three states, giving six combinations.
Twenty-eight task icons times two themes give 56 variants. A zero-argument
native loader has one key. These relationships explain the counts in
[icons.py](../cachemonitor/icons.py), [overlay_icons.py](../cachemonitor/overlay_icons.py),
and [native_backend.py](../cachemonitor/native_backend.py). They do not justify
unrelated color, text, or appearance cache sizes.

## Origin and evidence limits

The [first repository import](https://github.com/jisoq/Codexon/commit/eeb583dfaf179b20d4ae01bf48ff8e276815b95f)
already contains several observation, replay, and source-probe defaults. Earlier
numeric decisions are unavailable in the inspected repository history. Their
presence at import is not evidence that they were calibrated.

The [prepared collection introduction](https://github.com/jisoq/Codexon/commit/64394ddcc3b1dcc241fc7e909a04dd254516f095)
introduced the fixed catalog, prepared-file, certificate, and streaming budgets.
The original responsibilities were bounded IPC, preparation, and retained work.
Complete-history admission is the responsibility removed by this cleanup.

The [durable backlog introduction](https://github.com/jisoq/Codexon/commit/56437c2d3e8a673c8236ef8f96d1aae3b794f746)
established bounded failure recovery and producer backpressure. The
[checkpoint cleanup](../cachemonitor/speed_checkpoint.py)
removes duplicate source validation while retaining the owning packet guard.
The historical Selector socket rationale does not derive today's Proactor
connection defaults.

In the inspected SQLite 3.49.1 runtime, `max_page_count` is connection-local.
Removing the prepared writer's lifetime admission requires no persisted-limit
reset migration. Recovery-schema and backlog page limits still belong to their
own connections.

## Selected authority

Prepared format 9 separates selected membership and relative order from the
retained canonical population and global order. A selected result cannot retire
unrelated facts or claim complete aggregate coverage. A selected overlay must
resolve its family through the existing source index. Paging all history is not
an overlay prerequisite. Aggregate consumers retain complete-membership work.
See [prepared analysis cache](storage.md#prepared-analysis-cache).

Exact-default calibration and long-lived replay memory remain evidence gaps.
