# CHG-114 search quality

## Provider diagnostics

Acquisition-v2 adds provider/mode/platform diagnostics alongside the existing
search-quality dimensions. The provider metrics report task completion and
failure counts, persisted cards, provider record IDs, canonical jobs, and
trusted qualified yield. Provider partitioning does not change task/family
metric identity, frozen ordering, or its minimum-sample and confidence
guardrails. Runs, tasks, discovery receipts, and source occurrences expose
durable provider provenance. Historical rows are labeled legacy-browser with
unknown acquisition mode.

R1 establishes the adapter boundary only; it does not authorize or select a
managed provider for production. The custom normal-Chrome Big-3 runner is
frozen legacy/deprecated pending migration. Public ATS/employer retrieval
continues as independent verification/enrichment. Future browser-agent
services are fallback providers only and cannot independently establish
Actionable evidence.

CHG-114 measures search yield from the durable CHG-170 task/query ledger and the CHG-113 evidence and qualification states. It does not add a strategy profile, modify query families, or replace qualification or application-destination provenance. Metrics are derived from SQLite source rows on request; there is no materialized metric cache.

## Metric definitions

The definitions are versioned as `chg114-search-quality-v2` and returned by `/api/search-quality`. Version 2 separates the current actionable reservoir from trusted historical qualified yield; version 1 treated an actionable recommendation as the yield positive and could lose credit when application handling replaced that recommendation.

| Metric | Definition |
| --- | --- |
| `cards_persisted` | Number of `search_task_results` rows in the grouping. `cards_discoveries_persisted` is the equivalent explicit name. |
| `unique_source_ids` | Distinct `(source_site, source_job_id)`, using `source_url` when the source ID is blank. |
| `source_occurrence_metrics` | Cross-check of canonical source-occurrence identities by their durable profile/version/platform/family/kind/pass provenance. `source_occurrences` has no query age-window or phase field, so it supplements rather than replaces exact task metrics. |
| `unique_canonical_jobs` | Distinct non-null canonical `job_id` values linked to those results. |
| `sightings`, `duplicate_sighting_ratio` | `sightings` sums `sighting_count`. Duplicate ratio is `sum(max(sighting_count - 1, 0)) / sightings`; task-reported duplicate-card counts are exposed separately. |
| `recall_selected`, `recall_qa_samples` | Counts of result rows selected for detail enrichment and QA sampling. These do not change CHG-170 recall selection. |
| Detail counts and rate | Attempts sum `detail_attempts`; completion requires `detail_status=COMPLETE` and `content_state=COMPLETE`; partial and failure counts use durable result states. Completion rate is complete results divided by complete, partial, failed, retryable, and externally blocked result outcomes. |
| Verification conversions | Distinct canonical jobs in a CHG-170 verified source state or CHG-113 verified application-destination state, divided by linked canonical jobs. |
| Current actionable reservoir | `current_actionable_count` (also retained as `actionable_count`) counts distinct jobs with `evidence_readiness_state=READY`, `qualification_readiness_state=READY`, and a current actionable recommendation (`APPLY_NOW`, `APPLY_VOLUME`, or `HIGH_VALUE_STRETCH`). `ALREADY_HANDLED` and failed no-repeat status continue to prevent resurfacing and do not count as current actionable inventory. |
| Trusted qualified yield | `trusted_qualified_yield_count` is distinct linked canonical jobs that pass the current intrinsic qualification predicate described below. It can include a handled job when current durable evidence still proves it qualified. It is recalculated from current job state, not frozen at first qualification. |
| Hard reject, review, leakage | Distinct linked jobs with durable hard-reject reasons or `SKIP_HARD_GATE`, review recommendation/readiness, actionable/no-repeat conflict, and actionable/hard-reject conflict. These are indicators derived from current durable states. |
| Search/detail cost | Task pages visited plus detail reads. Cost per evidence-ready or actionable opportunity is null when its denominator is zero. Detail reads, pages, scroll generations, elapsed task seconds, and mean elapsed seconds are also reported. |
| Challenge and failure counts | Challenge, auth, external-block, interruption, and task-failure counts are distinct affected task instances within a metric row. Detail external blocks are also counted at result level. |
| Downstream stages | Distinct canonical jobs with application, screen, interview/final, or offer events in `application_events`. A job-stage is credited once to the earliest discovery in its logical query family. |

`task_metrics` includes profile/version, platform, family, kind/pass, exact query/task key, task age window (`window_days`), and phase. `family_metrics` combines exact queries within profile/version, platform, family, age window, and phase. Exact-task qualified counts can overlap between query rows; use family rows for within-family deduplication and do not sum across phase/window rows to produce a global unique-job total.

### Attribution and double counting

Occurrence metrics retain every persisted task result, including duplicate cards and multiple queries surfacing the same job. Canonical counts use distinct `job_id` within the displayed grouping. `multi_query_canonical_jobs` shows canonical jobs visible through more than one exact task in a family grouping.

For downstream funnel stages, the logical query dimension is strategy profile + profile version + platform + query family. Each canonical job is attributed once in that dimension, to the result with the earliest `first_seen_at`, then lowest `task_id`, then lowest `result_id`. Query kind, pass, task age window, and phase do not create extra copies of an application, screen, interview, or offer. The family/phase/window row containing that first discovery receives the event.

