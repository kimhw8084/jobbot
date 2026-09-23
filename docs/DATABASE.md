# Database

`data/jobs.sqlite3` is the permanent append-oriented ledger and is not tracked by Git. Connection policy enables WAL, foreign keys, a 10-second busy timeout, and `synchronous=FULL` by default.

Core tables include `jobs`, `source_occurrences`, `job_versions`, `job_diffs`, `browser_runs`, `browser_search_tasks`, `search_task_results`, `application_events`, `funnel_events`, and `source_verifications`. `schema_migrations` records sequential migration versions.

Migration 15 adds the production-integrity state contract. Search sightings
retain identity/card/enrichment state and recall priority; jobs retain content,
location, remote, application-destination, and provenance state. Browser tasks
retain requested versus observed URLs and context recovery attempts. Platform
runs retain readiness and user-action state. Existing identity-only rows that
were historically marked `COMPLETE` become `RETRYABLE`/missing-content rows,
while job versions and sightings remain unchanged. Use `jobbot re-enrich` only
after a user decision; it requeues incomplete content and never deletes history.

Migration 19 adds the CHG-113 evidence-readiness contract. Canonical jobs and
their source occurrences keep separate URL roles for discovery, board detail,
observed board apply action, employer job page, public ATS requisition, and
verified final application destination. `evidence_readiness_json` records
observed evidence, missing items, blockers, and both evidence and qualification
readiness decisions. Old actionable labels are held at `REVIEW` until the
versioned scoring pass recalculates them against the current strategy.

Migration 20 adds the CHG-114 frozen ordering fields to browser tasks and the
ordering-parameter record to browser runs. It also adds indexes for bounded
task/discovery/funnel quality aggregation. Search-quality metrics are derived
from the underlying task, result, job-readiness, event, and browser-event rows;
they are not copied into a materialized metric table. The additive provenance
summary is computed for API/dashboard/export output, while raw evidence JSON
remains in its existing job and occurrence fields. See
[`SEARCH_QUALITY.md`](SEARCH_QUALITY.md) for exact metric and attribution rules.

Important interpretations:

- `remote_required` is query intent. `remote_evidence_state=OBSERVED` requires observed detail evidence; missing location is `UNKNOWN`.
- `apply_url` is populated only for a distinct observed application destination. A LinkedIn/Indeed/Glassdoor board URL is not application verification.
- `verified_application_url` is populated only after employer/public ATS provenance is verified and identity matches. `application_destination_verification_state` distinguishes `VERIFIED_ATS`, `VERIFIED_EMPLOYER`, `OBSERVED_UNVERIFIED`, `BOARD_ONLY`, `MISSING`, and `IDENTITY_MISMATCH`; the compatibility `apply_destination_state` remains an observation-only field.
- `evidence_readiness_state='READY'` requires complete identity and substantive detail, supported requirements and responsibility/domain evidence, canonical employer/ATS verification, and a verified application destination. `qualification_readiness_state='READY'` also requires every CHG-170 qualification gate to pass. Only rows ready on both states enter application exports or daily plans.
- `detail_status=COMPLETE` is content-complete only when `content_state=COMPLETE`.

Before pending migrations on an existing database, JobBot runs `PRAGMA integrity_check`, creates a consistent backup with SQLite’s online backup API under `data/backups/`, applies idempotent migrations, and runs another integrity check. It never uses a naïve copy of an active SQLite file.

Lifecycle is `NEW`, `UNCHANGED`, `UPDATED`, `CLOSED`, and `REOPENED`. Unchanged sightings update last-seen/counts without a version. Meaningful source changes create immutable snapshots plus field-level old/new diffs, including requirement, credential, remote/location, state-eligibility, and schedule evidence extracted from changed descriptions. Canonical ATS disappearance needs two successful complete scans unless an explicit employer close state or canonical URL confirms closure.
