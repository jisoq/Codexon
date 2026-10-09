# Project instructions

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development, architecture, graph axes, validation, installation, and release policies. Read [docs/verification.md](docs/verification.md) for the applicable check procedures.

## Agent workflow

- Choose the simplest solution that fully meets the request. Keep responsibilities focused and propose maintainability improvements without implementing unrequested scope.
- Preserve unrelated work and user data. Commit and push only within the user-authorized scope.
- Before removing artifacts, verify their resolved absolute paths and whether any running process or other task uses them. Preserve the current installation, recovery tools, required rollback material, and verification evidence. Never force-delete locked files.
- Retire managed worktrees through the app's `archive_worktree` tool so their changes remain recoverable.
- Do not use the middle-dot character in new output. Prefer clear noun phrases for UI labels and avoid unnecessary Korean particles.

## Release Notes

- Use [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) for changelogs and release notes. This structure takes precedence over the format of older release notes. Preserve the project's existing language and writing style.
- Use `## [<version>] - YYYY-MM-DD` for version entries, with the heading linked to the comparison against the previous public release. Collect ongoing changes under `## [Unreleased]`; a versioned release draft may use its target version and planned date, which must be confirmed before publication. Preserve the project's existing version numbering.
- Group changes under the exact headings `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, and `### Security`, in that order. Include only categories with relevant changes; do not add empty headings.
- Keep Korean and English descriptions equivalent in scope, conditions, and limitations. Place each translation with its corresponding entry under the same change category. Keep installation instructions and download references below the change categories.
- Focus on what users can now do, which inconveniences have been reduced, and how existing behavior has changed. Do not list commits or modified files.
- Group related changes by the user experience they affect. Include internal refactoring, test improvements, and implementation details only when they directly affect users.
- Give each item a short, specific title and a concrete description of the change. Do not rely solely on vague phrases such as "improved stability" or "optimized performance."
- Describe only changes actually included in the release. Do not speculate about unverified benefits, performance figures, or scope of support.
- Clearly explain compatibility changes, discontinued support, known limitations, and any actions required after updating. Preserve the existing format for installation instructions and reference links, and verify that they apply to the relevant version.
