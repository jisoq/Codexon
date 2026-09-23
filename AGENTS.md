# Codexon repository guidance

This is the public Windows desktop application. `cachemonitor/` is an internal Python package name retained for data and settings compatibility. User-facing product naming is `Codexon`; logo/header naming is `CODEX·ON`.

For a request to install Codexon or prepare a usable local environment, follow [INSTALL.md](INSTALL.md). Use the verified per-user Setup release for ordinary use. Do not offer a portable ZIP; retain source setup for development and safe migration of legacy portable copies. Complete runtime and actual interface checks before reporting readiness.

## Proxy invariants

- Never force-cancel an in-flight request or resend it automatically. Verification must not create model requests.
- Background `ensure()` is read-only. Configuration changes require a user action.
- Preserve the existing Codex address, settings, index, and observation history during updates. Proxy switching uses the independent scheduled updater, journal, and rollback command.
- Distinct files are required for updater, settings-control, and journal locks. A health timeout of at least 3 seconds is required when deciding whether a Windows service stopped.
- Do not test against the installed proxy port 8768. Use isolated Codex homes, databases, task names, and ports.

## Verification

Run `python tools/verify_changes.py --plan` and the matching checks. When there is no baseline, or the change spans common infrastructure and dependencies, run the full suite. UI changes require real Qt Quick clicks and captures. The frozen executable must pass `--verify-runtime` and the isolated core package smoke. A proxy update needs the five proxy tests and the isolated two-executable switch test. See [verification](docs/verification.md).

The user may keep using the mouse during verification. Do not add or run automated tests that depend on the physical cursor staying in place, hover/tooltip timing, mouse dragging, or the desktop foreground window remaining unchanged. Use cursor-independent logic checks and rendering/captures; do not ask the user to stop using the computer to make tests pass.

## Packaging and publication

- Build in a versioned output folder; never overwrite a running executable. Keep any folder used by the GUI, supervisor, updater, or rollback journal.
- Build from a clean committed source revision so the manifest SHA is meaningful. The public manifest contains no local absolute paths. Use `tools/package_release.py --product <product-directory> --validate-only` to inspect the final installer payload.
- Preserve the module-specific Qt notices, the source offer and relinking instructions, and Pretendard JP's OFL notice.
- Release Windows x64 `Codexon-Setup.exe` and `Codexon-Setup.exe.sha256`, and verify the remote asset digest. The checksum is for the app updater and installation agents; ordinary users only download Setup. Do not publish a portable app ZIP. Public screenshots and logs must use synthetic or redacted data.
