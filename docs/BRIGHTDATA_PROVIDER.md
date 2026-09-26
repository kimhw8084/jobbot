# Bright Data Jobs Scraper adapter

Managed/API acquisition through acquisition-v2's shared provider-neutral
boundary is the selected Big-3 production direction. The Bright Data Jobs
Scraper adapter is integrated behind that boundary, but remains unqualified
and is not production-approved until bounded LinkedIn, Indeed, and Glassdoor
qualification passes independent review. CHG-275 remains open pending local
account configuration and an explicit maximum-record or dollar cap. No
successful live operation, production readiness, ROI, or exhaustive live
coverage is established here. RUN_NOW has not switched and remains on the
retained legacy Chrome/MV3 compatibility route, frozen for compatibility only.
Public ATS/employer retrieval remains a separate verification and enrichment
path.

Before bounded qualification, verify the locally configured account's access,
limits, pricing, and each selected scraper's exact input and output schema.
No account-specific dataset ID, schema, coverage, or pricing is assumed here.
Public product and pricing pages are not account evidence or an API contract;
verify current account-specific terms during qualification. See the
[Jobs Scraper product page](https://brightdata.com/products/web-scraper/jobs-scraper),
[async trigger contract](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests),
[progress contract](https://docs.brightdata.com/api-reference/scrapers/management-apis/monitor-progress),
and [snapshot parts contract](https://docs.brightdata.com/api-reference/scrapers/management-apis/get-snapshot-delivery-parts). Qualification requires an explicit maximum-record or dollar cap and a disposable database; it does not use the production SQLite database.

## Runtime configuration

If qualification is authorized, the token is read only from the environment
variable `BRIGHTDATA_API_TOKEN`.
Store it in the operator's secret manager or shell environment. Do not add it
to TOML, JSON fixtures, logs, exports, task metadata, or source control.

For each platform included in qualification, provide a JSON runtime
configuration environment variable only after verifying that account's exact
schema:

- `JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG`
- `JOBBOT_BRIGHTDATA_INDEED_CONFIG`
- `JOBBOT_BRIGHTDATA_GLASSDOOR_CONFIG`

If configured, each value must contain the account's exact `dataset_id`, a
required keyword input field, optional supported filters, and exact row output
fields. The following is a placeholder shape only; use actual account/schema
values only after verifying them during qualification. No dataset ID or
per-account field name is assumed by JobBot:

```json
{
  "dataset_id": "<account-specific-dataset-id>",
  "input_schema": {
    "keyword": "<exact-keyword-field>",
    "location": {"field": "<exact-location-field>", "source": "home_state"},
    "remote": {"field": "<exact-remote-field>", "value": "<schema-approved-remote-value>"},
    "window_days": {"field": "<exact-freshness-field>", "type": "integer"}
  },
  "output_schema": {
    "source_job_id": "<exact-job-id-output-field>",
    "source_url": "<exact-job-url-output-field>",
    "title": "<exact-title-output-field>",
    "company": "<exact-company-output-field>",
    "location": "<exact-location-output-field>",
    "posted_text": "<exact-posting-text-output-field>",
    "description": "<exact-description-output-field>",
    "salary": "<exact-salary-output-field>",
    "application_url": "<exact-application-url-output-field>"
  }
}
```

In a verified account configuration, `location.source` may be `home_state` or
`home_metro` and reads the configured candidate facts (currently Texas / Austin
metro). A verified freshness field can use
`{"field":"...","type":"integer"}` for a day count, or a configured
`template` containing `{days}` for that scraper's exact representation. Remote
input is sent only when the frozen task says remote is required and the
qualified schema supports it. Omit a filter when that scraper schema does not
support it; the request never becomes location, remote, salary, employment,
posting, or application evidence.

For a verified account configuration, `output_schema` maps JobBot's allowed
observed-field names to that scraper's exact output keys. At least
`source_job_id` or `source_url` must be mapped. If the qualified result is
wrapped in an account-specific object instead of a root array, set
`result_rows_key` to its exact row-array property. Provider verification claims
are ignored and cannot be mapped into trusted evidence.

The offline preflight reports only presence/validity flags. It does not contact
Bright Data, cannot validate whether a non-empty API token is active, and does
not qualify the adapter:

```bash
python -m jobbot acquire --provider brightdata-jobs --preflight --platform linkedin
```

The live transport requires explicit opt-in and a caller-supplied maximum
record-validation budget. This illustrative command uses a disposable
database; `100` is an example only, not a selected cap or authorization. A
budget stop leaves unfinished tasks incomplete and does not establish
exhaustion:

```bash
JOBBOT_DATABASE_PATH=/tmp/jobbot-brightdata-qualification.sqlite3 \
python -m jobbot acquire --provider brightdata-jobs --live-transport \
  --max-records 100 --mode fast --platform linkedin
```

This documentation reconciliation makes no live call and establishes no live
qualification result. Adapter tests inject a mock transport and make zero
network requests.

## Lifecycle and evidence

The adapter targets the documented dataset flow: `POST /datasets/v3/trigger`,
bounded `GET /datasets/v3/progress/{snapshot_id}` polling, a snapshot-part
count, and retrieval of every reported part. Qualification must verify that
the configured account and schema support the expected live behavior. A
snapshot ID alone is not completion. The task completion gate requires
terminal `ready`, a valid part count, all reported parts retrieved and parsed,
no reported row errors or continuation markers, and sanitized completion
evidence containing platform, CHG-170 task key, snapshot ID, terminal state,
part information, and request fingerprint.
Transient timeouts, 429/5xx, missing results, malformed data, unsupported
cursors, uncertain truncation, and exact-budget caps stay retryable/incomplete.
Retries and pending polls have provider-local bounds.

The `max_records` budget is caller supplied and the adapter maps it to Bright
Data's documented `limit_multiple_results`; confirm that the configured
account accepts the expected limit behavior during qualification. It is not a
hard-coded provider or marketing-page cap. If a response reaches the remaining
budget, the task is incomplete because collection may have been truncated.
Provider-reported cost or credits are persisted only if the API returns them.

When records are returned, normalized fields are observations only: source
ID/URL, title, company, location, posting date/text, employment type,
description/summary, salary, and present application/employer/ATS URLs. Missing
fields remain unknown. Source surface remains `linkedin`, `indeed`, or
`glassdoor`; acquisition provider is `brightdata-jobs`. The exact sanitized
trigger/result request shape and task provenance are designed to be persisted
with provider metadata when the qualified adapter runs. Authentication headers
and token values are never persisted.

Provider diagnostics are available in the dashboard Search quality section,
`provider_diagnostics.json`, and `live_discoveries.csv`. They add provider
counts, delivered records, failures, budget stops, and returned cost/credit
observations. They do not change CHG-114 learned-order dimensions or execution
ranks.
