# Operations

The normal-Chrome Big-3 crawler commands below are frozen legacy/deprecated
pending acquisition-provider migration. R1 leaves RUN NOW unchanged for
compatibility and selects no managed provider for production. Public
ATS/employer retrieval remains a separate verification/enrichment path.

For offline JSON or JSONL fixture ingestion, use an explicit disposable database path:

    JOBBOT_DATABASE_PATH=/tmp/jobbot-acquisition.sqlite3 python -m jobbot acquire --provider jsonl-file --path fixture.jsonl

The file provider only marks a task exhausted when the file contains explicit
completion evidence for that task. Missing proof, partial batches, timeouts,
and provider errors leave tasks incomplete/retryable. R1 has no live managed
provider transport and never falls back to Chrome.

Run `python -m jobbot doctor` before live work. Inspect `python -m jobbot search-plan --mode deep --open` before starting. Complete a per-platform acceptance run before the full strategy.

Complete the one-time bootstrap with `./INSTALL_MAC.command
--profile-directory "<final component from chrome://version Profile Path>"`.
It deploys the exact integrated extension source to the machine-local stable
path printed by the command and records the selected profile in a separate
machine-local binding file. Load unpacked from that stable path only. The
repository and Fabric worktree paths are never Chrome extension sources.

After bootstrap, use `./SYNC_EXTENSION.command` to deploy source changes and
`./REFRESH_EXTENSION.command` for a maintenance refresh. Refresh uses the
authenticated loopback bridge and succeeds only after the targeted installed
instance reports a runtime `manifest.json` `version_name` exactly equal to the
integrated source. The run, resume, and validation launchers request the same
gate automatically. An absent, unreachable, stale, wrong-profile, or
wrong-source extension is a failed operation, not a success. JobBot never
reads or edits Chrome profile files.

The macOS full launcher uses `caffeinate -dimsu` only while JobBot runs. The
extension receives heartbeat, lease, and watchdog values from `config/runtime.toml`.
Persistent leases reclaim stale running tasks after crashes. Restart with
`python -m jobbot resume`; a small overlap is expected and is absorbed by dedupe.

The durable Python dashboard at `http://127.0.0.1:8765/` is the sole persistent
operator UI. Run/resume/watch/refresh uses an inactive extension bootstrap page
only for the authenticated handshake; it closes automatically and is never a
status console. Each active platform has its own worker lane and checkpoint.
Focus window foregrounds the already-owned Chrome window. It never opens a new
tab or steals the user's current window during normal operation.

For a remote iPhone or browser, forward the dashboard through a user-created
SSH tunnel, such as `ssh -N -L 8765:127.0.0.1:8765 mac-host`, and open the
forwarded localhost URL. The tunnel does not move browser control or visual
challenge handling off the Mac, and JobBot does not expose the dashboard on a
LAN/public interface.

For conservative local automation use `./RUN_CONTINUOUS.command`. Its durable
watch state reports `RUNNING`, `WAITING`, or `STOPPED` in the dashboard and
uses six-hour recent and daily deep cadences by default. Use `python -m jobbot
watch --once` only with an isolated acceptance database.

Use **Stop after current job** for orderly shutdown. The stop latch finishes
only the already-active atomic detail and prevents any next task acquisition;
queued work remains durable. Emergency stop marks an active task incomplete and
resumable. Challenges and auth requirements are platform-local. Do not
repeatedly revisit a challenge page or attempt to bypass it; clear it normally,
then use Resume checkpoint to trigger a positive readiness recheck.

The observation cache at `data/crawl_observations.sqlite3` is optional and
safe to delete. It never supplies application/funnel state and never contains
cookies, credentials, profile data, challenge material, or tokens. Fresh
acceptance samples intentionally bypass the cache.

Readiness is fail-closed without false-negative sign-out. A missing sign-in
selector is not authentication evidence; the requested search surface is
probed, and scoped cards or a verified empty state can establish readiness.
An explicit login/authwall records sign-in required, while a CAPTCHA/challenge
is recorded as `challenged_cooldown` with deferred tasks. Sign in or clear the
challenge manually in the intended ordinary Chrome profile, then resume that
checkpoint. Use `./REENRICH_SEARCH.command` when a prior run captured durable
identities but needs user-invoked content enrichment.

Run `python -m jobbot audit` to distinguish incomplete coverage, missing descriptions, challenge/auth state, qualification losses, and true market volume. Run `python -m jobbot export` for portable CSV, JSONL, and Markdown files. The export set includes separate `live_discoveries.csv` intake receipts and `application_history.csv` human-event history. The local dashboard defaults to `http://127.0.0.1:8765/`.
