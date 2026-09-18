# Troubleshooting

## Extension says disconnected or `Failed to fetch`

Run `./INSTALL_MAC.command --profile-directory "<Profile Path final component>"`
once, then load the exact stable path it prints (macOS:
`~/Library/Application Support/JobBot/extension`) in that targeted ordinary
Chrome profile. Use `./SYNC_EXTENSION.command` after integrated source changes.
Start through `python -m jobbot run-now`, `python -m jobbot resume`, or a
`.command` launcher. Those commands use the machine-local profile binding,
generate the per-run port/token, request the freshness gate, and open
`dashboard.html` in ordinary Chrome. For a maintenance-only check use
`./REFRESH_EXTENSION.command`; it confirms the loaded `manifest.json`
`version_name` and deployment identity before reporting success. Confirm the ID
matches `config/EXTENSION_ID.txt`.

The first `Load unpacked` installation remains a manual Chrome bootstrap. A
refresh failure is fail-closed and includes a structured classification:
`intended_extension_reachable_and_current`, `wrong_or_stale_build`,
`extension_absent_disabled_or_unavailable`,
`wrong_or_untargeted_chrome_profile_or_instance`,
`bridge_auth_or_configuration_failure`, or
`bootstrap_or_deployment_source_mismatch`. `active_run` means an active browser
run was protected from interruption. Do not click Reload in
`chrome://extensions` as a routine step; resolve the reported boundary and
rerun the supported command.

The bridge is intentionally an ephemeral `127.0.0.1` process. When a run
finishes or is stopped, the launcher tears it down; an extension dashboard
that continues polling the old port can briefly show `Failed to fetch`. The
updated extension records the last terminal run and renders that state instead
of treating normal shutdown as an active-run failure. If the error appears
while the run is active, the message includes the port and RPC action; inspect
the matching `out/logs/run_<id>_bridge.log`, then use the local dashboard and
`python -m jobbot audit`. The orchestrator retries a dead bridge with the same
run checkpoint and bounded restart limit.

On macOS, RUN NOW and the crawler use background Chrome opening (`open -g` and
inactive tabs). They should not steal focus from the application you are using.

## Authentication or challenge

Use `OPEN_BIG3_LOGINS.command` and sign in normally in the same Chrome profile. JobBot intentionally records `AUTH_REQUIRED` or `CHALLENGED`, checkpoints the task, cools down that platform, and continues other platforms. It will not solve or bypass verification. Resume only after the ordinary-Chrome surface is manually clear; the next run positively rechecks readiness before acquiring that platform’s deferred work.

If a run has durable identities but missing detail content, use
`./REENRICH_SEARCH.command --platform linkedin` (or the appropriate platform)
after confirming the intended profile is ready. This is a user-invoked queue
operation; it preserves sightings, versions, and prior evidence.

## Search marked incomplete

Inspect `out/logs/run_<id>.log`, the extension dashboard, and `python -m jobbot audit`. `SAFETY_STOP` means the crawler could not prove platform exhaustion (for example, stable fingerprints without an explicit end). Resume later; do not relabel it exhausted.

## Database migration failure

Do not delete the database. Run SQLite integrity check through `python -m jobbot doctor`, preserve the most recent `data/backups/jobs_pre_v3_2_migration_*.sqlite3`, and inspect the exact exception. `IMPORT_EXISTING_DB.command` uses SQLite backup semantics for a validated import.

## Paths with spaces or iCloud

All launchers resolve their own directory and quote paths. Keep the Terminal process and Chrome open while a run is active. If iCloud temporarily evicts files, make the repository and database available offline before resuming.
