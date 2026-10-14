## Changelog and Release Notes

- Follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/); it overrides older release-note formats. Keep the project's existing language, style, and version numbering.
- Link each version heading to its comparison with the previous public release. Verify the target version and planned date before publishing.
- Group verified, user-relevant changes under the applicable Keep a Changelog categories (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`); omit empty categories.
- Write each change as one short, single-line bullet describing the user-visible result. Omit detailed implementation explanations, internal identifiers, and technical limits unless essential to a user action or limitation. For example, write "캐시 한도를 최적화했습니다." instead of explaining serialization and buffer sizes.
- Write the complete Korean section first, then a Markdown horizontal rule (`---`), then the complete English section. Do not alternate Korean and English within individual entries or categories. Keep both sections equivalent in scope, conditions, limitations, and category order.
- Retain compatibility changes, known limitations, and required user actions as concise, single-line bullets.
- Keep installation and download references below the change categories, in their existing format, and verify they apply to the release.
- To publish, push the reviewed version and notes to `main`, then run `windows.yml` with `prepare=true` for that commit. After verification succeeds, run it separately with `publish=true`. Publication reuses the exact commit's verified installer and does not start a separate build.
- Keep source archives at their locked download locations, since releases reuse them. When Qt/PySide6 changes, update `third-party-sources.lock.json` with the matching upstream hashes. Do not duplicate source assets or delete archives referenced by earlier releases.

## Verification before CI

- During feature work, run `tools/verify_changes.py` against the last successful local state (`--base <verified-commit>` for an explicit baseline). Do not run the full suite after every edit.
- `--full` runs the fast essential suite, and affected changes automatically include their integration tests. `--integration` runs all integration tests, and `--extended` adds large-data benchmarks. Do not run the broader suites routinely.
- Add a test only for a distinct user-visible behavior, data invariant, protocol contract, or demonstrated failure. Prefer small fixtures and observable results over implementation details, copy, pixel constants, and Cartesian UI matrices.
- CI uses `tools/ci_plan.py` to select source and compatibility checks. A release or manual dispatch alone does not require all source tests or legacy migrations. See [verification policy](docs/verification.md).
- Use a dedicated Python environment installed from `requirements-release.lock`. `tools/verify_changes.py` checks versions and imports. Fix environment differences there, not in the running app's environment. Every source and test import, including those inside functions, must be in the lock.
- In async tests, wait for an observable completion condition with a deadline, not a fixed sleep.
- UI tests must use the hidden desktop and rendered control geometry, and keep compact-window coverage. Deliver chart events directly when testing selection independently of hover.
- After a CI failure, read the failing assertion and retained evidence before retrying. Preserve numerical, state, and navigation contracts, and avoid assertions tied only to copy or punctuation.
