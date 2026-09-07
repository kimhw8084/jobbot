# v3.1.0 Architecture — Platform-First Normal Chrome

```text
Normal installed Google Chrome
        │
        ▼
JobBot Manifest V3 extension
        │
        │ tokenized HTTP RPC to 127.0.0.1 only
        ▼
jobbot_bridge.py
        │
        ▼
Python scoring + SQLite ledger
        │
        ├── canonical jobs
        ├── immutable versions / diffs
        ├── source sightings
        ├── platform search tasks/checkpoints
        └── application funnel
```

## Discovery hierarchy

1. LinkedIn — primary platform search
2. Indeed — primary platform search
3. Glassdoor — primary platform search
4. Employer ATS/career pages — canonical verification + supplemental discovery
5. Other boards/public remote feeds — supplemental recall

Employer watchlists never define or restrict the market universe.

## Search task model

Each `(platform, keyword, age window)` is a persistent task. Production tasks use `max_results = NULL`. The extension keeps traversing normal visible search results until no further result page/batch is reachable, an age/search boundary is met, a site challenge occurs, a real failure occurs, or the user explicitly stops the run.

Overlapping searches are expected and desirable. Dedupe happens after discovery.

The extension uses one stable search tab and one reusable detail tab per platform. For each task it records visible result identities first, then opens details and records the full posting before qualification. `search_task_results` normalizes task/source sightings without putting a giant JSON blob in the task row. Safety guards such as repeated page fingerprints, zero-new-result scroll attempts, and wall-clock checkpoints produce `incomplete`/`SAFETY_STOP`, never false `exhausted` coverage.

The controller state machine is:

```text
queued → running → exhausted
                 ├→ incomplete / stopped
                 ├→ challenged
                 ├→ auth_required
                 └→ failed
```

Tasks carry a renewable lease. A restart reclaims stale leases and resumes only queued/running/incomplete work, retaining page, scroll, URL, fingerprint, and last source-job checkpoints. A platform challenge changes only that platform's remaining tasks; other platforms continue.

## Local bridge

v3.1.0 deliberately does not depend on Chrome Native Messaging for Big-3 traversal. The launcher starts the bridge directly using the package's `.venv/bin/python`, which removes macOS host-registration and iCloud executable-path failure modes. Big-3 traversal does not use Playwright, Selenium, Puppeteer, Chrome-for-Testing, stealth, CAPTCHA solvers, proxy rotation, or fingerprint spoofing.

Security:
- binds only `127.0.0.1`;
- ephemeral OS-selected port;
- cryptographically random token per launcher run;
- token required for every RPC;
- 4 MiB request cap;
- bridge process ends with the launcher.

## Ledger and audit

`jobs.sqlite3` is append-oriented: canonical jobs, source occurrences, immutable versions, field-level diffs, application status, funnel events, browser runs, tasks, events, leases, and normalized task/result sightings are retained. Schema migration uses SQLite's online backup API and integrity checks. `jobbot.py audit` writes both `out/retrieval_audit.md` and `out/retrieval_audit.html`, with per-platform query, result, detail, duplicate, and task-status coverage.
