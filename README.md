# AeroSense AI

AeroSense AI includes the original compact Streamlit air-quality monitor and
an additive AeroSense 2.0 platform scaffold. The original app remains in
`app.py`/`air_quality.py`; the platform adds provider adapters, normalized
PostgreSQL/PostGIS storage, FastAPI, a Next.js dashboard, ingestion jobs,
baseline forecasting/anomaly detection, document retrieval, and optional
Gemini/LangGraph investigations.
If Gemini is unavailable during an investigation, the platform returns and
stores a clearly labeled, source-cited summary of the collected evidence.
PM2.5 summaries compare only matching source/station readings; nearby weather
is presented as context rather than proof of cause. The answer generator tries
the configured primary Gemini model and a verified fallback model before using
the evidence-only summary.

The platform does not create synthetic environmental observations. A provider
is shown as unavailable when its credentials or data are missing. OpenAQ
requires an API key; Open-Meteo's weather and modeled air-quality products do
not. Model-based products are identified separately from ground-station
measurements.

## Repository map

| Path | Responsibility |
| --- | --- |
| `app.py`, `air_quality.py` | Original Streamlit monitor and its data logic. |
| `backend/` | FastAPI routes, authentication, database access, integrations, ingestion, forecasting, agents, and RAG. |
| `backend/agents.py` | LangGraph investigation workflow that gathers and cites evidence from specialist agents. |
| `backend/rag.py` | Document extraction and chunking, Gemini embeddings, pgvector semantic search, and a lexical fallback. |
| `frontend/` | Next.js and TypeScript dashboard. |
| `database/` | PostgreSQL image and Alembic schema migrations, including PostGIS and pgvector setup. |
| `data_engineering/`, `airflow/` | Ingestion commands and the optional Airflow DAG. |
| `powerbi/` | Power BI report definition and dashboard builder. |
| `Dockerfile`, `docker-compose*.yml` | Containerized API and local service stack, with optional deployment overlays. |

The investigation orchestration uses LangGraph; `langchain-core` is a
transitive dependency of LangGraph. Document retrieval is implemented in
`backend/rag.py` using the Gemini SDK and PostgreSQL/pgvector rather than
LangChain's retriever or vector-store APIs. GitHub's language percentages count
tracked source lines, not frameworks, dependencies, or platform features.

## Keep the workspace small

Python environments, npm dependencies/build output, local databases, secrets,
and Docker state should stay outside source control. For Python, use an
environment outside this directory. The frontend `node_modules` and `.next`
are ignored; use the Docker workflow or an external temporary workspace when
you do not want local build dependencies. Docker named volumes persist
separately and are not automatically size-limited.

## Original Streamlit monitor

The original monitor supports Chennai, Delhi, Mumbai, and Bengaluru and stores
bounded history in `data/air_quality.sqlite3`.

```powershell
py -m venv "$env:USERPROFILE\air-quality-env"
$pythonExe = Join-Path $env:USERPROFILE "air-quality-env\Scripts\python.exe"
& $pythonExe -m pip install --require-hashes -r requirements-dev.lock
& $pythonExe -m pip install --require-hashes -r requirements-build.lock
& $pythonExe -m pip install --no-deps --no-build-isolation -e .
$env:OPENAQ_API_KEY = "your-key"
& "$env:USERPROFILE\air-quality-env\Scripts\streamlit.exe" run app.py
```

Open-Meteo weather is available without a key. The OpenAQ key can instead be
entered in the app's password field and is not written to disk. The displayed
India AQI is an estimate based on available particulate measurements, not an
official government AQI.

## AeroSense 2.0 local platform

The default platform requires PostgreSQL with PostGIS and pgvector extensions.
For local development with Docker Compose:

```powershell
Copy-Item .env.example .env
# Generate separate values and copy them into .env as POSTGRES_PASSWORD and AUTH_SECRET_KEY.
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32)); print('AUTH_SECRET_KEY=' + secrets.token_urlsafe(48))"
# Add provider credentials to .env only if available.
docker compose up --build
```

The Next.js UI is at `http://localhost:3000`; FastAPI and its OpenAPI page are
at `http://localhost:8000` and `http://localhost:8000/docs`. To run a bounded
ingestion pass:

```powershell
docker compose exec api python -m data_engineering.ingest_once
```

For a real OpenAQ PM2.5 historical backfill across all active cities, run the
bounded three-month command. It fetches three contiguous 30-day windows per
city and reports each run separately:

```powershell
docker compose exec api python -m data_engineering.ingest_once --source openaq --historical-days 90
```

The backfill is limited to the nearest eight OpenAQ stations per city and PM2.5
history, with capped pagination per sensor. Check each run's accepted/rejected
counts and truncation metadata before treating the period as complete.

