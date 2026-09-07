# Migrating an Existing Ledger

Use `IMPORT_EXISTING_DB.command` or copy the old `jobs.sqlite3` to `data/jobs.sqlite3`.

v3 browser schema is additive. It preserves existing `jobs`, `job_versions`, source sightings, statuses, notes, application events, and funnel history.

Validated against the supplied 5,029-job ledger: v3 added 486 browser search tasks without changing the 5,029 existing canonical job count.

Do not copy old Playwright `.browser-profile` folders. v3 uses the ordinary Chrome profile in which the extension is installed.
