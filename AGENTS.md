# Project instructions

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development, architecture, graph axes, validation, installation, and release policies. Read [docs/verification.md](docs/verification.md) for the applicable check procedures.

## Agent workflow

- Choose the simplest solution that fully meets the request. Keep responsibilities focused and propose maintainability improvements without implementing unrequested scope.
- Preserve unrelated work and user data. Commit and push only within the user-authorized scope.
- Before removing artifacts, verify their resolved absolute paths and whether any running process or other task uses them. Preserve the current installation, recovery tools, required rollback material, and verification evidence. Never force-delete locked files.
- Retire managed worktrees through the app's `archive_worktree` tool so their changes remain recoverable.
- Do not use the middle-dot character in new output. Prefer clear noun phrases for UI labels and avoid unnecessary Korean particles.
