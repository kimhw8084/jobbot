# Operations

Run `python -m jobbot doctor` before live work. Inspect `python -m jobbot search-plan --mode deep --open` before starting. Complete a per-platform acceptance run before the full strategy.

The macOS full launcher uses `caffeinate -dimsu` only while JobBot runs. The
extension receives heartbeat, lease, and watchdog values from `config/runtime.toml`.
Persistent leases reclaim stale running tasks after crashes. Restart with
`python -m jobbot resume`; a small overlap is expected and is absorbed by dedupe.

For conservative local automation use `./RUN_CONTINUOUS.command`. Its durable
watch state reports `RUNNING`, `WAITING`, or `STOPPED` in the dashboard and
uses six-hour recent and daily deep cadences by default. Use `python -m jobbot
watch --once` only with an isolated acceptance database.

Use **Stop after current job** for orderly shutdown. Emergency stop marks an active task incomplete and resumable. Challenges and auth requirements are platform-local. Do not repeatedly revisit a challenge page; clear it normally, observe cooldown, then resume.

Run `python -m jobbot audit` to distinguish incomplete coverage, missing descriptions, challenge/auth state, qualification losses, and true market volume. Run `python -m jobbot export` for portable CSV, JSONL, and Markdown files. The local dashboard defaults to `http://127.0.0.1:8765/`.
