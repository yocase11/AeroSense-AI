# AeroSense AI 2.0 architecture

## Runtime shape

```text
OpenAQ · Open-Meteo · NASA POWER/FIRMS · GDELT · Overpass
                       │
        bounded HTTP provider adapters
                       │
       validation + canonical unit conversion
                       │
       raw payloads + normalized PostgreSQL
        PostGIS coordinates · pgvector chunks
                       │
         FastAPI domain/query endpoints
              │                  │
          Redis SSE        LangGraph agents
              │          Gemini when configured
              └──── Next.js dashboard

Airflow DAGs (optional scheduled ingestion) · Kafka (optional event mirror)
```

The existing Streamlit/SQLite application remains in `app.py` and
`air_quality.py` as a local reference path. AeroSense 2.0 uses PostgreSQL as
its primary persistence layer. It does not silently fall back to SQLite when
PostgreSQL is unavailable.

## Code boundaries

- `backend/integrations.py`: independent HTTP/API parsers and provider
  configuration status.
- `backend/ingestion.py`: validation, source/station/sensor upserts, raw
  payloads, normalized observations, anomaly/alert derivation, and run status.
- `backend/database.py`: SQLAlchemy models and PostgreSQL connection.
- `database/migrations/`: Alembic schema versioning and PostGIS/pgvector
  extension setup.
- `backend/api.py`: versioned reads, forecasts, knowledge operations,
  investigations, alerts, reports, and SSE.
- `backend/ml.py`, `backend/agents.py`, `backend/rag.py`: data-grounded
  analytics, supervisor workflow, and document retrieval.
- `data_engineering/`: one-shot ingestion and bounded history pruning.
- `airflow/dags/`: optional Airflow orchestration.
- `frontend/`: Next.js dashboard consuming FastAPI; it has no provider keys.

## Persistence

`source_observations` is the canonical, station-aware observation table.
`raw_api_payloads` stores the corresponding response snapshots separately.
`ingestion_runs` records outcome and accepted/rejected counts. Provider
measurement time and ingestion time are separate UTC timestamps. Raw request
URLs must not contain credentials; the FIRMS adapter explicitly redacts its
key from provenance.

Cities and providers are seeded as database rows by `python -m backend.seed`;
the five default cities are Chennai, Bengaluru, Hyderabad, Mumbai, and Delhi.
Provider coverage is not assumed. `api_sources.configured` and recent run
records are used for explicit status.

## Ingestion and events

Run ingestion independently from API requests:

```powershell
python -m data_engineering.ingest_once
python -m data_engineering.ingest_once --city chennai --source open_meteo
python -m data_engineering.ingest_once --city chennai --source openaq --historical-days 3
```

The API does not poll external providers for dashboard requests. A bounded
one-shot retry policy applies to transient HTTP failures. Redis publishes
post-ingestion and investigation events for SSE clients when configured; SSE
falls back to reading persisted changes when Redis is unavailable. Kafka is an
optional event mirror and is not required for the core data flow. Airflow is an
optional local scheduler and is not required for API or dashboard operation.
Its DAG invokes the application seed/ingestion CLI in a separate locked
virtual environment, avoiding the SQLAlchemy 1.4/2.x incompatibility between
Airflow and the application runtime.

## Trust boundaries and current limitations

- The API uses database-backed sessions and role checks. Investigation and
  report reads are owner-scoped except for administrators. Configure a unique
  auth secret, HTTPS-only cookies, exact CORS origins, and reviewed upload
  policy before public deployment.
- Document uploads are disabled by default.
- Gemini is optional. Without a server-side key, investigations use
  deterministic evidence summaries and explicitly report that no Gemini
  synthesis was run.
- OpenAQ and FIRMS require credentials. Unconfigured sources remain visibly
  unavailable; no synthetic measurements are inserted.
- Open-Meteo and CAMS-derived values are marked model output and are not
  represented as ground-station measurements.
- Forecasting reports unavailable until enough usable observations exist and
  the time-based evaluation split is possible.
- Copernicus CAMS global forecasts are ingested as modeled data when the ADS
  personal access token is configured. The adapter stores PM2.5/PM10 values
  with forecast-cycle provenance and polls no more than every 12 hours per
  city; it does not represent them as ground-station readings.
- The initial migration is a frozen Alembic baseline, including PostGIS and
  pgvector setup. Add a new reviewed migration for each subsequent model
  change; do not modify an already-applied revision.
- Production `DATABASE_URL` is supplied by the deployment environment; the
  local code default contains no username or password. Compose requires a
  non-empty `POSTGRES_PASSWORD`, with no reusable default.
