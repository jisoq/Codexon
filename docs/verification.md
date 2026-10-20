# Verification

Use a dedicated environment installed from `requirements-release.lock`. `tools/verify_changes.py` checks that environment, runs audited calculation/data tests without Qt, and runs GUI and process tests serially on a hidden Windows desktop. The core group fails if it imports Qt. No test groups run in parallel.

| Situation | Command | Coverage |
| --- | --- | --- |
| Feature development | `python tools/verify_changes.py` | Changes since the last successful local check |
| Inspect selection | `python tools/verify_changes.py --plan` | Selection and reasons, without execution |
| Compare with a verified commit | `python tools/verify_changes.py --base <commit>` | Changes since that commit, including local edits |
| Essential regression sweep | `python tools/verify_changes.py --full` | Unit contracts and representative integration checks |
| Integration sweep | `python tools/verify_changes.py --integration` | Essential checks and all UI/process workflows |
| Large-data investigation | `python tools/verify_changes.py --extended` | All checks plus explicitly marked benchmarks |

Missing baselines and shared test/dependency changes select the essential suite. When a comparison commit is available, the same sweep also includes integration cases affected by those changes. Each selected case runs once. Unmapped source changes stop with an actionable error. Test execution stops at the first failure and retains its evidence. Failed checks and package-only checks do not advance the local source baseline. Keep a baseline only for the checkout and environment that produced it.

Plain `pytest` uses the fast essential profile. The `integration` marker identifies longer UI/process workflows; `essential` keeps representative cases in the default sweep. Affected selections automatically include their integration tests. For an explicit test path, use `python tools/run_ui_checks.py -- python -m pytest -m "not extended" <path>` to include those workflows. To run only benchmarks, use the same command with `-m extended`.

The default suite retains compact dashboard/quota rendering, rendered loading and stale-result handling, overlay controls, real protocol exchanges, accounting/history invariants, and installation rollback tests. Do not replace those contracts with exact labels, private call sequences, or screenshots without assertions.

## Native CPU kernels

Source builds require rustup and Microsoft C++ Build Tools for x64. `rust-toolchain.toml` pins the Rust compiler, and `native/Cargo.lock` pins native dependencies. Run these commands with the dedicated Python environment:

```powershell
python tools/build_native.py --test
python tools/verify_changes.py
python tools/benchmark_cpu.py --rows 20000 --samples 5 --min-cpu-ms 250 --output artifacts/verification/cpu-comparison.json
```

The first native build may download the pinned toolchain and crates. Add `--offline` to `build_native.py` after they are cached. The build helper checks the extension's import and ABI in a fresh Python process. `--reuse --test` reuses only a tested binary with matching sources, locks, build helper, compiler, Python ABI, platform, and build configuration. A missing or invalid receipt triggers a rebuild. `--check --test` validates the downloaded binary's source, locks, helper, Python/platform compatibility, binary hash, and test receipt before importing it. Artifact consumers do not need the producer's installed compiler. `build.ps1 -UseNativeArtifact` packages that validated artifact and fails if it is missing or invalid; ordinary local packaging retains compilation-cache validation. Documentation-only verification does not load or build Rust.

The native producer also prepares the exact crate and standard-library notices with `collect_notices.py --prepare-rust-artifact cachemonitor/_cpu_native.notices.zip`. The cache and shared artifact include this archive alongside the binary and receipt. It binds the receipt, locked dependency inputs, collector version, and notice contents. A valid archive is reusable without a Cargo source cache; fresh notice generation downloads locked dependencies when necessary. Artifact packaging validates and copies the archive without invoking Rust tools. Changing from the old two-file cache invalidates that cache once. Within the new format, collector-only changes can restore the matching binary by its native key and regenerate the notices.

`CODEXON_CPU_BACKEND=python` selects the retained Python reference. `rust` requires the compiled extension, while the default `auto` permits an absent extension in source checkouts. Unsupported numeric ranges and Python objects can still use the reference implementation to preserve accounting semantics. CI requires Rust for packaging and tests that may use application CPU kernels. Audited helper-only selections can skip it. Packaged runtime checks always require it.

The benchmark compares both backends on identical synthetic inputs, including Python/Rust conversion overhead. It reports median CPU milliseconds per call using `process_time_ns`, elapsed milliseconds using `perf_counter_ns`, repeated samples, result parity, and source/binary hashes. CPU reduction is `100 * (1 - Rust CPU time / Python CPU time)`. It excludes network and disk latency, GUI rendering, production data, and monitoring waits. These results describe the measured kernels and pipelines, not whole-application CPU usage or every possible Python bottleneck. Keep the machine otherwise idle and avoid concurrent builds or tests during measurement.

## CI and releases

`tools/ci_plan.py` is the policy for `.github/workflows/windows.yml`:

