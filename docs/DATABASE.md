# Database

`data/jobs.sqlite3` is the permanent append-oriented ledger and is not tracked by Git. Connection policy enables WAL, foreign keys, a 10-second busy timeout, and `synchronous=FULL` by default.

Core tables include `jobs`, `occurrences`, `job_versions`, `job_diffs`, `browser_runs`, `browser_search_tasks`, `search_task_results`, `application_events`, `funnel_events`, and `source_verifications`. `schema_migrations` records sequential migration versions.

Before pending migrations on an existing database, JobBot runs `PRAGMA integrity_check`, creates a consistent backup with SQLite’s online backup API under `data/backups/`, applies idempotent migrations, and runs another integrity check. It never uses a naïve copy of an active SQLite file.

Lifecycle is `NEW`, `UNCHANGED`, `UPDATED`, `CLOSED`, and `REOPENED`. Unchanged sightings update last-seen/counts without a version. Meaningful source changes create immutable snapshots plus field-level old/new diffs. Canonical ATS disappearance needs two successful complete scans unless an explicit employer close state or canonical URL confirms closure.
