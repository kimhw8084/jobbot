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

CHG-112 integrity checks also require that card identity persistence is not
reported as detail completeness: missing descriptions stay pending/partial,
missing locations stay unknown, and board URLs do not become verified apply
destinations. The dashboard must show platform readiness independently, and
the stop latch must not lease a subsequent task. If Indeed remains on CAPTCHA
or Glassdoor remains signed out, record that exact external blocker and verify
the manual recovery/recheck path; do not claim a positive live result.

For LinkedIn, a direct `/jobs/view/<id>/` response can be an authenticated
identity shell without the posting body. The runner compares that bounded
standalone diagnostic with the same card selected in the authenticated
`/jobs/search/?currentJobId=<id>` pane and uses the pane only when it exposes
the evidenced `About the job`/description section. A verification badge's
visually-hidden accessory text is excluded from the card title; missing card
company, location, or posted metadata remains unknown. Detail diagnostics are
bounded and contain no page HTML, credentials, cookies, or browser-session
material.
