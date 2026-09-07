# v3.0.1 — macOS Loopback Bridge Reliability Hotfix

## Why this release exists

v3.0 proved that the unpacked Chrome extension could load, but on the user's Mac the extension displayed `Native host has exited.` before the search could begin. The browser extension itself was alive; the failure was entirely in Chrome Native Messaging process startup/registration.

## Architectural fix

- Chrome Native Messaging is no longer used by the default runner.
- `RUN_ACCEPTANCE_INDEED.command`, `RUN_FAST_SEARCH.command`, and `RUN_FULL_SEARCH.command` now start `jobbot_bridge.py` directly from the same local Python environment the user launched from Terminal.
- The bridge binds **only** to `127.0.0.1` on an ephemeral local port.
- Every run uses a cryptographically random bearer token; the extension receives the port/token only on its own `chrome-extension://.../start.html` URL.
- The extension must include the token on every local RPC call.
- The bridge is killed when the launcher exits.
- The extension no longer requests the `nativeMessaging` permission.
- The old `com.jobbot.local` Native Messaging manifest is removed by `INSTALL_MAC.command` so stale host registration cannot interfere.

## Security properties

- Loopback bind only (`127.0.0.1`), never `0.0.0.0`.
- Random per-run token (not stored in config or DB).
- 4 MiB inbound request cap.
- No form submission, application submission, CAPTCHA solving, challenge bypass, cookie export, or password handling.
- The local bridge has no internet-listening socket.

## Search behavior unchanged

This hotfix changes only the Chrome-extension-to-Python transport. Platform-first search, exhaustive keyword tasks, permanent SQLite history, dedupe, versioning, diffs, remote gates, qualification scoring, and no production result-count cap remain unchanged.
