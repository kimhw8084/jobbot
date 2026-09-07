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

A site challenge or authentication page is an external incomplete state, not a pass. Clear it manually through normal browsing, wait for the configured cooldown when challenged, then resume.

## Canonical cross-platform CLI

After installation, run commands from the repository root:

```bash
.venv/bin/python -m jobbot doctor
.venv/bin/python -m jobbot search-plan --mode fast --open
.venv/bin/python -m jobbot search-plan --mode deep --open
.venv/bin/python -m jobbot run --mode fast
.venv/bin/python -m jobbot run --mode deep
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

## macOS launchers

- `RUN_FULL_SEARCH.command` — deep Big-3 search followed by supplemental feeds; uses `caffeinate` only for the process lifetime.
- `RUN_FAST_SEARCH.command` — overlapping seven-day P0/P1 search plus supplemental feeds.
- `RUN_PLATFORM_LINKEDIN.command`, `RUN_PLATFORM_INDEED.command`, `RUN_PLATFORM_GLASSDOOR.command` — isolate one primary platform.
- `RESUME_SEARCH.command` — resumes persistent unfinished checkpoints.
- `STOP_SEARCH.command` — requests an orderly stop after the current job; use `python -m jobbot stop --emergency` only when immediate checkpointing is necessary.
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
