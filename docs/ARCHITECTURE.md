# Architecture

## Runtime boundary

The primary discovery path is ordinary installed Google Chrome → Manifest V3 extension → token-authenticated loopback bridge → Python/SQLite. The bridge binds only to `127.0.0.1`, chooses an available high port per run, requires a cryptographically random token on every request, and accepts the stable extension origin. SQLite is the source of truth after extension service-worker suspension or process failure.

Routine extension freshness uses that same control plane. The repository-owned
extension is first copied by `sync-extension` to the machine-local stable
deployment path, whose marker records the exact source content identity,
`version_name`, stable ID, and integrated source head. Chrome is bootstrapped
once from that path; a separate machine-local binding records the selected
ordinary-Chrome `--profile-directory`. Launchers target that profile with
supported Chrome command-line behavior, including after Chrome restarts; they
never inspect or modify Chrome profile files. A launcher or the
`refresh-extension` command then records an idempotent SQLite refresh request;
the extension dashboard asks its service worker to call
`chrome.runtime.reload()` when the loaded code is stale, and the bridge
confirms the request only after receiving the loaded `manifest.json`
`version_name` plus the stable deployment identity. Requests are fail-closed
for absent/unreachable, wrong-profile, wrong-source, or wrong-build extensions
and are rejected while any browser run is active, so refresh cannot interrupt a
checkpointed crawl.

The extension uses one persistent search tab and one reused detail tab per task. A result card is committed to `search_task_results` before age or semantic qualification. The detail is then read and committed before normalization, canonicalization, requirement extraction, remote/employment/credential gates, and scoring. The bridge rejects empty-substance detail payloads and error/login/challenge/interstitial surfaces before they can create or mutate canonical content.

### Evidence and enrichment state

`search_task_results` is the durable receipt layer. `identity_status=PERSISTED`
means source identity was captured; `card_metadata_status` describes whether
company/location/posted card fields were observed; `detail_status` and
`content_state` describe enrichment (`PENDING`, `RUNNING`, `PARTIAL`,
`RETRYABLE`, `DEFERRED_RECALL`, `EXTERNAL_BLOCKED`, `FAILED`, or complete).
Historical `COMPLETE` rows with missing descriptions are migrated to
re-enrichment-needed states without deleting sightings or `job_versions`.

On `jobs`, `location_evidence_state`, `remote_evidence_state`, and
`apply_destination_state` distinguish observed evidence from search intent or
unknown values. A source-board URL is never copied into `apply_url` merely
because it is canonical. The existing `legacy_engine.recall_prefilter()` only
orders expensive detail work; recall negatives remain durable and sampled for
later QA. The explicit `re-enrich` command switches a user-requested run to
`enrichment_mode=all`.

Requested and observed search URLs are stored separately. Redirects and lost
query context are `INCOMPLETE` recovery states, never exhaustion. Bridge
retries use the same request identity and bounded attempts for transient local
receiver/page-load failures.

Big-3 browsing never uses Playwright, Selenium, Puppeteer, Chrome-for-Testing, cookie export, stealth, CAPTCHA solving, proxy evasion, or fingerprint alteration.

## Modules

- `config.py`, `candidate.py`, `search_plan.py`: typed executable configuration and plan compilation.
- `db.py`, `migrations/`: database connection policy, online backups, integrity checks, sequential schema changes.
- `ledger.py`, `canonical.py`, `versioning.py`: canonical jobs, cross-source occurrences, immutable versions, field diffs, and lifecycle.
- `requirements.py`, `remote.py`, `employment.py`, `scoring.py`: deterministic qualification pipeline.
- `browser_tasks.py`, `bridge/`, `sources/browser.py`: persistent task queue, loopback RPC, platform adapter contracts, static fixture parser.
- `dashboard.py`, `audit.py`, `exports.py`, `application.py`, `funnel.py`: local warehouse UX and application learning.
- `legacy_engine.py`, `legacy_core.py`: retained, tested supplemental feed/ATS retrieval and proven ledger/scoring implementation behind focused public modules. These do not automate Big-3 browsing.

## Failure semantics

`EXHAUSTED` requires explicit platform end evidence or an age boundary in a newest-sorted search. Repeated fingerprints, zero-new scrolls, watchdog stalls, or ambiguous missing pagination produce `INCOMPLETE` with `SAFETY_STOP`, never false exhaustion. A challenge or auth failure checkpoints and isolates only its platform; other platforms continue.

Platform readiness is durable and independent: `verified`, `sign_in_required`,
`challenged_cooldown`, `user_action_required`, `retryable`, and `resumed` are
shown in the dashboard. Indeed and Glassdoor require positive authenticated
session evidence and a validated search surface. The recovery flow is ordinary
Chrome only: clear a challenge or sign in manually, then use Resume checkpoint
so the platform is positively rechecked while other platform work remains
checkpointed.
