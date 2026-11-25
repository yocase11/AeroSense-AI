# Local and cloud deployment

## Local Docker Compose

Prerequisites: Docker Desktop with Compose v2 and a supported build platform.
The database image builds PostGIS and pgvector extensions. Docker stores named
database volumes outside the source tree; no local database or Node dependency
directory is committed.

```powershell
Copy-Item .env.example .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32)); print('AUTH_SECRET_KEY=' + secrets.token_urlsafe(48))"
# Copy both generated values into .env. Compose refuses to start without the database password.
# Add provider credentials to .env only if you have them.
docker compose up --build
```

Open `http://localhost:3000` for Next.js and `http://localhost:8000/docs` for
FastAPI. The API container runs Alembic migrations and seeds reference data
before starting. Without provider credentials, the dashboard displays
unconfigured sources and empty-state messages. It does not fabricate readings.
Anyone can create an account from the sign-in screen. Self-registered accounts
receive viewer access and are signed in automatically. Registration requires
an email address and a password of at least 12 characters, and is rate-limited
per client IP. Viewers can run evidence-based investigations, while report
generation, forecasting, knowledge search, alert acknowledgement, and document
uploads (when enabled) are also available to viewers. Investigation and report
records remain owner-scoped. Email verification is not currently enabled.

Create the initial admin account after the API starts and before relying on
public registration:

```powershell
docker compose exec api python -m backend.auth bootstrap-admin
```

Additional accounts can also be created with the prompt-based CLI, for example:

```powershell
docker compose exec api python -m backend.auth create-user --role analyst
```

The command prompts for the email and password. Published application, Airflow,
and optional Kafka ports bind to loopback only. Local Compose sets `AUTH_COOKIE_SECURE=false`;
set it to `true` behind HTTPS in any non-local deployment. Generate a unique
`AUTH_SECRET_KEY` for every environment. See [SECURITY.md](./SECURITY.md).

To run local ingestion once:

```powershell
docker compose exec api python -m data_engineering.ingest_once
```

Optional Kafka:

```powershell
# Set these in .env to build the API with the optional Kafka client.
# AEROSENSE_REQUIREMENTS_LOCK=requirements-streaming.lock
# KAFKA_BOOTSTRAP_SERVERS=kafka:9092
# The API uses the internal kafka:9092 listener; host tools use localhost:9092.
docker compose --profile streaming up --build
```

The default API image installs only `requirements-prod.lock`. The streaming
profile remains inactive unless the optional client lock and bootstrap address
are selected. For local Python development, install
`requirements-streaming.lock` with `pip --require-hashes`.

Optional Airflow:

```powershell
docker compose --profile airflow up --build
```

Airflow's local profile uses its own SQLite metadata database and
`SequentialExecutor`, so it is for single-process development only. Kafka and
Airflow are not required for the default platform profile. Their images and
databases increase disk and memory use; stop/remove optional profiles when not
in use. The current Airflow dependency lock still has known OSV advisories;
do not use that profile in a public or production environment until its lock
and image have been upgraded and scanned clean. Docker named volumes persist
until explicitly removed.

Run retention cleanup on demand:

```powershell
docker compose exec api python -m backend.retention
```

## Local Python development

Keep environments outside the repository to limit workspace size:

```powershell
py -3.12 -m venv "$env:USERPROFILE\aerosense-env"
$pythonExe = Join-Path $env:USERPROFILE "aerosense-env\Scripts\python.exe"
& $pythonExe -m pip install --require-hashes -r requirements-dev.lock
& $pythonExe -m pip install --require-hashes -r requirements-build.lock
& $pythonExe -m pip install --no-deps --no-build-isolation -e .
Copy-Item .env.example .env
$env:AUTH_SECRET_KEY = (& $pythonExe -c "import secrets; print(secrets.token_urlsafe(48))").Trim()
$env:AUTH_COOKIE_SECURE = "false"
$env:AUTH_COOKIE_SAMESITE = "lax"
$dbPassword = (& $pythonExe -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$env:DATABASE_URL = "postgresql+psycopg://aerosense:$dbPassword@localhost:5432/aerosense"
& "$env:USERPROFILE\aerosense-env\Scripts\alembic.exe" upgrade head
& $pythonExe -m backend.seed
& $pythonExe -m backend.auth bootstrap-admin
& "$env:USERPROFILE\aerosense-env\Scripts\uvicorn.exe" backend.main:app --reload
```

In another terminal:

```powershell
cd frontend
npm ci
$env:NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000/api/v1"
npm run dev
```

The legacy Streamlit UI remains available separately with `streamlit run app.py`
and its SQLite database. It is not the AeroSense 2.0 PostgreSQL API client.
The Airflow profile installs the hash-pinned Airflow environment separately
and executes application commands in an isolated virtual environment using
`requirements-prod.lock`.

## Cloud profile guidance

- **Frontend:** deploy `frontend/` to Vercel. Set
  `NEXT_PUBLIC_API_BASE_URL` to the HTTPS backend URL at build time.
- **Database:** use managed PostgreSQL with `postgis` and `vector` extensions
  enabled (for example, a compatible Supabase project). Apply Alembic migrations
  from a trusted deployment job. Managed service extension availability and
  limits vary by plan.
- **Backend:** deploy the Python API container to Render or another container
  host with an always-available managed PostgreSQL DSN and secret store.
  Configure health probes at `/health/live` and `/health/ready`.
- **Redis:** use a managed Redis-compatible service for cross-instance SSE
  events. Without it, current fallback polling is process/database based.
- **Scheduler:** Airflow requires a continuously running scheduler, metadata
  database, and worker resources. Do not assume it is free or available on a
  sleeping web-service tier. A managed cron/worker service can run the
  one-shot ingestion command at a bounded interval.
- **Kafka:** optional and likely paid/operationally expensive when hosted.
- **Gemini/OpenAQ/FIRMS:** provide server-side secrets only. Verify each
  account's quota and terms before enabling.

No cloud account was available during implementation, so cloud deployments,
managed extension availability, GitHub Actions, and provider credentials have
not been externally verified. This is a deployment blueprint, not a claim of
production readiness or guaranteed free-tier uptime.

## Backups and size

Set retention variables for database policy, back up PostgreSQL independently,
and test restore procedures. The local Compose database uses a Docker named
volume; it is persistent but not size-limited automatically. Restrict raw
payload retention and monitor volume usage. Do not use the project folder as a
production database volume.
