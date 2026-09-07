# v3.0

- Replaced Playwright Big-3 traversal with ordinary Chrome + Manifest V3 extension.
- LinkedIn, Indeed and Glassdoor are first-class primary discovery sources.
- Full strategy expands to 486 persistent deep-mode Big-3 search tasks.
- Production `max_results` removed (`NULL`).
- Result-page scrolling and next-page/next-batch traversal added.
- Full job-detail extraction before scoring.
- Search checkpoints and automatic resume added.
- Platform-specific authentication state added.
- Challenge on one platform pauses that platform while remaining platforms continue.
- Existing SQLite canonical jobs/version/diff logic reused.
- ATS/public feeds demoted to supplemental discovery/verification after Big-3 traversal.
- Added Mac install, login helper, acceptance runner, full runner, fast runner, stop/dashboard launchers.
