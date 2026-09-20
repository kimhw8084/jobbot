# Architecture

## Runtime boundary

The primary discovery path is ordinary installed Google Chrome → Manifest V3 extension → token-authenticated loopback bridge → Python/SQLite. The bridge binds only to `127.0.0.1`, chooses an available high port per run, requires a cryptographically random token on every request, and accepts the stable extension origin. SQLite is the source of truth after extension service-worker suspension or process failure.

Routine extension freshness uses that same control plane. The repository-owned
extension is first copied by `sync-extension` to the machine-local stable
deployment path, whose marker records the exact source content identity,
`version_name`, stable ID, and integrated source head. Chrome is bootstrapped
once from that path; a separate machine-local binding records the selected
ordinary-Chrome `--profile-directory`. Launchers target that profile with
supported Chrome command-line behavior, including after Chrome restarts; they
never inspect or modify Chrome profile files. A launcher or the
`refresh-extension` command then records an idempotent SQLite refresh request;
a short-lived inactive `bootstrap.html` page asks the service worker to perform
the handshake, then closes automatically. The persistent operator console is
the localhost Python dashboard, not an extension page. The service worker calls
`chrome.runtime.reload()` when the loaded code is stale, and the bridge
confirms the request only after receiving the loaded `manifest.json`
`version_name` plus the stable deployment identity. Requests are fail-closed
for absent/unreachable, wrong-profile, wrong-source, or wrong-build extensions
and are rejected while any browser run is active, so refresh cannot interrupt a
checkpointed crawl.

The transient bootstrap has a backward-compatibility contract: build N+1 must
be able to upgrade a resident build N without any N+1-only message handler.
It sends `JOBBOT_CONFIGURE_BRIDGE`, then `JOBBOT_REFRESH_EXTENSION`, which are
the stable predecessor messages. When the response requires a reload it first
persists `jobbot_bridge_config`, `jobbot_expected_extension_build`,
`jobbot_refresh_id`, and (for a queued run) `jobbot_active_run_id`, then invokes
the supported extension `chrome.runtime.reload()`. After reload, the new
service worker confirms its exact build/deployment identity; it resumes the
queued run through the existing idempotent startup path or completes
maintenance without starting a run. `JOBBOT_BOOTSTRAP_START` is not a required
upgrade path. A durable `jobbot_bootstrap_handoff` marker makes replayed
transient pages close without issuing a second reload or start.

Each active Big-3 platform owns one serial worker, one ordinary Chrome window,
and one reused search tab. Cards are committed to `search_task_results` before
detail work. Detail acquisition selects the card in that search page and waits
for the embedded pane; identity, pane provenance, and substantive description
must be proven before the detail is committed. Standalone per-job tabs are only
available to an explicitly user-invoked re-enrichment compatibility path. The
bridge rejects empty-substance detail payloads and
error/login/challenge/interstitial surfaces before they can create or mutate
canonical content.

`data/crawl_observations.sqlite3` is a disposable, safe observation cache, not
the ledger. It contains normalized card fields, complete detail evidence,
hashes, freshness, provenance, and source-build/schema metadata only. Cache
failure falls back to fresh pane crawling; acceptance/validation samples bypass
it. SQLite `browser_search_tasks` leases and `browser_platform_runs` remain the
authoritative checkpoint/runtime state.

### Evidence and enrichment state

`search_task_results` is the durable receipt layer. `identity_status=PERSISTED`
means source identity was captured; `card_metadata_status` describes whether
company/location/posted card fields were observed; `detail_status` and
`content_state` describe enrichment (`PENDING`, `RUNNING`, `PARTIAL`,
`RETRYABLE`, `DEFERRED_RECALL`, `EXTERNAL_BLOCKED`, `FAILED`, or complete).
Historical `COMPLETE` rows with missing descriptions are migrated to
re-enrichment-needed states without deleting sightings or `job_versions`.

On `jobs`, `location_evidence_state`, `remote_evidence_state`, and
`apply_destination_state` distinguish observed evidence from search intent or
unknown values. A source-board URL is never copied into `apply_url` merely
because it is canonical. The existing `legacy_engine.recall_prefilter()` only
orders expensive detail work; recall negatives remain durable and sampled for
later QA. The explicit `re-enrich` command switches a user-requested run to
`enrichment_mode=all`.

Requested and observed search URLs are stored separately. Redirects and lost
query context are `INCOMPLETE` recovery states, never exhaustion. Bridge
retries use the same request identity and bounded attempts for transient local
receiver/page-load failures.

Big-3 browsing never uses Playwright, Selenium, Puppeteer, Chrome-for-Testing, cookie export, stealth, CAPTCHA solving, proxy evasion, or fingerprint alteration.

## Modules

- `config.py`, `candidate.py`, `search_plan.py`: typed executable configuration and plan compilation.
- `db.py`, `migrations/`: database connection policy, online backups, integrity checks, sequential schema changes.
- `ledger.py`, `canonical.py`, `versioning.py`: canonical jobs, cross-source occurrences, immutable versions, field diffs, and lifecycle.
- `requirements.py`, `remote.py`, `employment.py`, `scoring.py`: deterministic qualification pipeline.
- `browser_tasks.py`, `bridge/`, `sources/browser.py`: persistent task queue, loopback RPC, platform adapter contracts, static fixture parser.
- `dashboard.py`, `audit.py`, `exports.py`, `application.py`, `funnel.py`: local warehouse UX and application learning.
- `legacy_engine.py`, `legacy_core.py`: retained, tested supplemental feed/ATS retrieval and proven ledger/scoring implementation behind focused public modules. These do not automate Big-3 browsing.

## Failure semantics

`EXHAUSTED` requires explicit platform end evidence or an age boundary in a newest-sorted search. Repeated fingerprints, zero-new scrolls, watchdog stalls, or ambiguous missing pagination produce `INCOMPLETE` with `SAFETY_STOP`, never false exhaustion. A challenge or auth failure checkpoints and isolates only its platform; other platforms continue.

Platform readiness is durable and independent from account evidence:
`verified`, `sign_in_required`, `challenged_cooldown`, `user_action_required`,
`retryable`, `unknown`, and `resumed` are shown in the dashboard, while auth evidence can
remain `unknown` until it is conclusive. For all three Big-3 platforms, a
requested search surface with scoped cards or a verified empty state can prove
readiness even when a landing page has no account-navigation marker. Only an
affirmative login/authwall surface becomes `sign_in_required`; a CAPTCHA or
other challenge becomes `challenged_cooldown` without declaring the account
signed out. The recovery flow is ordinary Chrome only: clear a challenge or
sign in manually, then use Resume checkpoint so the platform is rechecked while
other platform work remains checkpointed.
The dashboard binds only to `127.0.0.1`. For remote operator access, establish
an ordinary user-controlled SSH local forward, for example
`ssh -N -L 8765:127.0.0.1:8765 mac-host`, then browse to
`http://127.0.0.1:8765/` on the remote device. This forwards the dashboard
only; Chrome, visual challenges, sign-in, and the human clearance step remain
on the Mac session and are never bypassed.

REC-102 is intentionally superseded for normal Big-3 crawling by the
one-window/one-search-tab/pane-first design. Historical compatibility remains
for user-invoked re-enrichment and is covered separately.
