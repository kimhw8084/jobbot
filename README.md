# JobBot v3.2

JobBot is a local, read-only, remote-only career search ledger. Managed/API
acquisition through acquisition-v2's shared provider-neutral boundary is the
selected Big-3 production direction. The Bright Data Jobs adapter is integrated
behind that boundary but remains unqualified and is not production-approved
until bounded qualification for LinkedIn, Indeed, and Glassdoor passes
independent review. CHG-275 remains open pending local account configuration
and an explicit maximum-record or dollar cap. SQLite remains authoritative;
JobBot does not submit applications, fill forms, handle credentials, or bypass
challenges.

The search objective is exhaustive coverage of the configured query/platform/age universe. The 10–20 application target is a human workload target, never a discovery cap.

RUN_NOW has not switched and currently remains on the retained legacy
Chrome/Manifest V3 compatibility route. That route is frozen for compatibility
only and is not the selected production direction. Public ATS/employer
retrieval remains a separate verification and enrichment path. See
[Bright Data adapter setup](docs/BRIGHTDATA_PROVIDER.md).

## Legacy RUN_NOW compatibility setup (Chrome/MV3)

The requirements and steps below apply only to the retained legacy RUN_NOW
compatibility route: macOS, Python 3.11 or newer, normal Google Chrome, and
enough disk space for the cumulative ledger.

The requirements above apply to the retained legacy crawler.

1. Open `chrome://version` in the ordinary Chrome profile that will hold the
   JobBot extension. Record the final component of **Profile Path** (for
   example, `Default` or `Profile 1`). Double-click `INSTALL_MAC.command`, or
   run it with that exact directory:

   ```bash
   cd "/path/containing spaces/jobbot"
   ./INSTALL_MAC.command --profile-directory "Profile 1"
   ```

2. In the targeted profile’s `chrome://extensions`, enable Developer mode,
   choose **Load unpacked**, and select the exact machine-local path printed by
   the bootstrap (macOS: `~/Library/Application Support/JobBot/extension`).
   Never load a repository or Fabric worktree `extension` directory. The
   stable extension ID remains recorded in `config/EXTENSION_ID.txt`.
3. In that same normal Chrome profile, sign in normally to LinkedIn, Indeed, and Glassdoor. JobBot does not read or export cookies or passwords.
4. After integrated source changes, run `./SYNC_EXTENSION.command` to copy the
   exact current extension source and identity to the stable path. A routine
   `./REFRESH_EXTENSION.command` performs that sync and then requests
   `chrome.runtime.reload()` through the authenticated loopback bridge; it exits
   successfully only after the intended installed instance reports a runtime
   build exactly equal to `manifest.json` `version_name`. Run/resume launchers
   perform the same freshness gate automatically.
5. Run the controlled Indeed acceptance gate before a full search:

   ```bash
   ./RUN_ACCEPTANCE_INDEED.command
   ```

For a final release proof after the canonical extension is loaded, run
`./VALIDATE_PRODUCTION.command`. It creates timestamped isolated validation,
soak, and semi-production databases; it never opens `data/jobs.sqlite3`.

A site challenge or authentication page is an external incomplete state, not a pass. Clear it manually through normal browsing, wait for the configured cooldown when challenged, then resume.

Discovery and enrichment are separate durable stages. Every scoped card identity
is retained in `search_task_results` first; `identity_status=PERSISTED` does not
mean its detail is complete. `content_state` is `MISSING`, `PARTIAL`, or
`COMPLETE`, and only the last state is content-complete. Use the user-invoked
maintenance path to re-read existing incomplete identities without deleting
sightings or immutable history:

```bash
./REENRICH_SEARCH.command [--run-id ID] [--platform linkedin]
```

`remote_required` is search intent, not observed evidence. Missing locations
remain unknown, and a board detail URL is a source occurrence rather than a
verified application destination. Dashboard scores are triage signals until
substantive detail evidence is complete.

## Current RUN_NOW compatibility behavior

RUN_NOW has not switched to managed/API acquisition. After the extension is
loaded and the desired sites are signed in, this existing local compatibility
entry point continues to invoke the frozen Chrome/MV3 route:

```bash
./RUN_NOW.command
```

To start only the already-working LinkedIn path while leaving other platform
tasks checkpointed:

```bash
./RUN_NOW.command --platform linkedin
```

RUN NOW opens the localhost Python dashboard and crawler windows in the
background on macOS, so Chrome does not take focus from the application you are
using. A short-lived extension bootstrap page handles the authenticated
handshake and closes automatically. The local
dashboard is normally `http://127.0.0.1:8765/`; it reads SQLite while the
crawler writes, so new cards, completed details, scores, and application
status changes appear during the run. Use **Actionable** for the decision
queue and **All discoveries** when auditing every genuine search-result card.
The **Live discoveries** panel is the durable intake receipt: it can contain a
card whose detail is still pending, while the Jobs table contains the
deduplicated, enriched canonical record.

The browser commands above are frozen compatibility-only behavior, not the
selected Big-3 production direction. Future browser-agent services may be
fallback providers only and cannot be the sole authority for Actionable
evidence. Application execution remains human-only.

