# JobBot v3.1.0 Safety

The browser automation is intentionally read-only for discovery and research.

It may navigate normal job-search pages, scroll/paginate, open job-detail pages, read rendered job data, and send structured records to the local ledger.

It does **not** submit applications, fill employer application forms, upload files, solve CAPTCHAs, bypass Cloudflare/security challenges, alter fingerprints, rotate proxies, exploit hidden APIs, export cookies/passwords, or execute downloaded content.

The local Python bridge binds only to `127.0.0.1`, uses an ephemeral high port and a cryptographically random per-run token required on every RPC, and is terminated when the launcher exits. The extension has no need for the Chrome `nativeMessaging` permission. The bridge logs events to `out/logs/run_<RUN_ID>.log` without recording cookies, passwords, or browser profile data.

If a platform presents a verification/challenge page, that platform is checkpointed/paused rather than bypassed. Other platforms can continue. Emergency stop marks active work incomplete and resumable; it does not claim that the search universe was exhausted.
