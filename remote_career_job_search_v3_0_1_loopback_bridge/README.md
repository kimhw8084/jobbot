# Remote Career Job Search Automation v3.1.0 — Platform-First Normal Chrome

This release uses the **normal Google Chrome profile you already use**, a small unpacked Chrome extension, and a local Python/SQLite engine. LinkedIn, Indeed, and Glassdoor are the primary discovery platforms. Employer ATS boards and public remote feeds are supplemental discovery/verification sources.

## Core search rule

The researched platform + keyword + Remote/date conditions define discovery. The runner walks the reachable result set, opens job details, records every encountered job, and lets SQLite deduplicate/version it. Production search tasks have **no strategic result-count cap**.

## v3.1.0 production-hardening changes

v3.0 and v3.0.1 used Chrome Native Messaging. On some macOS setups Chrome could load the extension but immediately report `Native host has exited.`. v3.1.0 removes that fragile dependency.

The `.command` launcher now starts a Python bridge itself on an ephemeral `127.0.0.1` port. A random token protects every request, and bounded reconnects tolerate service-worker suspension or temporary bridge loss. The Chrome extension talks only to that local bridge. When the launcher exits, the bridge exits too.

The browser controller leases tasks and persists search-page checkpoints, normalized task/result sightings, detail-read counts, page/scroll fingerprints, challenge isolation, and stop/resume state. SQLite remains the source of truth; extension memory is disposable.

## Mac installation

1. Unzip the package into a normal folder where you intend to keep it.
2. If you already have a real JobBot database, run `IMPORT_EXISTING_DB.command` and choose the old `jobs.sqlite3`.
3. Run:

```bash
chmod +x *.command
./INSTALL_MAC.command
```

4. Chrome opens `chrome://extensions`.
5. Turn on **Developer mode**.
6. **Remove the older JobBot v3 extension** if it points to a previous package folder.
7. Click **Load unpacked** and choose this package's `extension/` folder.
8. Confirm extension ID:

```text
jfdlmelgonjhgnabpbipjefgamedpgfb
```

9. In that same normal Chrome profile, sign into LinkedIn, Indeed, and Glassdoor normally.
10. After loading a new unpacked extension build, click the extension's **Reload** button once. The dashboard must show `Bridge: CONNECTED` when a launcher is running.

## First real acceptance test

Run:

```bash
./RUN_ACCEPTANCE_INDEED.command
```

The terminal should print something like:

```text
Local bridge ready on 127.0.0.1:54321
Indeed acceptance run #...
```

Chrome then opens the JobBot extension runner. It should no longer display `Native host has exited.` because Native Messaging is not used.

Acceptance mode runs three real Indeed searches with Remote + past 7 days and visits up to 20 detail pages per query **only as a test gate**:

1. patient enrollment specialist
2. patient access specialist
3. healthcare operations coordinator

Production mode does not use that result cap.

## Production search

After acceptance works:

```bash
./RUN_FULL_SEARCH.command
```

Deep mode generates the complete researched LinkedIn + Indeed + Glassdoor task universe. Each task remains persistent/checkpointed in SQLite and production `max_results` is NULL.

Normal daily/fast mode:

```bash
./RUN_FAST_SEARCH.command
```

## Database behavior

Every encountered job becomes one of:

- `NEW` — insert canonical job and first immutable version.
- `UNCHANGED` — preserve the job, update sightings/last seen.
- `UPDATED` — same canonical job, new immutable version + diff.
- `CLOSED` / `REOPENED` — lifecycle changes remain historically preserved.

Multiple keywords/platforms can intentionally rediscover the same job; that becomes multiple sightings of one canonical posting rather than duplicate applications.

## If the extension runner reports a bridge error

The launcher writes a run-specific log under:

```text
out/v3_bridge_run_<RUN_ID>.log
```

The authoritative human-readable event log is:

```text
out/logs/run_<RUN_ID>.log
```

Useful operational commands:

```bash
./RESUME_SEARCH.command       # requeue only unfinished tasks
./STOP_SEARCH.command         # persist an emergency stop; unfinished work stays resumable
./AUDIT.command                # coverage + funnel explanation, including an HTML audit
./DOCTOR.command               # local installation, security, ledger, and scoring checks
./OPEN_DASHBOARD.command       # open the cumulative ledger dashboard
```

The audit distinguishes exhausted, incomplete/safety-stop, challenged, auth-required, failed, queued, and running tasks. A low application reservoir is therefore attributable to retrieval coverage, missing descriptions, canonical verification, or qualification gates instead of being reported as an unexplained count.

This is the first diagnostic file to send back. You can also run:

```bash
./.venv/bin/python jobbot_bridge.py --self-test
./.venv/bin/python jobbot_v3.py install-check
```

Both should pass before browser traversal.

## Safety boundary

The extension/browser layer is read-only job-search automation. It does not submit applications, fill employer forms, upload résumés, solve CAPTCHAs, bypass Cloudflare/site challenges, manipulate browser fingerprints, rotate proxies, export cookies, or collect passwords. A challenge checkpoints/pauses that platform and the remaining sources can continue.
