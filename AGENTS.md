## Changelog and Release Notes

- Follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). Keep the project's language, style, and version numbering.
- Link each version heading to its comparison with the previous public release. Verify version and date before publishing.
- Group user-visible changes under `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. Omit empty categories.
- One short line per change, stating the user-visible result. No implementation details, internal identifiers, or technical limits (e.g. "캐시 한도를 최적화했습니다.").
- Write the full Korean section, then `---`, then the full English section, with the same scope and category order. Do not interleave.
- Keep compatibility changes, known limitations, and required user actions as one-line bullets.
- Keep installation/download references below the categories in their existing format, and verify they apply to the release.

## Publishing

- Push the reviewed version and notes to `main`, run `windows.yml` with `prepare=true` for that commit, then run it separately with `publish=true` after verification succeeds. Publishing reuses the verified installer and does not rebuild.
- Do not delete or duplicate source archives referenced by earlier releases. When Qt/PySide6 changes, update `third-party-sources.lock.json` with the upstream hashes.

## Verification before CI

- Run `tools/verify_changes.py` against the last verified state (`--base <commit>`), not the full suite after every edit.
- Use `--full`, `--integration`, `--extended` only when needed.
- Add tests only for distinct user-visible behavior, data invariants, protocol contracts, or demonstrated failures. Avoid asserting on implementation details, copy, pixel constants, or UI matrices.
- CI selects checks via `tools/ci_plan.py`. A release or manual dispatch alone does not require all tests. See [verification policy](docs/verification.md).
- Use a dedicated environment built from `requirements-release.lock`. Every import, including those inside functions, must be in the lock.
- Async tests: wait on an observable condition with a deadline, never a fixed sleep.
- UI tests: use the hidden desktop and rendered control geometry, and keep compact-window coverage. Deliver chart events directly when testing selection independent of hover.
- On CI failure, apply the [codexon-mode skill](.agents/skills/codexon-mode/SKILL.md) and inspect the failing assertion and retained evidence before retrying. Preserve numerical, state, and navigation contracts; avoid assertions tied only to copy or punctuation.
