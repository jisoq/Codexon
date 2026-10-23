# Update startup regressions

On October 23, an update completed without reopening Codexon. Codex had already restarted before observation was restored. A resolved collection-delay notification prompted a separate delivery reproduction. The installed version number stayed the same, so verification identifies the source commit and running executable.

## Restarting the client before its route

A stopped Codexon session can retain a saved request to resume its proxy. Its temporary direct route does not mean recording was deliberately disabled. The old installer classified that temporary route as Off and allowed Codex to restart before Codexon restored the proxy. The running client retained direct routing and produced no proxy timing observations.

A normal update must start the new Codexon, verify its intended connection route, and only then reopen Codex. Readiness must belong to the same update operation and installed executable. Process existence alone does not prove that the route is ready. Explicit Off and an external route edit remain authoritative. An authorized launch reopens after a completed stop. A live or uncertain exit owner retains control.

Connection replacement can continue in a separate worker after GUI startup. The GUI must consume the matching operation's terminal result before acknowledging readiness. A completed connection result cannot replace missing GUI startup evidence. Retry must recover the same owner and honor current close authorization.

Missing output speed in this incident did not come from unknown request mode. The inspected root and descendant requests retained Standard mode. Output speed requires actual completed-response timing. Do not infer missing wire timing from token counts, latency, or a current global preference.

## Reporting collection delay while results are fresh

The old service controller waited for ancillary startup before delivering collection status. Analysis preparation could also delay the first snapshot. A fresh collector publication could therefore remain invisible to the dashboard for more than its freshness deadline.

Collection publication evidence must reach the dashboard and overlay independently of ancillary startup and analysis preparation. A collector that never publishes must still become delayed. Explicit source and lifecycle failures remain visible. Repeated pending polls cannot extend the deadline.

The original resolved history event alone did not prove a failed collector. The independent blocked-delivery reproduction established the faulty mechanism.

## Regression evidence

The regression guards cover these observable contracts.

- [Installer reopening](../../tests/test_install_management.py) preserves completed shutdown, active exit ownership, explicit no-launch, and a failed launch without rolling back the verified installation.
- [Client restart](../../tests/test_client_restart.py) rejects both Off and restored failure results while saved resume remains pending. Retry preserves completed close authorization before route readiness.
- [Installed startup](../../tests/test_installed_startup.py) guards GUI and route ordering, saved resume, retry, owner loss, and recovered connection readiness.
- [Collection observation](../../tests/test_collection_observation.py) preserves publication order and owner identity. Fresh observations cannot clear explicit lifecycle failures. A passive reader creates no missing store, and shutdown joins its worker and rejects queued observations.

The blocked-delivery reproduction holds connection startup and analysis delivery at event barriers. Fresh publications must prevent dashboard and overlay delay without creating a history row. The no-publication control must retain a visible delay. The original resolved history event does not establish which delivery path blocked during that particular launch.

Use `python tools/verify_changes.py --base <last-verified-commit>` to select affected checks. Local incident evidence belongs in the ignored verification artifacts.
