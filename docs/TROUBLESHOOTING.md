# Troubleshooting

## Extension says disconnected

Start the run through `python -m jobbot run`, `python -m jobbot resume`, or a `.command` launcher. Those commands generate the per-run port/token and open `dashboard.html` in normal Chrome. Reload the unpacked extension after source updates. Confirm the ID matches `config/EXTENSION_ID.txt`.

## Authentication or challenge

Use `OPEN_BIG3_LOGINS.command` and sign in normally in the same Chrome profile. JobBot intentionally records `AUTH_REQUIRED` or `CHALLENGED`, checkpoints the task, cools down that platform, and continues other platforms. It will not solve or bypass verification.

## Search marked incomplete

Inspect `out/logs/run_<id>.log`, the extension dashboard, and `python -m jobbot audit`. `SAFETY_STOP` means the crawler could not prove platform exhaustion (for example, stable fingerprints without an explicit end). Resume later; do not relabel it exhausted.

## Database migration failure

Do not delete the database. Run SQLite integrity check through `python -m jobbot doctor`, preserve the most recent `data/backups/jobs_pre_v3_2_migration_*.sqlite3`, and inspect the exact exception. `IMPORT_EXISTING_DB.command` uses SQLite backup semantics for a validated import.

## Paths with spaces or iCloud

All launchers resolve their own directory and quote paths. Keep the Terminal process and Chrome open while a run is active. If iCloud temporarily evicts files, make the repository and database available offline before resuming.
