# CODEX·ON

[한국어](README.ko.md) · [English](README.md)

**Did the model you called match the model that responded?** Codexon is a Windows companion for Codex that monitors request/response model mismatches to help you spot signs of model routing. Keep model names and usage visible in an overlay, explore session token usage and API-equivalent costs, and track your remaining usage limits.

![Codexon overlay with sample sessions](docs/media/overlay-sample.png)

- **Monitor request/response model mismatches.** Compare the model names recorded in each request and response to see when the reported response model differs from the one you called.
- See the latest call's tokens, cache rate, and API-equivalent cost in an overlay while working in Codex.
- Explore sessions and individual calls, including confirmed child-agent usage in session totals.
- Follow remaining usage limits and reset times. The tray and taskbar widget keep them visible without opening the dashboard.

Costs are estimates using API prices; they are not your Codex bill. Model monitoring compares names recorded for a request and its response. Live limit checks use the installed Codex app-server and require an available account connection.

## Get started

1. Download the Windows x64 ZIP and SHA-256 file from [Releases](https://github.com/jisoq/Codexon/releases).
2. Check the ZIP's SHA-256, then extract the **entire** `Codexon` folder.
3. Run `Codexon.exe`. Python is included in the package and does not need a separate installation.

The dashboard reads local Codex records. If Codex is not installed or there are no records yet, use Codex first and refresh the dashboard. In **Settings → General → Language**, choose English or 한국어; the selection takes effect on the next launch. Turn on the session overlay in **Settings → Session overlay**. Request/response model monitoring is optional: enable it in **Settings → Proxy**, then restart Codex to apply the connection change.

[User guide](docs/user-guide.md) covers updates, removal, connection recovery, local data, and safe bug reports. [Development](CONTRIBUTING.md) covers building and testing. [Security reporting](SECURITY.md) · [MIT license](LICENSE) · [Third-party notices](THIRD-PARTY-NOTICES.md).
