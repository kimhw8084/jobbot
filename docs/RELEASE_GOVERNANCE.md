# Release governance

JobBot production launch is tied to one immutable validated source identity.
The normal release sequence is:

1. Build and verify the archive from a pushed commit with `scripts/build_release.py --commit <sha>`.
2. Complete the isolated deterministic and normal-Chrome validation ladder.
3. Require the validation report to match the exact commit, Git tree, extension runtime digest, and pushed upstream.
4. Integrate to `main` through a pull request, preserving the validated commit history.

Repository administration should protect `main`, require the macOS/Linux/Windows
core CI matrix and release-integrity checks, prohibit force pushes and branch
deletion, and retain tags or other recoverable release references.

## Source cleanliness policy

Production validation and the production launch guard use Git's complete
porcelain status (`--untracked-files=all`) as the source-of-truth policy.
Tracked modifications, staged changes, deletions, and every nonignored
untracked path block immutable-source validation. There is no filename,
suffix, duplicate-hash, or historical-copy exemption for source-like files
under `src/`, `extension/`, `scripts/`, `config/`, `docs/`, or `tests/`.

The repository `.gitignore` contains only narrowly named local-state classes:
databases/backups, output/log/cache/browser-profile state, resumes, and other
generated runtime artifacts. Those ignored paths may exist locally because they
are not source identity and cannot enter a release: release archives are built
from committed Git objects, not the mutable filesystem. A new local artifact
must be placed in one of those documented runtime classes only when it is
genuinely runtime state; otherwise it remains a blocking untracked source
input.

Cloud-synced duplicate copies in source or test directories remain blocking
untracked state. They must be intentionally removed, relocated to a supported
local-state directory, or versioned by the owner before production validation.

The repository is a private single-user snapshot. Candidate evidence and resume
files remain local inputs and are not included in release archives. Databases,
logs, browser state, credentials, caches, and generated outputs are never
release inputs.
