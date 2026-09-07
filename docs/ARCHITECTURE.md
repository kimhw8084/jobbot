# Architecture

## Runtime boundary

The primary discovery path is ordinary installed Google Chrome → Manifest V3 extension → token-authenticated loopback bridge → Python/SQLite. The bridge binds only to `127.0.0.1`, chooses an available high port per run, requires a cryptographically random token on every request, and accepts the stable extension origin. SQLite is the source of truth after extension service-worker suspension or process failure.

The extension uses one persistent search tab and one reused detail tab per task. A result card is committed to `search_task_results` before age or semantic qualification. The detail is then read and committed before normalization, canonicalization, requirement extraction, remote/employment/credential gates, and scoring.

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
