# AeroSense AI 2.0 — Phase 1.5 Architecture Audit

**Audit date:** 2026-11-22  
**Scope:** Existing project files, SQLite implementation, external API code paths,
and the requested future architecture. This audit makes no application or
dependency changes.

## Executive summary

The project is a small Streamlit prototype, not yet a multi-source ingestion
platform. It has reusable pieces worth preserving—API fetch helpers, pure AQI
and anomaly calculations, a bounded SQLite store, and a passing unit-test
baseline—but the current data model records a city-level snapshot, not
station-level observations or raw source data.

Verification is limited and is reported explicitly:

- **Open-Meteo:** `fetch_open_meteo("Chennai")` made a live request and returned
  current weather fields during this audit. This verifies that endpoint and
  parser for that one request, not the other cities, sustained availability, or
  full Streamlit UI behavior. Open-Meteo describes current weather as model
  based, not as a station observation ([API documentation](https://open-meteo.com/en/docs)).
- **OpenAQ:** not live-verified. No `OPENAQ_API_KEY` was configured, and the
  official quick start requires an API key
  ([OpenAQ quick start](https://docs.openaq.org/using-the-api/quick-start)).
  Code inspection is not evidence that authentication, response parsing, or
  Chennai station coverage works against the live API.
- **Tests:** all five existing unit tests passed (details below).
- **UI startup:** not verified. The available Python environment has a
  Streamlit package but is missing its `blinker` dependency, so importing
  Streamlit fails in that environment. This is an environment limitation, not
  evidence that a clean installation from `requirements.txt` fails.

## Repository inventory

The workspace contains exactly these six project files (24,224 bytes in total
at audit time), and no SQLite database file or other repository files:

| File | Existing responsibility |
|---|---|
| [`app.py`](./app.py) | Streamlit UI, API-key input, five-minute refresh, dashboard, charts, AQI alerts, and anomaly display. |
| [`air_quality.py`](./air_quality.py) | City configuration, OpenAQ/Open-Meteo HTTP calls, parsing, SQLite schema and persistence, rolling mean, India AQI estimate, and anomaly rule. |
| [`test_air_quality.py`](./test_air_quality.py) | Five unit tests for AQI, AQI categories, anomaly detection, database deduplication, and row caps. |
| [`requirements.txt`](./requirements.txt) | At the time of this original audit, this was the legacy Streamlit dependency declaration. It now delegates to the development lock; see the current profile locks and regeneration instructions in [`README.md`](./README.md). |
| [`README.md`](./README.md) | Windows setup, API-key instructions, storage limits, AQI caveat, and test command. |
| [`.gitignore`](./.gitignore) | Excludes Python caches, local environments, the `data/` directory, and Streamlit secrets. |

This directory is **not a Git repository**, so a diff or commit history is
unavailable. The six files above are all files present and inspected; it is not
possible to independently establish which were changed in a prior revision.
Tests create their database in a temporary directory, so no database artifact
was present during the inventory.

## Existing architecture and behavior

1. `app.py` owns the entire UI and invokes a Streamlit fragment every 300
   seconds. `load_snapshot` calls each provider serially, caches results for
   the same interval, and returns both data and per-city `DataSourceError`
   messages.
2. `air_quality.py` owns configuration, transport, provider-specific parsing,
   calculations, and persistence. It uses Python's standard-library
   `urllib`, `json`, and `sqlite3`; no API client or ORM is used.
3. OpenAQ lookup starts with nearby locations (up to 100 returned), selects at
   most five candidate stations, then requests each station's latest readings.
   For each pollutant, the newest timestamp among the selected stations is kept
   as one city reading.
4. Open-Meteo requests current temperature, humidity, wind speed, and weather
   code for the configured city coordinates.
5. SQLite stores only normalized scalar readings. A unique key suppresses
   duplicate provider timestamps. On each store operation, data older than
   seven days is deleted and each city/source/metric series is capped at 1,500
   rows.
6. The UI calculates the mean of stored PM values in the last 24 hours, maps
   PM2.5/PM10 to an estimated Indian AQI, and applies a median/MAD-style spike
   rule to the latest history. Neither calculation uses a separately persisted
   derived result.

### Hardcoded configuration and duplicated presentation values

- Four city names and coordinates (`CITIES`); a 25 km radius, 100-location
  response limit, and five selected locations in OpenAQ lookup.
- Poll interval of 300 seconds, HTTP timeout of 15 seconds, four Open-Meteo
  current variables, two OpenAQ pollutant names, and display units/labels.
- AQI threshold 100, India AQI concentration breakpoints and categories,
  anomaly minimum sample count and MAD multiplier/floor.
- Retention of seven days and a 1,500-row per-series cap.
- Several settings are restated in user-facing captions or documentation
  (notably five minutes, AQI 100, and seven days). These can drift from their
  code constants. There is no separate configuration layer.

There is little duplicated algorithm code: HTTP JSON transport is shared,
while provider-specific URL construction and parsing are separate. The main
coupling is architectural rather than textual: provider integration,
normalization, persistence, calculations, and UI-facing behavior all live in
one utility module.

## Data architecture assessment

| Capability | Current support | Assessment |
|---|---|---|
| Multiple external providers | `source` is a text column; provider calls are separate functions. | A useful prototype seam, but no adapter contract, provider registry, or independent ingestion process. |
| Raw API payload retention | None. Only selected scalar values and labels are stored. | Cannot reproduce parsing decisions, audit upstream changes, or reprocess old source data. |
| Normalized observations | One row shape: city, source, metric, value, unit, observed time, location name. | Insufficient for sensor identity, provider IDs, quality flags, or consistent canonical metric/unit semantics. |
| Multiple cities | City string and hardcoded coordinate map. | Works for the four configured entries; no city table or extensible location management. |
| Multiple stations | One selected reading per pollutant per city is persisted. Location is only a display name. | Station and sensor IDs are discarded; readings from different stations can collide or be conflated. |
| Historical readings | SQLite timestamped rows, idempotent inserts, time pruning and a per-series row cap. | Good bounded local prototype storage; not a durable or complete observation history. The row cap can make actual retention shorter than seven days at higher sampling rates. |
| Provenance and timestamps | Source label, location name, and provider observation timestamp. | Missing station/sensor IDs, source license/provider details, ingestion timestamp, request/payload reference, and freshness/quality status. |
| Future PostgreSQL migration | Basic SQL and SQLite schema are localized in one module. | Data concepts can be carried forward, but the schema is not directly portable: SQLite PRAGMAs, `INSERT OR IGNORE`, `?` placeholders, and SQLite-specific retention query need replacement. There is no migration/versioning system. |
| Storage bounds | Seven-day deletion plus 1,500 rows per city/source/metric; database directory is Git-ignored. | Appropriate for the small local demo, not a global byte/row bound. Adding arbitrary stations, metrics, or providers increases the number of capped series. |

The implementation uses a city/source/metric/observed-time unique key. Without a
station or sensor identifier, two distinct stations reporting the same metric
at the same instant are treated as the same observation. This is a material
data-loss risk if station-level readings are later added.

## API integration audit

### Open-Meteo

- Uses the documented forecast endpoint and `current` parameters for four
  configured coordinates. The Chennai call succeeded during this audit.
- The response parser checks that `current` is an object with a time and
  includes numeric requested values. It does not comprehensively validate
  response shape, finite/range-valid values, or time parsing.
- Units are taken from `current_units`; weather code is stored as a numeric
  observation. The source is labeled `Open-Meteo`, but UI records do not show
  provider attribution or distinguish modeled current conditions from
  instrument readings.
- Current conditions are model-based and their update interval is not the same
  as the dashboard's five-minute polling interval. Repeated polls may return
  the same timestamp; the uniqueness key prevents duplicates. A five-minute
  poll therefore does not imply five-minute-new weather data.
- No retries/backoff, rate-limit handling, or independent request metrics are
  implemented.

### OpenAQ

- Uses OpenAQ v3 locations and location-latest routes, with the documented
  `X-API-Key` header pattern. The OpenAQ latest resource itself provides the
  most recent value per sensor and does not guarantee complete or freshly
  ingested time-series coverage ([latest resource documentation](https://docs.openaq.org/resources/latest)).
- Live behavior is unverified in this environment because no key is
  configured. The location selection, response mapping, permissions, and
  available PM stations in the target cities therefore remain unconfirmed.
- It takes the first page of up to 100 locations, then at most five stations.
  It does not paginate or persist the selected stations' IDs. It chooses the
  newest PM2.5/PM10 reading across those stations rather than representing each
  station or explicitly aggregating them.
- Parameter names are normalized, but the code assigns `µg/m³` rather than
  carrying the sensor parameter's declared unit. Non-finite values and absent
  timestamps are rejected; timestamps are otherwise compared as strings, not
  parsed and normalized. A boolean value would pass Python's numeric type
  check.
- Location/sensor/provider/license metadata available in the API response is
  mostly discarded. The dashboard does not present station-level data
  provenance or attribution details. OpenAQ location responses expose
  provider, coordinates, sensors, and licenses
  ([locations resource documentation](https://docs.openaq.org/resources/locations)).
- A 401 and 429 receive specific error messages. Other HTTP errors are
  surfaced generically. There are no retries, exponential backoff, request
  concurrency controls, or explicit handling for all malformed-response and
  timeout shapes.

### Credentials, transport, and shared integration concerns

- OpenAQ key can come from `OPENAQ_API_KEY` or a password-type Streamlit input;
  it is sent in the request header and no project file stores it. The app does
  not use Streamlit secrets directly. Deployment still needs a server-side
  secret-management policy and must not expose the key in logs or client code.
- A 15-second `urllib` timeout is set, but there is no retry/backoff policy.
  Requests are sequential: a city can trigger one location lookup plus up to
  five latest calls, in addition to weather calls for all four cities. The
  worst-case polling duration can exceed the five-minute interval.
- `DataSourceError` is caught per city/provider in the snapshot loader, but
  unexpected response shapes or SQLite failures can escape and fail a
  fragment run. Validation is not centralized.
- `observed_at` means provider measurement/model time; there is no
  `ingested_at`/fetch time. The UI can show old observations as current because
  age thresholds are not enforced. The successful API-check time is not a
  per-source success timestamp.
- PM is assigned a presumed common unit rather than normalizing units. Do not
  combine readings across providers or stations until unit and averaging
  semantics are explicit.

## Technical gap analysis

### Data correctness — high priority

1. The dashboard labels the mean of whatever PM snapshots happen to be stored
   during the previous 24 hours as a 24-hour mean. This is not a guaranteed
   time-weighted or complete 24-hour concentration. The India AQI breakpoints
   are then applied to that incomplete estimate. Present this as an estimate,
   enforce freshness/completeness rules, and only publish an AQI when its
   required averaging window is valid.
2. PM snapshots can switch stations over time, but station identity is not
   stored. A displayed trend or average may mix stations without making that
   visible.
3. Provider units and pollutant metadata need validation and canonical
   conversion before combining observations.
4. Old provider timestamps are accepted without a stale-data status or
   dashboard warning.

### Reliability and scale — high priority before production ingestion

1. Retries, bounded backoff, rate limiting, per-provider health status, and
   structured logs are absent.
2. The Streamlit process is both dashboard and poller. Every active browser
   session can trigger storage writes; there is no durable scheduler,
   checkpointing, job status, or backpressure.
3. SQLite has no schema migrations, explicit journal/concurrency strategy, or
   operational backup/restore procedure. Its local retention policy is not a
   substitute for a production data-retention policy.
4. No global storage/byte limit exists if arbitrary sources, stations, or
   metrics are introduced.

### Testing and operations — high priority before Phase 2

- Existing tests cover pure calculations and two SQLite behaviors only. They
  do not test API request construction/parsing, units, station identity,
  stale timestamps, provider errors, retention expiry, or a full app startup.
- No lockfile or pinned reproducible environment is present. `streamlit` is
  the only declared dependency; current environment import failure is due to a
  missing transitive dependency (`blinker`), so UI smoke validation remains
  outstanding here.
- No configuration validation, health endpoint, metrics, deployment setup,
  migration tooling, or data-quality monitoring exists.

## Proposed AeroSense AI 2.0 architecture

Keep the current prototype intact while creating explicit boundaries in
successive phases:

```text
Next.js dashboard
       │ HTTPS / typed API
FastAPI service ─── auth, request validation, read/query endpoints
       │
Domain services ─── observations, AQI, alerts, forecast queries
       │
PostgreSQL + migrations
       ▲
Scheduled ingestion workers
       │
Provider adapters ─── OpenAQ | Open-Meteo | future verified sources
       │
Validated raw responses → normalized observations → quality/derived products

Forecasting pipeline ─── versioned features/models/predictions
Anomaly pipeline ─────── deterministic rules first, ML later
Gemini + LangGraph ───── bounded, read-oriented tools over approved data/API
```

### Recommended boundaries and records

- **Provider adapters:** one adapter per external service with explicit
  configuration, fetch, validation, normalization, attribution, and
  provider-specific error types. Adapters return typed canonical records;
  they do not write directly to dashboard state.
- **Ingestion:** scheduled worker(s), separate from API request handling and
  browser sessions. Track run ID, source, started/finished time, outcome,
  retry count, and records accepted/rejected. Make ingestion idempotent.
- **Raw payloads:** retain only what is needed for audit/reprocessing. Record
  source, endpoint/resource, fetched time, response status, content hash, and
  payload or controlled object-storage reference. Never store credentials or
  request headers containing secrets. Set explicit size and retention limits.
- **Normalized PostgreSQL model:** stable IDs and foreign keys for `cities`,
  `stations`, `data_sources`, and (if needed) `sensors`/`parameters`; an
  `observations` table with canonical metric, numeric value, canonical unit,
  `observed_at`, `ingested_at`, source/station/sensor IDs, quality flags, and
  raw-payload reference. Use a unique key based on the actual source record
  identity, not only city and metric. Add indexes for station/metric/time and
  city/time queries. Manage schema changes through versioned migrations.
- **Derived data:** keep AQI, alerts, and forecasts distinguishable from raw
  observations. Store calculation/model version, input window, generation
  time, and quality/completeness status where historical reproducibility
  matters.
- **FastAPI:** expose versioned read APIs and operational endpoints; keep
  ingestion/forecast jobs outside synchronous request handlers. Validate
  request/response models and report unavailable/stale data clearly.
- **ML forecasting and anomaly detection:** start with transparent, tested
  baselines and data-quality checks. Add models only after sufficient,
  quality-controlled history exists; version model, features, training data
  window, and forecast timestamps.
- **Gemini + LangGraph:** introduce only after stable APIs and authorization.
  Give agents narrow, read-only tools with validated inputs/outputs and
  bounded execution; keep deterministic data ingestion and AQI calculations
  outside the LLM decision path. Manage Gemini credentials server-side.
- **Next.js:** consume the typed FastAPI contract; it should not call providers
  directly or hold provider/Gemini secrets. Keep charts and user interaction
  separate from ingestion and calculation logic.

## Reusable existing components

- **AQI conversion and category helpers** in `air_quality.py`: preserve as
  pure domain functions after validating official averaging windows and
  breakpoint semantics; add boundary tests.
- **Anomaly rule** in `air_quality.py`: a small, explainable baseline to retain
  as one versioned rule, not a substitute for provider quality checks or a
  trained anomaly model.
- **HTTP JSON helper** in `air_quality.py`: useful prototype, but production
  adapters should replace or wrap it with shared timeout, retry, redaction,
  response-size, and observability policies.
- **OpenAQ sensor-name normalization and Open-Meteo field mapping:** reuse the
  intent; improve them to typed, unit-aware adapters with station/sensor IDs
  and validation.
- **SQLite retention/deduplication behavior:** useful local-dev reference and
  test fixture. Keep SQLite for local prototype runs if desired; port semantics
  through explicit PostgreSQL migrations rather than carrying SQLite SQL
  directly into production.
- **`DataSourceError` and UI error presentation:** retain the principle of
  surfacing provider failures, then replace with structured, provider-specific
  errors and backend health/status responses.
- **Five existing tests:** preserve and extend; no test has been removed.
- **Small dependency footprint and ignored local data:** continue to avoid
  committing databases, environments, credentials, generated datasets, or
  model artifacts.

## Recommended migration sequence

1. **Stabilize current behavior:** resolve the critical data semantics and
   provider-verification issues below; add parser, freshness, units, and
   attribution tests. Confirm a clean install and a Streamlit smoke run.
2. **Define contracts:** specify canonical observation and source metadata
   types, timestamps/units, freshness/quality rules, idempotency keys, and
   versioned AQI/alert outputs. Keep pure calculations and existing tests.
3. **Extract adapters and ingestion boundary:** move each integration behind
   an adapter interface; add bounded retries and observability; run ingestion
   independently of Streamlit. Keep UI behavior during the transition.
4. **Introduce PostgreSQL and migrations:** create normalized tables and
   indexes, migrate only retained data that meets quality criteria, and
   verify deduplication, retention, and query equivalence. There is no
   database file in the current workspace to migrate.
5. **Add FastAPI:** expose the stable read/query contract and health status;
   switch Streamlit to the API temporarily if needed to minimize concurrent
   UI/backend change. Validate before replacing the UI.
6. **Build the Next.js dashboard:** consume the same API contract; compare
   key AQI/trend/alert outputs with the existing UI.
7. **Add forecasting and advanced anomaly models:** only after sufficient
   trusted history and a reproducible feature pipeline.
8. **Add Gemini/LangGraph:** last, with scoped tools, server-side secrets,
   authorization, auditability, and deterministic fallbacks for non-LLM
   safety-critical values.

## Critical issues to fix before Phase 2

1. **OpenAQ is not live-verified.** Obtain/configure a key in a safe
   environment and verify authenticated location discovery, latest readings,
   target-city coverage, throttling, and error cases. Do not represent its
   integration as operational until this passes.
2. **Correct AQI and trend semantics.** Do not label sparse, possibly mixed
   station snapshots as a valid 24-hour mean. Define completeness, freshness,
   and station-selection/aggregation rules before presenting a numeric AQI.
3. **Preserve observation identity and provenance.** Capture provider
   station/location and sensor/parameter IDs, source metadata, `observed_at`,
   `ingested_at`, provider units, and source attribution. Update uniqueness so
   distinct stations cannot overwrite one another.
4. **Validate and normalize data.** Parse timestamps, reject booleans and
   non-finite/out-of-range values, validate response shapes, and convert only
   explicitly supported units. Surface stale/missing data distinctly.
5. **Harden request behavior.** Add bounded retries/backoff for transient
   failures, honor rate limits, avoid blocking polling beyond its schedule,
   and provide per-source success/failure status. Never retry authentication
   or validation failures blindly.
6. **Establish migration and storage policy.** Before multi-source ingestion,
   define schema versioning, PostgreSQL migration tests, backups, raw-payload
   retention/size limits, and a global storage policy. The existing per-series
   SQLite cap alone is not a production bound.
7. **Verify the application environment.** Install from the declared
   requirements in a clean external environment, run the Streamlit startup
   smoke test, and add API/parser tests that do not require live credentials.

## Verification record

Command run:

```text
python -B -m unittest -v test_air_quality
```

**Result:** 5 tests passed; 0 failed.

| Test | Result |
|---|---|
| AQI uses worst available PM sub-index | Passed |
| AQI category boundaries | Passed |
| Anomaly minimum history and spike behavior | Passed |
| SQLite duplicate suppression | Passed |
| SQLite per-series row cap | Passed |

Live provider checks during this audit:

| Provider | Result |
|---|---|
| Open-Meteo, Chennai | Request succeeded and returned current fields. No claim is made for all cities or sustained service. |
| OpenAQ | Not verified: no API key configured. |
| Streamlit application startup | Not verified: current interpreter cannot import Streamlit due to missing `blinker`. |

## References

- [OpenAQ quick start and API key](https://docs.openaq.org/using-the-api/quick-start)
- [OpenAQ locations resource](https://docs.openaq.org/resources/locations)
- [OpenAQ latest measurements](https://docs.openaq.org/resources/latest)
- [Open-Meteo Weather Forecast API](https://open-meteo.com/en/docs)
- [Streamlit fragments and periodic reruns](https://docs.streamlit.io/develop/concepts/architecture/fragments)
