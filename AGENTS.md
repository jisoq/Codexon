## Changelog and Release Notes

- Follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Keep the project's language, style, and version numbering.
- Link each version heading to its comparison with the previous public release. Verify version and date before publishing.
- Group user-visible changes under `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. Omit empty categories.
- Link each `Fixed` entry to the relevant issue or pull request. For direct fixes without either, link the fixing commit.
- One short line per change, stating the user-visible result. No implementation details, internal identifiers, or technical limits (e.g. "캐시 한도를 최적화했습니다.").
- Write the full Korean section, then `---`, then the full English section, with the same scope and category order. Do not interleave.
- Keep compatibility changes, known limitations, and required user actions as one-line bullets.
- Do not carry the legacy v2026.09.25.6 automatic proxy replacement warning into future release notes.
- Keep installation/download references below the categories in their existing format, and verify they apply to the release.

## Verification and CI Workflow

- Run `tools/verify_changes.py --base <last-verified-commit>` in the environment built from `requirements-release.lock`. Check affected changes first; use broader profiles only for a concrete coverage gap.
- Reuse successful results when code, dependencies, environment, and check settings match. A new session, PR merge, release, or manual dispatch alone does not justify rerunning them. Follow the existing planner and receipts in [verification policy](docs/verification.md).
- Run cheap failure checks before builds. Build a candidate once, verify that artifact, and publish the verified artifact without rebuilding.
- On failure, inspect that run's logs and artifacts first. Identify product, environment, or verifier failures, batch related fixes into one push, and rerun only invalidated checks. Never blindly retry an unchanged failure.
- In asynchronous and UI checks, wait for observable readiness with a deadline. Do not substitute fixed sleeps or longer timeouts for the correct completion condition.
- At handoff, record the verified commit, environment, check scope, CI run and artifact links, and unresolved failures. Never report incomplete or failed checks as passed.