- `resolve` selects the checks. When needed, `native` supplies one tested Rust extension to both `source` and `package_build`, which run on separate runners in parallel. `package_verify` waits for both, and `verify` checks every required result. Rerun the failed package verification job to reuse its successful source job and retained candidate. Rerunning all jobs deliberately repeats work. A required skipped, cancelled, missing, or failed job fails the final gate.
- Source checks use an ancestor's successful `source` job and matching source receipt, even if its later installation check failed. Receipts bind coverage, commit, run ID, locked dependencies, Python, OS, architecture, runner image family, and source execution configuration. Runner image patch versions remain diagnostic; artifact retention and step names do not reset coverage. Every verification job checks compatibility before installing dependencies or compiling. Branch dispatches may reuse their own branch's receipts or main receipts; publication trusts only main. PRs may reuse a trusted dispatch receipt from their head branch when it is an ancestor of the tested merge commit, otherwise they use their base commit. Missing source receipts select essentials plus integrations affected since the deployment baseline. Without either baseline, CI runs essentials and the conservative package checks. Benchmarks remain opt-in.
- The native job restores only an exact cache match, validates the binary and receipt, and builds when necessary. Source and packaging jobs download that job's tested binary and validate it before installing dependencies. Caches cannot turn a failed native build or invalid binary into a successful check.
- Deployment impact uses its own fully verified ancestor. Advancing a source baseline never hides a previously failed installation or migration check. The baseline lookup examines at most 100 recent main runs. Local Git comparisons exclude incompatible tracked inputs before querying or downloading their source receipts. Expired or incompatible receipts fall back to broader checks.
- Pushes and manual requests for the same branch and commit share a concurrency group. A prepare request waits for the existing run and reuses its verified installer. Different commits do not cancel each other, so a newer push cannot interrupt an explicit release. Cancel obsolete runs explicitly when they are no longer needed.
- Prepare a release with `workflow_dispatch`, `prepare=true` on the reviewed main commit. Notes and a new version alone do not force packaging. When an ordinary main push already requires packaging and has an unpublished version with release notes, it also audits dependencies and retains the public installer after verification. Preparation can reuse that installer. Use `publish=true` separately after preparation succeeds; publication rejects missing or expired verified artifacts instead of building again.
- `prepare=true` also permits an unpublished branch candidate for pre-merge validation. Its receipt cannot authorize publication. For an explicit recovery exercise on a non-main branch, `exercise_rerun=true` fails the first package validation attempt after downloading and checking the candidate. Rerunning the failed job executes the real package checks with that retained candidate. The exercise is rejected on main and never suppresses a real check failure.
- Release candidates always build the current commit, check the frozen application, exercise recovery, test install/update/rollback/removal, validate the payload, and seal the exact installer with hashes.
- The installation lifecycle runs once. Installer or dependency changes select the MSIX-origin path, which also exercises native activation. Other package changes use the native-origin path.
- Legacy proxy/collector migration runs for relevant implementation, installer, or dependency changes. Only an app `VERSION` literal change can bypass the version file's proxy checks; changes to `PROXY_VERSION` or compatibility code still require them. Changed QA tools use a matching published binary when its packaged inputs are unchanged.
- Fractional and 2x scaling checks run for relevant UI or dependency changes. Ordinary tests already cover the base scale. Recovery runs in one language; source tests retain language selection and stored-value contracts.
- Manual `extended=true` adds benchmarks and legacy compatibility checks. It still runs only one installation origin.
- Publication reuses the successful exact-commit installer. A failed verification cannot publish by falling back to an older artifact. Receipts name the `affected-regression-v1` policy; older `full` receipts remain acceptable.

Each source run writes `tests-core.xml`, `tests-gui.xml`, `tests-installation.xml` for selected groups, test durations, and a JSON report under `artifacts/verification`. Test setup, call, and teardown start times stream into Actions while file logs remain available. Stack dumps do not by themselves terminate tests.

The source test phase has an internal 820-second deadline, including cleanup reserve, before the unchanged 15-minute Actions limit. Individual defaults are 180 seconds for core, 720 for GUI, and 540 for installation checks; each group is clipped to the remaining source budget with 20 seconds reserved for cleanup. A suspended child joins a kill-on-close Windows Job Object before running. At timeout, evidence records active test/phase, stacks and live PIDs before terminating that job. Fresh scopes identify scheduled or packaged processes outside it; cleanup checks their task markers, process creation times, and descendants. Cleanup failure fails the check.

Diagnostic logs have a 64 MiB per-runner budget; exceeding it fails the check rather than silently dropping evidence. Evidence is retained for seven days. Candidate payloads are capped at 2 GiB per run, bind all files to one commit and workflow run, and are retained for 30 days to cover the Actions rerun window. Source receipts (maximum accepted size 32 KiB) and verified release artifacts are also retained for 30 days. An expired candidate requires an explicit build rerun. A new commit always gets a new candidate; publication continues to use the sealed verified installer.

## Keeping checks small

Add a test for a distinct failure or externally observable contract. Use the smallest fixture that crosses the relevant boundary. Large-data latency thresholds belong in `extended`; ordinary tests check exact selections, preserved gaps, bounded results, and correct totals with small data.

Cover independent UI dimensions with representative combinations. Retain compact-window rendering, keyboard/navigation behavior, and theme/language boundaries. Avoid multiplying the same workflow across every combination or asserting unrelated styling constants.

Before this change, [release preparation 39408854990](https://github.com/jisoq/Codexon/actions/runs/39408854990) took 29m18s: source 15m06s, package build 4m01s, and package verification 8m48s. Its source selection included 1,790 cases because no compatible source receipt was available. These are one run's observations, not a promised duration or a controlled performance comparison.
