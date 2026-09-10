# JobBot v3.2

JobBot is a local, read-only, remote-only career search ledger. LinkedIn, Indeed, and Glassdoor are traversed in the user’s ordinary installed Google Chrome session through a Manifest V3 extension and a token-authenticated `127.0.0.1` Python bridge. SQLite is authoritative; the extension never submits an application, fills a form, handles credentials, or bypasses a challenge.

The search objective is exhaustive coverage of the configured query/platform/age universe. The 10–20 application target is a human workload target, never a discovery cap.

## Fresh macOS installation

Requirements: macOS, Python 3.11 or newer, normal Google Chrome, and enough disk space for the cumulative ledger.

1. Double-click `INSTALL_MAC.command`, or run:

   ```bash
   cd "/path/containing spaces/jobbot"
   ./INSTALL_MAC.command
   ```

2. In `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select this repository’s `extension` directory. If the extension was already installed, click **Reload**. The stable extension ID is recorded in `config/EXTENSION_ID.txt`.
3. In that same normal Chrome profile, sign in normally to LinkedIn, Indeed, and Glassdoor. JobBot does not read or export cookies or passwords.
4. Run the controlled Indeed acceptance gate before a full search:

   ```bash
   ./RUN_ACCEPTANCE_INDEED.command
   ```

For a final release proof after reloading the unpacked extension, run
`./VALIDATE_PRODUCTION.command`. It creates timestamped isolated validation,
soak, and semi-production databases; it never opens `data/jobs.sqlite3`.

A site challenge or authentication page is an external incomplete state, not a pass. Clear it manually through normal browsing, wait for the configured cooldown when challenged, then resume.

## Today’s fast path

After the extension is loaded and the desired sites are signed in, use the
single production entry point:

```bash
./RUN_NOW.command
```

To start only the already-working LinkedIn path while leaving other platform
tasks checkpointed:

```bash
./RUN_NOW.command --platform linkedin
```

RUN NOW opens the local dashboard and crawler tabs in the background on macOS,
so Chrome does not take focus from the application you are using. The local
dashboard is normally `http://127.0.0.1:8765/`; it reads SQLite while the
crawler writes, so new cards, completed details, scores, and application
status changes appear during the run. Use **Actionable** for the decision
queue and **All discoveries** when auditing every genuine search-result card.
The **Live discoveries** panel is the durable intake receipt: it can contain a
card whose detail is still pending, while the Jobs table contains the
deduplicated, enriched canonical record.

The crawl intentionally reads details serially in a reused background detail
tab and commits each observation immediately. This is slower than opening
many tabs, but preserves normal-Chrome behavior, challenge safety, and
write-through durability. Stop with `./STOP_SEARCH.command`; resume with
`./RESUME_SEARCH.command` or the dashboard’s **Resume checkpoint** button.

The first production cycle is ordered GOLD fastest-door searches, then SILVER,
GROWTH, HEDGE, and DEEP_TAIL coverage. Independent band cadence reduces repeat
browser work without disabling any configured core definition. TODAY is a
precision queue: when the apply-ready reservoir is too small to maintain the
80% APPLY_NOW/APPLY_VOLUME target, it shows the real reservoir instead of
padding it with weaker roles.

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

## macOS launchers

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

The core portfolio is exactly 80% healthcare, 15% higher education/EdTech, and 5% content/bilingual/AI quality. Within healthcare it is 35% operations/access, 15% health information/documentation/QA, 20% quality/data/analytics, and 10% implementation/project/program operations. Transferable generic roles are a labeled fallback and do not displace the core portfolio.

## Tests and release

```bash
.venv/bin/python -m unittest discover -s tests -v
for file in extension/*.js; do node --check "$file"; done
.venv/bin/python scripts/build_release.py
```

The release builder excludes databases, resumes, browser state, credentials, logs, caches, virtual environments, and ZIP artifacts from the package. See [architecture](docs/ARCHITECTURE.md), [operations](docs/OPERATIONS.md), [database](docs/DATABASE.md), and [troubleshooting](docs/TROUBLESHOOTING.md).

### Release governance and privacy

This repository is a private, single-user JobBot snapshot. `config/candidate.toml`
and local resume files are intentionally excluded from release archives; the
working checkout supplies those owner-private inputs. Releases are built from
an immutable Git commit with `scripts/build_release.py`, not from arbitrary
working-tree files. The emitted provenance sidecar records the source commit,
tree, extension runtime digest, migration level, file hashes, and archive hash.

Before promoting a validated release, protect `main` with pull-request-only
integration, require the core OS/Python CI checks and release-integrity checks,
disable force-push and branch deletion, and retain recoverable release tags.
Production launchers reject a stale or dirty validated source tree.
