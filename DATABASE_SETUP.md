# Database setup and recovery

## Database architecture

AeroSense 2.0 uses PostgreSQL as its primary database. The custom database
image installs PostGIS and pgvector; Alembic migrations create the platform
schema and authentication tables. The original Streamlit application remains
separate and uses its bounded SQLite history.

Compose builds the application DSN from `POSTGRES_DB`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD`. There is no default PostgreSQL password. Supply a unique
URL-safe password and keep it in `.env` or a deployment secret store. The
database port is not published by the local Compose configuration.

## Local Compose setup

Docker Desktop with Compose v2 is required:

```powershell
Copy-Item .env.example .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32)); print('AUTH_SECRET_KEY=' + secrets.token_urlsafe(48))"
# Copy both generated values into .env before starting Compose.
docker compose up --build
```

The API container applies migrations and seeds reference data before starting.
Verify service health and readiness:

```powershell
Invoke-RestMethod http://localhost:8000/health/live
Invoke-RestMethod http://localhost:8000/health/ready
```

Provision the first administrator only after the API is ready:

```powershell
docker compose exec api python -m backend.auth bootstrap-admin
```

## Running migrations manually

For local Python development, set `DATABASE_URL` to the intended PostgreSQL
database and ensure the driver, server, PostGIS, and pgvector are available.
Use a URL-safe generated password so it can be placed in a DSN without
additional escaping.

```powershell
$env:DATABASE_URL = "postgresql+psycopg://aerosense:<URL_SAFE_PASSWORD>@localhost:5432/aerosense"
python -m alembic upgrade head
python -m alembic current
```

Do not run a live migration against production until a backup exists and the
migration has been reviewed and exercised on a disposable copy. The following
commands only render SQL and do not connect to a database:

```powershell
python -m alembic upgrade head --sql
python -m alembic downgrade 0002_authentication:base --sql
```

Offline SQL rendering is not evidence that a migration applied successfully.
In this environment, both upgrade and downgrade SQL rendered, but no live
PostgreSQL migration was possible because Docker/PostgreSQL tools were absent.

## Backup and restore

Use the deployment provider's supported backup mechanism for managed
PostgreSQL. For a local Compose database, create a custom-format dump inside
the database container, then copy it to a protected host location:

```powershell
docker compose exec db pg_dump -U aerosense -d aerosense -Fc -f /tmp/aerosense.dump
docker compose cp db:/tmp/aerosense.dump .\aerosense.dump
```

Treat the dump as sensitive data: restrict filesystem permissions, encrypt
off-host copies, define retention, and verify a restore regularly. Restore to a
new empty disposable database first. Do not overwrite or drop production data
without an explicit recovery plan and a verified backup.

The commands above are documented procedures, not a claim that backup or
restore has been executed in this environment.

## Schema and data notes

- `source_observations` stores normalized, station-aware measurements.
- `raw_api_payloads` stores response payloads and available provenance,
  including actual HTTP response status.
- `ingestion_runs` tracks provider ingestion outcomes.
- Spatial locations use PostGIS geography; RAG chunks use pgvector.
- Migration `0001_platform_schema` creates the initial schema and extensions;
  `0002_authentication` adds users, sessions, throttling, and resource-owner
  columns.
- Apply future model changes with new Alembic revisions; do not rewrite an
  already-applied migration.
