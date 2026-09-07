# Search plan

Generate the exact pre-crawl plan with:

```bash
python -m jobbot search-plan --mode fast --open
python -m jobbot search-plan --mode deep --open
```

Outputs are `out/search_plan.json`, `out/search_plan.csv`, and `out/search_plan.html`. Each row shows lane, configured allocation, profile, platform, exact query, Remote condition, age window, priority, enabled state, resume route, URL, and `UNLIMITED` production result count.

Task identity is `(platform, exact query, age window, remote constraint)`. Identical tasks are deduplicated; the same query at a different age window remains distinct. Current configuration compiles 139 unique core title queries per primary platform in deep mode (417 total) and 105 P0/P1 queries per platform in fast mode (315 total). Counts are generated, never hardcoded by the crawler.