For an offline JSON or JSONL fixture, point JobBot at a disposable database:

    JOBBOT_DATABASE_PATH=/tmp/jobbot-acquisition.sqlite3 python -m jobbot acquire --provider jsonl-file --path fixture.jsonl

Each file-provider task needs a separate completion row with non-empty
completion_evidence before it can be marked exhausted. Bright Data setup,
offline preflight, runtime schema keys, and the qualification boundary are
documented in [Bright Data adapter setup](docs/BRIGHTDATA_PROVIDER.md).

On the retained compatibility route, the crawl uses one serial worker and one
reused search tab per active Big-3 platform. It selects each card into that
platform's embedded detail pane and commits the observation immediately.
Standalone per-job tabs are not routine crawl behavior. Stop with
`./STOP_SEARCH.command`; resume with
`./RESUME_SEARCH.command` or the dashboard’s **Resume checkpoint** button.

## Canonical cross-platform CLI

After installation, run commands from the repository root:

```bash
.venv/bin/python -m jobbot doctor
.venv/bin/python -m jobbot search-plan --mode fast --open
.venv/bin/python -m jobbot search-plan --mode deep --open
.venv/bin/python -m jobbot run --mode fast
.venv/bin/python -m jobbot run --mode deep
.venv/bin/python -m jobbot run-now
.venv/bin/python -m jobbot watch
.venv/bin/python -m jobbot watch --once
.venv/bin/python -m jobbot validate-production
.venv/bin/python -m jobbot resume
.venv/bin/python -m jobbot re-enrich --platform linkedin
.venv/bin/python -m jobbot stop
.venv/bin/python -m jobbot stop --emergency
.venv/bin/python -m jobbot dashboard
.venv/bin/python -m jobbot audit
.venv/bin/python -m jobbot export
.venv/bin/python -m jobbot application mark J123 APPLIED --notes "Applied on employer site"
.venv/bin/python -m jobbot application note J123 "Follow up next week"
.venv/bin/python -m jobbot application history J123
.venv/bin/python -m jobbot funnel
```

Linux and Windows wrappers are available as `scripts/jobbot.sh`, `scripts/jobbot.ps1`, and `scripts/jobbot.bat`. The Python module is authoritative.

`./RUN_CONTINUOUS.command` runs the durable local watch loop. It executes due
recent/deep phases, checkpoints the cycle in SQLite, waits conservatively, and
resumes only due work. `--once` is useful for an isolated acceptance database;
do not point unattended tests at `data/jobs.sqlite3`.

## Legacy Chrome/MV3 macOS launchers

- `RUN_FULL_SEARCH.command` — deep Big-3 search followed by supplemental feeds; uses `caffeinate` only for the process lifetime.
- `RUN_FAST_SEARCH.command` — overlapping seven-day P0/P1 search plus supplemental feeds.
- `RUN_PLATFORM_LINKEDIN.command`, `RUN_PLATFORM_INDEED.command`, `RUN_PLATFORM_GLASSDOOR.command` — isolate one primary platform.
- `RESUME_SEARCH.command` — resumes persistent unfinished checkpoints.
- `STOP_SEARCH.command` — requests an orderly stop after the current job; use `python -m jobbot stop --emergency` only when immediate checkpointing is necessary.
- `VALIDATE_PRODUCTION.command` — runs the bounded micro-validation, 15-minute soak, and 30–60-minute isolated semi-production proof; it never uses the production database.
- `OPEN_DASHBOARD.command`, `AUDIT.command`, `DOCTOR.command`, `IMPORT_EXISTING_DB.command` — local operations.

## Configuration and data safety

- `config/strategy.toml` is the sole executable search taxonomy and allocation source.
- `config/candidate.toml` is the factual candidate evidence graph and resume routing registry.
- `config/runtime.toml` contains local runtime, durability, cooldown, and source settings.
- `data/jobs.sqlite3` is append-oriented local state and is intentionally ignored by Git.
- Migrations run integrity checks before and after, and use SQLite’s backup API before changing an existing schema.
- Logs are written under `out/logs/`; exports and plan/audit reports are under `out/`.

The durable career architecture is 55% Healthcare Regulated Operations & Data Quality, 25% Healthcare Analytics/Project/Implementation, 15% Education/Learning/EdTech, and 5% Bilingual AI/Content Quality. Active live retrieval uses a separate versioned profile: its calibration order does not set permanent query quotas, and deep/staged plans recall every enabled credible family. See [Career strategy](docs/CAREER_STRATEGY.md), [Search plan](docs/SEARCH_PLAN.md), and [Search quality](docs/SEARCH_QUALITY.md).

## Tests and release

```bash
.venv/bin/python -m unittest discover -s tests -v
for file in extension/*.js; do node --check "$file"; done
.venv/bin/python scripts/build_release.py
```

The release builder excludes databases, resumes, browser state, credentials, logs, caches, virtual environments, and ZIP artifacts from the package. See [architecture](docs/ARCHITECTURE.md), [operations](docs/OPERATIONS.md), [database](docs/DATABASE.md), and [troubleshooting](docs/TROUBLESHOOTING.md).
