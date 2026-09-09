# Search plan

Generate the exact pre-crawl plan with:

```bash
python -m jobbot search-plan --mode fast --open
python -m jobbot search-plan --mode deep --open
```

Outputs are `out/search_plan.json`, `out/search_plan.csv`, and `out/search_plan.html`. Each row shows phase, lane, configured allocation, profile, platform, canonical role, exact query text, search band, cadence, Remote condition, age window, priority, enabled state, truthful resume route, URL, and `UNLIMITED` production result count.

Task identity is `(platform, exact query, age window, remote constraint)`. Identical tasks are deduplicated; the same query at a different age window remains distinct. Current configuration compiles 139 unique core title queries per primary platform in deep mode (417 total) and 105 P0/P1 queries per platform in fast mode (315 total). Counts are generated, never hardcoded by the crawler.

Search bands are independent of career lane: GOLD (6h), SILVER (12h), GROWTH (24h), HEDGE (48h), and DEEP_TAIL (168h). Cadence state is durable per platform/definition; a lower-frequency definition remains enabled and is not a result cap. Query-yield learning is descriptive until at least 30 completed descriptions across two run dates, then uses a confidence bound and actionable jobs per browser minute for within-band ordering.
