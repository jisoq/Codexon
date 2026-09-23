# CODEX·ON

[한국어](README.ko.md) · [English](README.md)

**Did the model you called match the model that responded?** Codexon is a Windows companion for Codex that monitors request/response model mismatches to help you spot signs of model routing. Keep model names and usage visible in an overlay, explore session token usage and API-equivalent costs, and track your remaining usage limits.

![Codexon overlay with sample sessions](docs/media/overlay-sample.png)

- **Monitor request/response model mismatches.** Compare the model names recorded in each request and response to see when the reported response model differs from the one you called.
- See the latest call's tokens, cache rate, and API-equivalent cost in an overlay while working in Codex.
- Explore sessions and individual calls, including confirmed child-agent usage in session totals.
- Compare usage across models, reasoning efforts and service tiers, and inspect the calls behind each result.
- Follow remaining usage limits and reset times. The tray and taskbar widget keep them visible without opening the dashboard.

Costs are estimates using API prices; they are not your Codex bill. Model monitoring compares names recorded for a request and its response. Live limit checks use the installed Codex app-server and require an available account connection.

| Feature | What it needs |
| --- | --- |
| Session tokens, cache and API-equivalent costs | Local Codex usage records |
| Remaining limits and reset times | An available Codex account connection |
| Session overlay | Codexon running, the overlay enabled, and one identifiable Codex task window |
| Request/response model comparison | The optional proxy and matching, completed request/response evidence |

Supported installation target: **Windows 10/11 x64**. Setup includes Python, Qt and the independent connection recovery tool. Codexon can use your existing Codex account connection; no separate API key is required.

## Get started

**Installing with an AI assistant?** Give it this repository link and ask it to follow the [AI / LLM installation guide](INSTALL.md). The guide covers verified installation, first launch, optional model monitoring, and a ready-to-run source environment.

1. Download `Codexon-Setup.exe` from [Releases](https://github.com/jisoq/Codexon/releases).
2. Run Setup. No administrator rights or separate Python installation are required.
3. Open **Codexon** from Start. Enable **Settings → General → Starts when you log in to Windows** if you want it available after login.

Codexon is distributed as an installer, with in-app updates, Start-menu shortcuts and uninstall support.

The dashboard reads local Codex records. If there are no records yet, use Codex first and refresh the dashboard. In **Settings → General → Language**, choose English or 한국어; the selection takes effect on the next launch. Closing the dashboard keeps Codexon in the tray. Use the tray's Exit action to stop it.

## Optional model monitoring

Enable **Settings → Proxy → Use proxy** to compare the requested and reported response model. Turning it on sends one verification call through your existing Codex account connection. Once setup succeeds, restart Codex to apply the connection change. Session history, cost analysis and usage-limit views can be used without the proxy.

Codexon reports a model match or mismatch only when completed request/response evidence can be linked reliably. Missing or conflicting evidence is not treated as a mismatch. The reported model name does not prove which hardware or internal model implementation handled the request.

## Codex updates and the overlay

Enable **Settings → Session overlay**. The overlay checks the running Codex process and its current task route; it does not use a fixed list of allowed Codex versions. A version-number change alone does not disable it. Codexon must remain running, and the overlay is shown when the corresponding Codex window or its overlay controls are active. Reopening Codex does not launch a stopped Codexon.

Incomplete route records, multiple Codex task windows, remote tasks, and tasks that cannot be matched uniquely to local records do not receive another task's usage. If a future Codex release changes its route-log format, compatibility work may still be necessary. No model request is sent to detect the current task.

## Updates and recovery

Use **Settings → About and troubleshooting → Check and install updates**. Codexon verifies the release download and updates the app and connection components together while preserving settings and history. Older portable users can migrate by running Setup once.

Setup installs each version in a separate folder. An active proxy is updated only after its existing connections finish; requests are not force-cancelled or replayed. Keep older folders while they are used by a running component or rollback.

If Codex cannot connect, use **Start → Codexon 연결 복구 → 직접 연결로 복원** (restore direct connection). This independent tool works without the internet, the dashboard or a running proxy. After configuration recovery succeeds, finish any active work and restart Codex to apply the route change. A restored configuration or a responding local proxy is not itself proof of successful model communication.

While the proxy is enabled, a scheduled check can notify you after repeated confirmed local failures. It does not change your connection automatically or send model requests.

## Local data and removal

Codexon stores its usage index and quota history under `%LOCALAPPDATA%\CacheMonitor`, and model evidence, proxy state and configuration backups under `%USERPROFILE%\.cachemonitor\model-observer`. The older internal names are retained for compatibility. Updating or removing the program does not erase these records.

Use **Windows Settings → Apps → Installed apps → Codexon → Uninstall**. Removal restores the managed connection setting and defers deletion if app components are still in use. Configuration backups can contain sensitive values; do not attach them or unredacted records to bug reports. See the [user guide](docs/user-guide.md) for data handling and recovery details.

[Development](CONTRIBUTING.md) covers building and testing. [Security reporting](SECURITY.md) · [License](LICENSE) · [Third-party notices](THIRD-PARTY-NOTICES.md).

Codexon uses the custom **Codexon Attribution License 1.0**. Use, modification, and redistribution are permitted while preserving the original author **jisoq** and the original repository **https://github.com/jisoq/Codexon**, including their attribution in distributed graphical interfaces. See [LICENSE](LICENSE) for the full terms; third-party components retain their own licenses.
