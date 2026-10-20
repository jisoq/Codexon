# Application and connection lifetime

Codexon retains separate owners for app services, connection settings, proxy
replacement and installation. The shared shutdown contract coordinates the
exit UI with service cleanup; it does not introduce another supervisor.

| Action | Intended result |
|---|---|
| Close the dashboard with a tray available | Hide the window; keep services |
| Close ChatGPT or a CLI | Keep Codexon and its enabled recording setting |
| Turn recording off | Restore direct routing; preserve accepted work |
| Exit Codexon | Finish responses and records, stop owned services |
| Interrupt responses and exit | Cooperatively interrupt the verified proxy, then finish cleanup |
| Replace the GUI for language or installation | Keep proxy and collector ownership |

## Cancellation and completion

Exit preparation remains cancellable. Unknown work continues serving until the
client closes it or the user explicitly requests supported response interruption.
Elapsed time never authorizes force.

Cancellation and the exit cutover share a decision gate. The service owner
records cutover under its existing connection lock before changing routing or
requesting drain. Waiting holds no arbitration lock. A cancellation request that
loses this race cannot claim success. The UI shows the owner's outcome, and
destroys its working resources only after successful shutdown.

The proxy itself owns admission and response completion. Work accepted after
the coordinator's last idle observation must still finish. Once draining starts,
new connections are rejected; accepted work is not replayed.

Physical release and successful storage flush are different facts. Recovery
requires process, listener and ownership-lock evidence. A missing storage
receipt remains a warning tied to its source instance. It never becomes a
fabricated successful flush. Proxy replacement retains its existing stricter
settlement requirements.

Explicit Off and an external connection-setting edit override saved resume
intent. An absent captured source does not authorize a guessed command or a
duplicate process.

Standalone recovery inspects the recorded exit owner's process identity.
Proven owner exit permits explicit direct-route recovery without draining or
terminating the remaining proxy. Recovery rechecks scope, operation and owner
under the route-control lock. A live or unverifiable owner keeps recovery blocked.

## Updates

The app update owner reconciles component results using operation identity,
connection scope and target identity. A terminal proxy result remains consumable
after a crash between journal writes. Replaying it must be idempotent and cannot
complete a newer operation.

Committed installation receipts repair a crash before the app result is written.
A crash after that result but before proxy dispatch is reconciled separately,
after the installer and updater have released ownership. Interrupted rollback
retains its original source and execution phase.

A stale timestamp indicates delayed information. Reporting interruption also
requires evidence that the responsible task and owner have ended. Uncertain
ownership prevents another launch. Widgets render these results; they do not
turn an aged progress record into a persistent failure.

Cancellation before installation dispatch differs from deferring a remaining
proxy update after the app has installed. Once proxy replacement begins, its
verification or rollback completes before another lifecycle change.

“Continue in the new app” carries the specific verified operation through GUI
replacement. The new GUI resumes that operation once its service controller is
ready. Ordinary startup does not automatically apply a deferred update.
GUI handoff shares one deadline across its waits and preserves the old window
when completion arrives too late.

## Verification

Focused checks cover cancellation on both sides of cutover, failed-exit UI
recovery, source capture, external setting edits, missing flush receipts,
operation-matched update replay and preserved GUI handoff. Relay checks submit
known and unknown work after an idle probe, then verify final delivery during
drain and refusal of new requests.

Run the affected checks through `tools/verify_changes.py --base <verified-commit>`
in the locked release environment. GUI and process checks run serially on the
hidden test desktop with isolated data and ports.
