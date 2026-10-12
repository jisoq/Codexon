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

Missing baselines and shared test/dependency changes select the essential suite. Unmapped source changes stop with an actionable error. Test execution stops at the first failure and retains its evidence. Failed checks and package-only checks do not advance the local source baseline. Keep a baseline only for the checkout and environment that produced it.

Plain `pytest` uses the fast essential profile. The `integration` marker identifies longer UI/process workflows; `essential` keeps representative cases in the default sweep. Affected selections automatically include their integration tests. For an explicit test path, use `python tools/run_ui_checks.py -- python -m pytest -m "not extended" <path>` to include those workflows. To run only benchmarks, use the same command with `-m extended`.

The default suite retains compact dashboard/quota rendering, rendered loading and stale-result handling, overlay controls, real protocol exchanges, accounting/history invariants, and installation rollback tests. Do not replace those contracts with exact labels, private call sequences, or screenshots without assertions.

## Native CPU kernels

Source builds require rustup and Microsoft C++ Build Tools for x64. `rust-toolchain.toml` pins the Rust compiler, and `native/Cargo.lock` pins native dependencies. Run these commands with the dedicated Python environment:

```powershell
python tools/build_native.py --test
python tools/verify_changes.py
python tools/benchmark_cpu.py --rows 20000 --samples 5 --min-cpu-ms 250 --output artifacts/verification/cpu-comparison.json
```

The first native build may download the pinned toolchain and crates. Add `--offline` to `build_native.py` after they are cached. The build helper checks the extension's import and ABI in a fresh Python process. `build_native.py --check`, source verification, and release packaging reject a present extension whose source or binary hash changed. Documentation-only verification does not load or build Rust.

`CODEXON_CPU_BACKEND=python` selects the retained Python reference. `rust` requires the compiled extension, while the default `auto` permits an absent extension in source checkouts. Unsupported numeric ranges and Python objects can still use the reference implementation to preserve accounting semantics. CI builds the extension and selects `rust`; packaged runtime checks require it. Rebuild after changing native sources.

The benchmark compares both backends on identical synthetic inputs, including Python/Rust conversion overhead. It reports median CPU milliseconds per call using `process_time_ns`, elapsed milliseconds using `perf_counter_ns`, repeated samples, result parity, and source/binary hashes. CPU reduction is `100 * (1 - Rust CPU time / Python CPU time)`. It excludes network and disk latency, GUI rendering, production data, and monitoring waits. These results describe the measured kernels and pipelines, not whole-application CPU usage or every possible Python bottleneck. Keep the machine otherwise idle and avoid concurrent builds or tests during measurement.

## CI and releases

`tools/ci_plan.py` is the policy for `.github/workflows/windows.yml`:

- The sequential jobs are `resolve`, `source`, `package_build`, `package_verify`, and the final `verify` gate. Rerun the failed package verification job to reuse its successful source job and retained candidate. Rerunning all jobs deliberately repeats all work. A required skipped, cancelled, missing, or failed job fails the final gate.
- Source checks use an ancestor's successful `source` job and matching source receipt, even if its later installation check failed. Receipts record coverage, commit, run ID, and the locked environment/runner image. Common configuration, runner, dependency, and image changes invalidate the receipt. Branch dispatches may reuse their own branch's receipts or main receipts; publication trusts only main. PRs may reuse a trusted dispatch receipt from their head branch when it is an ancestor of the tested merge commit, otherwise they use their base commit. Missing CI baselines require an integration sweep. Benchmarks remain opt-in.
- Deployment impact uses its own fully verified ancestor. Advancing a source baseline never hides a previously failed installation or migration check. The baseline lookup examines at most 100 recent main runs; expired or incompatible receipts fall back to broader checks.
- Ordinary pushes and PRs use branch/PR cancellation groups. New commits cancel superseded ordinary runs. Explicit release requests use separate run groups and are protected from that cancellation.
- Prepare a release with `workflow_dispatch`, `prepare=true` on the reviewed main commit. Notes and a new version alone do not request a candidate. Preparation includes dependency auditing. Use `publish=true` separately after preparation succeeds; publication rejects missing or expired verified artifacts instead of building again.
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

The reference release CI is [run 38039625255](https://github.com/jisoq/Codexon/actions/runs/38039625255): 22m47s from creation to completion, with 8m25s source checks, 5m43s across two installation origins, and 2m56s legacy migrations. These are historical observations, not a promised duration for future runners.

On the same local locked environment, the integration-inclusive sweep passed 877 tests in 255.09s. After separating the slow workflows, the essential sweep passed 783 tests in 123.53s (51.6% less wall time). These measurements compare coverage profiles, not equal workloads or different runner speeds. Two live Explorer checks remained skipped.

Applying the new policy to that reference release's changed files selects 563 source cases instead of 875 and removes one installation origin plus unrelated legacy migrations. Their historical step durations total about 5m47s. The new remote CI duration must be measured after pushing this configuration.
