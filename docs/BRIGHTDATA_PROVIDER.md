# Bright Data Jobs Scraper candidate

Bright Data Jobs Scraper API is JobBot's first production-candidate managed
acquisition adapter. It is a build/test integration only in CHG-200-r2 and is
not approved or selected for production. RUN NOW and the browser crawler remain
unchanged; browser acquisition is frozen legacy work under CHG-39-r16.

Before any later live qualification, verify current Bright Data product
coverage, account limits, pricing, and the selected scraper's exact input and
output schema. Product coverage and pricing can change. The current product
pages advertise LinkedIn Jobs, Indeed Jobs, Glassdoor Jobs, keyword discovery,
and a 5,000-record/month free tier; that marketing information is not treated
as an API contract or embedded as a budget assumption in JobBot. See the
[Jobs Scraper product page](https://brightdata.com/products/web-scraper/jobs-scraper),
[async trigger contract](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests),
[progress contract](https://docs.brightdata.com/api-reference/scrapers/management-apis/monitor-progress),
and [snapshot parts contract](https://docs.brightdata.com/api-reference/scrapers/management-apis/get-snapshot-delivery-parts).

## Runtime configuration

The token is read only from the environment variable `BRIGHTDATA_API_TOKEN`.
Store it in the operator's secret manager or shell environment. Do not add it
to TOML, JSON fixtures, logs, exports, task metadata, or source control.

Provide one JSON runtime configuration environment variable for each selected
platform:

- `JOBBOT_BRIGHTDATA_LINKEDIN_CONFIG`
- `JOBBOT_BRIGHTDATA_INDEED_CONFIG`
- `JOBBOT_BRIGHTDATA_GLASSDOOR_CONFIG`

Each value must contain the account's exact `dataset_id`, a required keyword
input field, optional supported filters, and exact row output fields. Example
shape only; replace each angle-bracket value with the exact field/schema value
shown for the selected account scraper. No dataset ID or per-account field name
is assumed by JobBot:

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

`location.source` may be `home_state` or `home_metro` and reads the configured
candidate facts (currently Texas / Austin metro). A freshness field can use
`{"field":"...","type":"integer"}` for a day count, or a configured
`template` containing `{days}` for the scraper's exact representation. Remote
input is sent only when the frozen task says remote is required. Omit a filter
when that scraper schema does not support it; the request never becomes
location, remote, salary, employment, posting, or application evidence.

`output_schema` is a mapping from JobBot's allowed observed-field names to the
exact output keys for that scraper. At least `source_job_id` or `source_url`
must be mapped. If the result is wrapped in an account-specific object instead
of a root array, set `result_rows_key` to its exact row-array property. Provider
verification claims are ignored and cannot be mapped into trusted evidence.

The offline preflight reports only presence/validity flags. It does not contact
Bright Data and cannot validate whether a non-empty API token is active:

```bash
python -m jobbot acquire --provider brightdata-jobs --preflight --platform linkedin
```

The live transport requires explicit opt-in and a caller-supplied maximum
record-validation budget. Use a disposable database during any separately
approved qualification; a budget stop leaves unfinished tasks incomplete and
does not establish exhaustion:

```bash
JOBBOT_DATABASE_PATH=/tmp/jobbot-brightdata-qualification.sqlite3 \
python -m jobbot acquire --provider brightdata-jobs --live-transport \
  --max-records 100 --mode fast --platform linkedin
```

CHG-200-r2 does not make that live call. Tests inject a mock transport and make
zero network requests.

## Lifecycle and evidence

The adapter uses the documented dataset flow: `POST /datasets/v3/trigger`,
bounded `GET /datasets/v3/progress/{snapshot_id}` polling, a snapshot-part
count, and retrieval of every reported part. A snapshot ID alone is not
completion. A task becomes complete only after terminal `ready`, a valid part
count, all parts retrieved and parsed, no reported row errors or continuation
markers, and sanitized completion evidence containing platform, CHG-170 task
key, snapshot ID, terminal state, part information, and request fingerprint.
Transient timeouts, 429/5xx, missing results, malformed data, unsupported
cursors, uncertain truncation, and exact-budget caps stay retryable/incomplete.
Retries and pending polls have provider-local bounds.

The `max_records` budget is caller supplied and is sent as Bright Data's
documented `limit_multiple_results`; it is not a hard-coded provider or
marketing-page cap. If the response reaches the remaining budget, the task is
incomplete because collection may have been truncated. Provider-reported cost
or credits are persisted only when the API actually returns them.

Normalized fields are observations only: source ID/URL, title, company,
location, posting date/text, employment type, description/summary, salary, and
present application/employer/ATS URLs. Missing fields remain unknown. Source
surface remains `linkedin`, `indeed`, or `glassdoor`; acquisition provider is
`brightdata-jobs`. The exact sanitized trigger/result request shape and task
provenance are persisted with provider metadata. Authentication headers and
token values are never persisted.

Provider diagnostics are available in the dashboard Search quality section,
`provider_diagnostics.json`, and `live_discoveries.csv`. They add provider
counts, delivered records, failures, budget stops, and returned cost/credit
observations. They do not change CHG-114 learned-order dimensions or execution
ranks.
