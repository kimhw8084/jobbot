# Troubleshooting

## Extension says disconnected or `Failed to fetch`

Start the run through `python -m jobbot run-now`, `python -m jobbot resume`, or a `.command` launcher. Those commands generate the per-run port/token and open `dashboard.html` in normal Chrome. Reload the unpacked extension after source updates. Confirm the ID matches `config/EXTENSION_ID.txt`.

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

Use `OPEN_BIG3_LOGINS.command` and sign in normally in the same Chrome profile. JobBot intentionally records `AUTH_REQUIRED` or `CHALLENGED`, checkpoints the task, cools down that platform, and continues other platforms. It will not solve or bypass verification.

## Search marked incomplete

Inspect `out/logs/run_<id>.log`, the extension dashboard, and `python -m jobbot audit`. `SAFETY_STOP` means the crawler could not prove platform exhaustion (for example, stable fingerprints without an explicit end). Resume later; do not relabel it exhausted.

## Database migration failure

Do not delete the database. Run SQLite integrity check through `python -m jobbot doctor`, preserve the most recent `data/backups/jobs_pre_v3_2_migration_*.sqlite3`, and inspect the exact exception. `IMPORT_EXISTING_DB.command` uses SQLite backup semantics for a validated import.

## Paths with spaces or iCloud

All launchers resolve their own directory and quote paths. Keep the Terminal process and Chrome open while a run is active. If iCloud temporarily evicts files, make the repository and database available offline before resuming.
