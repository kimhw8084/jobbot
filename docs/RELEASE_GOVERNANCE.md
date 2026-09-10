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

The repository is a private single-user snapshot. Candidate evidence and resume
files remain local inputs and are not included in release archives. Databases,
logs, browser state, credentials, caches, and generated outputs are never
release inputs.
