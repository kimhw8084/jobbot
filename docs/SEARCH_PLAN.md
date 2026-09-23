# Search plan

Generate a deterministic plan without starting a browser run:

```bash
python -m jobbot search-plan --mode fast
python -m jobbot search-plan --mode deep
python -m jobbot search-plan --mode staged
```

The outputs are `out/search_plan.json`, `out/search_plan.csv`, and `out/search_plan.html`. Each task exposes the strategy-profile id/version, query family, query kind and pass, platform, exact query, career-lane mapping, priority and initial order, remote condition, age window, resume route, execution rank, and URL. Live search counts are produced from `config/live_search.toml`; career allocation percentages describe long-term career architecture only and never determine query budgets.

## Platform passes

- **LinkedIn:** paired intent/natural-language and deterministic title-family queries for every enabled family.
- **Indeed:** compact exact-phrase and bounded Boolean family queries; no single giant Boolean expression.
- **Glassdoor:** several narrow title/family searches per family.

Fast mode uses the initial-order prefix in the active profile as a calibration priority. Staged mode partitions recent work into the initial prefix and the remaining families, then adds the full deep backfill. Deep and staged plans guarantee at least one deep recall pass for every enabled credible family on all three platforms. The fast prefix cannot change the deep recall universe. Every query has no result cap. Task identity remains `(platform, exact query, age window, remote constraint)`; duplicate tasks are removed without making phase or family part of the identity.

The active family set covers provider lifecycle/credentialing/provider data; nonclinical healthcare quality/data/documentation/compliance; healthcare project/implementation/program support; higher-ed academic back-office; bounded transferable records/data/process operations; back-office patient access/eligibility/enrollment; and bilingual AI/content quality. Search tasks carry strategy-profile/query-family provenance into `browser_search_tasks`, durable search sightings, and source occurrences. Cards are persisted before qualification; active family membership also puts its cards into the existing detail/qualification queue. CHG-114 remains the owner of yield/cost telemetry, adaptive ordering, duplicate clustering, and field-provenance UX.

At runtime, CHG-114 may reorder tasks only within the same phase and platform, after exact-query evidence-ready history passes configurable sample and confidence guardrails. The compiled plan itself stays deterministic and complete; a new run freezes its effective rank when queued. See [Search quality](SEARCH_QUALITY.md) for the API views, defaults, and proof boundaries.

## Qualification and ranking

Retrieval does not use preference-only signals as exclusion gates. Higher-ed/institutional settings and task/deadline-oriented back-office duties can rank higher; continuous phone/chat/live-intake demand, admissions sales, and customer-success patterns can rank lower. A preference mismatch does not remove a discovery.

The existing qualification path fails closed for actionable recommendations. Stored evidence must support home-only remote work compatible with Texas (including Texas when a posting restricts eligible states), full-time permanent employee status, verified employer-provided base pay of at least $24/hour or $49,920/year, zero mandatory travel/office attendance/onsite training/field work/required in-person events, a current/open posting, and mandatory credential/experience/skill fit supported by candidate evidence. Unknown facts remain review items. Title-only ambiguous terms do not establish final domain or qualification; substantive responsibility evidence is required. Handled application statuses set the effective recommendation to `ALREADY_HANDLED`, while historical discoveries and source sightings remain in the ledger.

The initial Sept. 22, 2026 priorities calibrate the first live results. They are not permanent career truth, fixed live-query weights, or quality evidence. Evaluate future changes using CHG-114's per-query/platform yield and cost evidence, not raw result totals alone. Runtime validation of live Big-3 browsing is separately human-gated and is not part of this build.
