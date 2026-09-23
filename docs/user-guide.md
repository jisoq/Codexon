# User guide

## Local data and network use

Codexon reads local Codex session records and usage metadata. Its usage index stores the fields needed for tokens, cache, model, time, status, and session analysis. It does not need conversation text to calculate these views. The index still contains local paths and identifiers. The separate quota ledger and model-observation data retain observed history. The dashboard may display your project and task names.

When you enable the optional proxy, Codexon backs up the **entire prior** Codex `config.toml` in `%LOCALAPPDATA%\CacheMonitor\backups`. That backup can contain sensitive values from your configuration. The proxy relays Codex requests. Live limits are read through the installed Codex `app-server` account RPC; analysis of past usage is based on local records. Codexon is not an offline-only product.

## Update

Extract the new ZIP into a new folder. Keep the previous folder while the GUI or proxy still uses its executable. Close the old GUI and launch the new `Codexon.exe`; verify the app version in **Settings → About and troubleshooting**. The proxy can continue running from the old folder. In **Settings → Proxy**, check its separate version and use **Update proxy** if offered. The update waits for existing connections to finish; idle WebSockets also count, so close Codex when needed. Check that the new proxy version and healthy status are shown before removing the old executable folder. A brief failure for new connections is possible during the switch.

## Remove

1. In **Settings → Proxy**, turn off proxy use. Check that the status says it is off and restart Codex to restore direct connection.
2. In **Settings → General**, turn off Windows login startup. Close Codexon from its tray icon.
3. Remove the extracted application folder only after the proxy and GUI no longer use it.

Application data under `%LOCALAPPDATA%\CacheMonitor` is separate. `usage-index.sqlite` is a rebuildable index; `quota-cycles.sqlite`, model evidence, and `backups` hold observed history and recovery material. Keep those if you might need past usage or a configuration rollback. Removing the executable does not remove this data.

## Recover direct connection without the GUI

In PowerShell, run the executable from an intact Codexon folder:

```powershell
& 'C:\path\to\Codexon\Codexon.exe' --disable-model-observer --control-report "$env:TEMP\codexon-recovery.json"
& 'C:\path\to\Codexon\Codexon.exe' --model-observer-status
```

The first command should finish without an `error` field. The second should report that the proxy is not configured. Restart Codex and check that its normal connection works. If recovery fails, preserve the existing configuration and backup and report the error privately; do not delete all of `config.toml` or your `.codex` folder. The report can include local paths or identifiers; review it before sharing.

## Safe bug reports

Report the Codexon, Windows, and Codex versions, display scale, steps to reproduce, and the expected and actual behavior. Use a synthetic session if a screenshot is needed. Do not upload your complete `.codex` directory, databases, backup `config.toml`, `--snapshot` output, or unredacted `--control-report`. Redact project names, task names, paths, IDs, and secrets from the minimum excerpt needed. For a vulnerability, use the private channel in [SECURITY.md](../SECURITY.md).