The database also provides the Power BI hourly PM2.5/weather view. See
[POWER_BI.md](./POWER_BI.md) for the read-only connection and dashboard setup.

The Compose profile starts PostgreSQL, Redis, API, and frontend. Kafka and
Airflow are optional profiles and consume additional disk and memory. API keys
are server-side only; never place them in `NEXT_PUBLIC_*` variables. Generate
A random `AUTH_SECRET_KEY` and set it in `.env`. Anyone can create an account
from the sign-in screen; self-registered accounts receive viewer access and
are signed in automatically. Viewers can use operational features including
investigations, forecasting, reports, knowledge search, alert acknowledgement,
and document uploads when enabled. Investigation and report data remains
owner-scoped. Create an administrator with the bootstrap command in
[DEPLOYMENT.md](./DEPLOYMENT.md).

If Gemini Developer API requests continue returning 503, Vertex AI can be
selected for answer generation. Enable Vertex AI and billing in the Google
Cloud project, configure Application Default Credentials on the host, then set
`GEMINI_PROVIDER=vertex_ai`, `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_APPLICATION_CREDENTIALS_FILE` in `.env`. Start the API with the Vertex
Compose overlay:

```powershell
docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d --build api
```

The ADC file is mounted read-only and must remain outside source control. The
Gemini Developer API key remains in use for document embeddings.
All `/api/v1` routes require a session. See [SECURITY.md](./SECURITY.md),
[ARCHITECTURE.md](./ARCHITECTURE.md), and
[ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md),
[DATABASE_SETUP.md](./DATABASE_SETUP.md), and
[OPERATIONS_RUNBOOK.md](./OPERATIONS_RUNBOOK.md) for setup and operations.
Current verification and blockers are summarized in
[PRODUCTION_READINESS_REPORT.md](./PRODUCTION_READINESS_REPORT.md).

## Tests

```powershell
python -m unittest test_air_quality test_aerosense_integrations test_platform test_api_integration
```

The tests use isolated fixtures/mocked HTTP responses and do not require live
provider credentials.

## Python dependency locks

`uv.lock` is the canonical cross-platform resolution for Python 3.11+. The
hash-pinned pip exports keep deployment profiles separate:

- `requirements-prod.lock`: API/runtime dependencies; installed by the API image.
- `requirements-dev.lock`: runtime plus pytest, httpx, and the legacy Streamlit UI.
- `requirements-streaming.lock`: runtime plus the optional Kafka client.
- `requirements-airflow.lock`: Airflow 2.11.1 for Python 3.12, resolved against
  Apache's matching constraints. The Airflow DAG runs application commands in
  a separate environment because Airflow's SQLAlchemy 1.x stack conflicts with
  the application's SQLAlchemy 2.x stack.
- `requirements-build.lock`: pinned setuptools and wheel for reproducible local
  project builds.

Regenerate the locks with `uv==0.8.22` from the repository root:

```powershell
py -m pip install uv==0.8.22
uv lock
uv lock --check
uv export --locked --no-dev --no-emit-project --format requirements.txt --output-file requirements-prod.lock
uv export --locked --extra test --extra legacy-ui --no-emit-project --format requirements.txt --output-file requirements-dev.lock
uv export --locked --extra streaming --no-emit-project --format requirements.txt --output-file requirements-streaming.lock
uv pip compile requirements-build.in --python-version 3.11 --generate-hashes --output-file requirements-build.lock
uv pip compile requirements-airflow.in --constraint https://raw.githubusercontent.com/apache/airflow/constraints-2.11.1/constraints-3.12.txt --python-version 3.12 --generate-hashes --output-file requirements-airflow.lock
```

Verify dependency consistency and known advisories with:

```powershell
uv lock --check
python -m pip check
uv tool run --from pip-audit==2.10.1 pip-audit --disable-pip -s osv -r requirements-build.lock
uv tool run --from pip-audit==2.10.1 pip-audit --disable-pip -s osv -r requirements-prod.lock
uv tool run --from pip-audit==2.10.1 pip-audit --disable-pip -s osv -r requirements-dev.lock
uv tool run --from pip-audit==2.10.1 pip-audit --disable-pip -s osv -r requirements-streaming.lock
uv tool run --from pip-audit==2.10.1 pip-audit --disable-pip -s osv -r requirements-airflow.lock
```

`uv lock` resolves package metadata across the declared Python range. The
project's runtime tests and lock installation still need to be run on each
supported Python interpreter and target container platform before release.
The current Airflow 2.11.1 profile still has known OSV advisories and is not
approved for deployment; see [PRODUCTION_READINESS_REPORT.md](./PRODUCTION_READINESS_REPORT.md).
