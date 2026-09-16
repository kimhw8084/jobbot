# v3.2.0 Live Acceptance

Complete the one-time bootstrap with `./INSTALL_MAC.command
--profile-directory "<final component from chrome://version Profile Path>"` and
load the exact machine-local stable extension path it prints. Do not load a
repository or Fabric worktree path. Before an acceptance run, use
`./SYNC_EXTENSION.command` after source changes and then
`./REFRESH_EXTENSION.command`; routine run launchers request and verify the
same stable-source/profile/build contract automatically. Run one platform at a
time:

```bash
python -m jobbot acceptance --platform indeed
python -m jobbot acceptance --platform linkedin
python -m jobbot acceptance --platform glassdoor
```

The acceptance run executes three sequential searches:

1. patient enrollment specialist
2. patient access specialist
3. healthcare operations coordinator

Constraints: Remote, past 7 days. Test cap: 20 detail pages/query. Production tasks always have a null result cap.

Pass criteria:

- authentication check does not redirect to login/challenge
- query 1 runs
- later result page/batch is reached when available
- query 2 starts automatically
- query 3 starts automatically
- extracted jobs enter SQLite
- rerunning does not create duplicate canonical jobs
- changed content becomes UPDATED
- interrupt/resume continues from a durable task checkpoint
- challenges or authentication requirements are recorded precisely and never bypassed

A challenge or authentication requirement is an externally blocked result, not a pass. The acceptance status remains persisted for a later normal-browser retry.
