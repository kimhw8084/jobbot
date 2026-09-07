# v3.1.0 Acceptance Test

Run `RUN_ACCEPTANCE_INDEED.command` after installing the extension and signing into Indeed in the same normal Chrome profile.

The acceptance run executes three sequential searches:

1. patient enrollment specialist
2. patient access specialist
3. healthcare operations coordinator

Constraints: Remote, past 7 days. Test cap: 20 detail pages/query. The cap is not used in production.

Pass criteria:

- authentication check does not redirect to login/challenge
- query 1 runs
- later result page/batch is reached when available
- query 2 starts automatically
- query 3 starts automatically
- extracted jobs enter SQLite
- rerunning does not create duplicate canonical jobs
- changed content becomes UPDATED
- challenges stop/pause safely rather than being bypassed
