# JobBot v3.1.0

## Production hardening

- Fixed the Big-3 acceptance deadlock caused by the extension's undefined authentication URL map.
- Added normal-Chrome MV3 auth probes, bounded loopback reconnects, two-tab traversal, per-platform challenge isolation, stop-after-current, emergency stop, and restart-safe task leases.
- Added persistent page/scroll checkpoints, normalized `search_task_results`, detail-read counters, duplicate sightings, progress heartbeats, and human-readable `out/logs/run_<run_id>.log` event logs.
- Kept production search unlimited (`max_results = NULL`); acceptance caps remain explicit test-only limits.
- Added `RESUME_SEARCH.command`, per-platform production launchers, `AUDIT.command`, `DOCTOR.command`, and safe `IMPORT_EXISTING_DB.command` SQLite backup/import handling.
- Added retrieval audit coverage for queued/running, exhausted, incomplete/safety-stop, challenged, auth-required, and failed tasks, with an HTML view linked from the ledger dashboard.

## Ledger and qualification fixes

- Preserved additive migration of existing jobs, versions, occurrences, application states, notes, and funnel events with pre-migration integrity-checked SQLite backups.
- Extended immutable snapshots/diffs for deadline, posting status, travel, timezone, and work authorization fields.
- Fixed hybrid text precedence and retained regression protections for SIS/Lean/DO boundaries, HTML-heavy descriptions, years requirements, offshore metadata, clinical roles, and unrelated senior technical/finance/security roles.
- Kept discovery recall-first: a targeted result is recorded before semantic qualification or enrichment.

## Known limitations

- LinkedIn, Indeed, and Glassdoor can change markup, pagination, authentication, or challenge behavior. Adapters use semantic/fallback selectors but still require a visible, signed-in normal Chrome window for live acceptance.
- Platform pages that issue a verification challenge are intentionally isolated and must be resumed after the user clears it normally.
- Canonical ATS verification remains supplemental and is not allowed to restrict Big-3 discovery.

## Verification evidence — 2026-09-07

- LinkedIn normal-Chrome acceptance verified authentication, detail hydration, full descriptions, result-batch/page advancement, multi-query progression, restart checkpoints, and duplicate/unchanged/updated ledger outcomes.
- Indeed normal-Chrome acceptance reached a genuine CAPTCHA; the run was isolated as `challenged`/`partial` without bypass attempts.
- Glassdoor normal-Chrome acceptance reported sign-in required; its tasks were isolated as `auth_required`/`partial`.
- The live acceptance slices were deliberately bounded. They do not claim that any platform's complete production universe was exhausted.