## Frozen evidence-aware ordering

At enqueue time, each task stores its CHG-170 `execution_rank` as `baseline_execution_rank` and stores `effective_execution_rank`, an ordering reason, sample size, and algorithm version. `browser_runs.ordering_config_json` records the parameters and peer-group decisions. Workers lease by effective rank only inside existing phase and selected-platform constraints. Phase progression, platform waves, worker leases, checkpoints, human waits, and every compiled task remain intact.

Ordering compares tasks only within the same phase and platform. Version `chg114-yield-v2` uses the same trusted qualified-yield predicate as search-quality telemetry. A positive requires current `evidence_readiness_state=READY`; complete identity; verified source and application destination; an active posting; a stable full-time employment class; no hard-reject reasons; the actionable intrinsic scorer recommendation; and every substantive CHG-170/CHG-113 qualification gate to pass. The required gates include evidence readiness, remote/location, work authorization, Texas eligibility, full-time and permanent employment, base pay, mandatory presence, currentness, supported requirements, and responsibility/domain evidence. Additional gates must also pass. The sole excluded gate is `no_repeat`.

No-repeat controls whether a job can be surfaced again. Application marking may set `ALREADY_HANDLED` and fail no-repeat, and a later rescore may set aggregate qualification readiness to `BLOCKED` only because no-repeat failed. Those states do not erase historical query quality when the durable scorer snapshot still records an actionable intrinsic recommendation and every current substantive gate passes. During rescoring, the scorer calculates and stores `intrinsic_recommendation` in `evidence_readiness_json` while keeping the current recommendation `ALREADY_HANDLED`; existing records can use their pre-handling recommendation snapshot. An application event by itself is never proof of qualification. A handled REVIEW, hard-fail, incomplete-evidence, identity-mismatch, or substantive-gate failure remains zero trusted qualified yield. A later trusted source update that closes the posting, fails a gate, removes verification, or makes evidence non-READY removes its yield credit at the next calculation.

The current actionable reservoir remains separate from historical yield. `actionable_count` and its explicit alias `current_actionable_count` retain the current recommendation/readiness rule and continue to exclude handled jobs. `qualified_yield` and `trusted_qualified_yield_count` use the intrinsic predicate above. Dashboard columns display both counts so acting on a good job can reduce current inventory without making its search query look worse.

Historical observations enter the ordering sample only after a completed or partial detail result links to a durable `READY`, `REVIEW`, or `BLOCKED` job state. Raw cards, card volume, title-only matches, and query counts do not contribute positive outcomes.

The score is a pooled-prior shrinkage estimate of trusted qualified outcomes per search/detail cost. The pooled trusted-qualified rate is the prior mean, with a configurable prior strength. A peer group is reordered only when every task meets the minimum sample and adjacent 95% Wilson intervals of qualified yield per cost are separated by the configured minimum effect. Otherwise the whole peer group keeps its CHG-170 baseline order. Learned displacement is capped. Stable ties use baseline execution rank, priority, then task key.

Defaults are `min_evaluated_jobs=8`, `prior_strength=8`, `confidence_z=1.96`, `minimum_effect_per_cost=0.02`, and `max_rank_displacement=20`. To override them, add a `[search_quality_ordering]` table to `config/runtime.toml` (or its loaded runtime mapping):

```toml
[search_quality_ordering]
min_evaluated_jobs = 8
prior_strength = 8.0
confidence_z = 1.96
minimum_effect_per_cost = 0.02
max_rank_displacement = 20
```

Insufficient samples or overlapping/contradictory intervals produce the exact baseline execution order. Ordering changes scheduling only: it never disables, removes, edits, or permanently demotes a query. Each new run freezes its decision before workers start.

## Probable siblings

`/api/sibling-clusters` and the dashboard show advisory groups from normalized employer/title, compatible location or remote bucket, posting-date proximity and description similarity. Identity-mismatch records are excluded. Both verified ATS requisition identities may be retained in a group as distinct related siblings. Each cluster includes algorithm version, confidence, basis, and canonical member job IDs. Recalculation is deterministic and read-only; canonical jobs and source occurrences are never merged, reassigned, or deleted.

## Field provenance

Job list/detail APIs provide a concise `field_provenance` summary for remote, location, salary, employment type, posted/current status, requirements, source verification, and application destination. Each field includes its value, evidence/source type, state, and readiness or verification state. Direct observations require a durable CHG-113 evidence marker or readiness state. A non-empty value without such support is labeled `INFERRED_OR_DERIVED`; absent values are `UNKNOWN`. Observed but unverified destinations remain labeled as such. The full `evidence_readiness_json`, `evidence_provenance_json`, discovery `card_json`, and occurrence `raw_json` remain available. CSV/JSON exports add `field_provenance_json` and retain the underlying evidence JSON.

## Views and validation boundary

- `/api/search-quality` returns metric definitions, query/family aggregates, and the latest run's baseline/effective order, reason, and sample size.
- `/api/sibling-clusters` returns deterministic advisory groups without mutation controls.
- Dashboard job details show field provenance; selected/all job exports include its structured summary and raw evidence.

The instrumentation is validated with isolated seeded SQLite fixtures. It does not establish that learned ordering improves live production yield. Production crawl data is not needed or read for the tests; live Big-3/runtime acceptance remains separately human-gated and `NOT_RUN` for this build.
