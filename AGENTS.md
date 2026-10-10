## Changelog and Release Notes

- Follow [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) for changelogs and release notes. Its structure takes precedence over older release-note formats. Preserve the project's existing language, writing style, and version numbering.
- Link each version heading to its comparison with the previous public release. Release drafts may use a target version and planned date, but verify both before publication.
- Group related changes by user experience. Use short, specific titles and concrete descriptions. Include only verified changes and user-relevant implementation details. Clearly state compatibility changes, known limitations, and required user actions.
- Keep Korean and English descriptions equivalent in scope, conditions, and limitations. Place both translations together under the same change category.
- Keep installation instructions and download references below the change categories. Preserve their existing formats and verify their applicability to the release.
- Push the reviewed version and release notes to `main`, then request `windows.yml` with `publish=true`. The workflow waits for and reuses the exact commit's fully verified installer; do not start a separate build for publication.
- Keep corresponding source archives available at their locked download locations. Releases reuse those links. When Qt/PySide6 changes, update `third-party-sources.lock.json` with the matching upstream hashes; do not duplicate source assets or delete archives referenced by previous releases.

## Verification before CI

- Use a dedicated Python environment installed from `requirements-release.lock`. `tools/verify_changes.py` checks installed versions and imports before running selected tests. Resolve environment differences in that dedicated environment instead of changing the running app's environment.
- Keep source and test imports in the lock, including imports inside functions. A library installed only on a developer machine is not a CI dependency.
- For asynchronous tests, wait for the observable completion condition with a deadline. Do not assume a fixed sleep guarantees completion.
- UI tests must use the hidden desktop and rendered control geometry. Preserve compact-window coverage and deliver chart events directly when testing selection independently of hover.
- After a CI failure, read the failing assertion and retained evidence before retrying. Preserve numerical, state and navigation contracts; avoid assertions tied only to copy or punctuation.
