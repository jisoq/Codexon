# Verification

Use a dedicated environment installed from `requirements-release.lock`. `tools/verify_changes.py` checks that environment and runs UI tests on a hidden Windows desktop.

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

- Source checks compare against the last successful main-branch ancestor, or the PR base. A release version or manual dispatch does not discard that baseline. Missing CI baselines and shared test/dependency changes require an integration sweep. Benchmarks remain opt-in.
- Release candidates always build the current commit, check the frozen application, exercise recovery, test install/update/rollback/removal, validate the payload, and seal the exact installer with hashes.
- The installation lifecycle runs once. Installer or dependency changes select the MSIX-origin path, which also exercises native activation. Other package changes use the native-origin path.
- Legacy proxy/collector migration runs for relevant implementation, installer, or dependency changes. Only an app `VERSION` literal change can bypass the version file's proxy checks; changes to `PROXY_VERSION` or compatibility code still require them. Changed QA tools use a matching published binary when its packaged inputs are unchanged.
- Fractional and 2x scaling checks run for relevant UI or dependency changes. Ordinary tests already cover the base scale. Recovery runs in one language; source tests retain language selection and stored-value contracts.
- Manual `extended=true` adds benchmarks and legacy compatibility checks. It still runs only one installation origin.
- Publication reuses the successful exact-commit installer. A failed verification cannot publish by falling back to an older artifact. Receipts name the `affected-regression-v1` policy; older `full` receipts remain acceptable.

Each source run writes `tests.xml`, test durations, and a JSON report under `artifacts/verification`. CI retains those reports and screenshots, not copies of old executable payloads. Use the failing assertion and those artifacts before retrying.

## Keeping checks small

Add a test for a distinct failure or externally observable contract. Use the smallest fixture that crosses the relevant boundary. Large-data latency thresholds belong in `extended`; ordinary tests check exact selections, preserved gaps, bounded results, and correct totals with small data.

Cover independent UI dimensions with representative combinations. Retain compact-window rendering, keyboard/navigation behavior, and theme/language boundaries. Avoid multiplying the same workflow across every combination or asserting unrelated styling constants.

The reference release CI is [run 38039625255](https://github.com/jisoq/Codexon/actions/runs/38039625255): 22m47s from creation to completion, with 8m25s source checks, 5m43s across two installation origins, and 2m56s legacy migrations. These are historical observations, not a promised duration for future runners.

On the same local locked environment, the integration-inclusive sweep passed 877 tests in 255.09s. After separating the slow workflows, the essential sweep passed 783 tests in 123.53s (51.6% less wall time). These measurements compare coverage profiles, not equal workloads or different runner speeds. Two live Explorer checks remained skipped.

Applying the new policy to that reference release's changed files selects 563 source cases instead of 875 and removes one installation origin plus unrelated legacy migrations. Their historical step durations total about 5m47s. The new remote CI duration must be measured after pushing this configuration.
