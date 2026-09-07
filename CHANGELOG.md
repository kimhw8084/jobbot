# Changelog

## 3.2.0 — Production consolidation

- Consolidated the version-named subtree into one repository-root `src/jobbot` package and canonical `python -m jobbot` CLI.
- Removed generated/private virtual environments, SQLite ledgers/backups, caches, outputs, resumes, browser state, and release ZIPs from Git tracking without deleting the local ledger.
- Split strategy, candidate evidence, and runtime configuration; locked the core 35/15/20/10/15/5 allocation and all specified title families.
- Added typed, visible JSON/CSV/HTML search-plan compilation with unlimited production tasks.
- Added sequential schema migrations, SQLite backup/integrity handling, WAL/foreign-key/busy-timeout/durability configuration, normalized task results, field diffs, source verification, application, and funnel events.
- Hardened the normal-Chrome MV3 state machine with centralized selectors, write-through result/detail commits, two-tab traversal, 20-second heartbeat, leases, checkpoint resume, bounded bridge retries, challenge cooldown/isolation, and honest safety-stop semantics.
- Added server-paginated local dashboard, persistent application actions/history, retrieval audit, portable exports, funnel analysis, release builder, and macOS/Linux/Windows launchers.
- Added static Big-3 fixtures and deterministic unit/integration/regression tests, including prior false positives and parser boundary defects.

Historical release notes are retained under `docs/history/`.
