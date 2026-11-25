# Operations runbook

## Readiness and startup

Local Compose requires Docker Desktop with Compose v2. Create `.env` from the
example and set unique URL-safe values for `POSTGRES_PASSWORD` and
`AUTH_SECRET_KEY`. Compose intentionally refuses an empty database password.

```powershell
Copy-Item .env.example .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32)); print('AUTH_SECRET_KEY=' + secrets.token_urlsafe(48))"
# Copy both generated values into .env.
docker compose up --build
docker compose exec api python -m backend.auth bootstrap-admin
```

Check liveness and readiness:

```powershell
Invoke-RestMethod http://localhost:8000/health/live
Invoke-RestMethod http://localhost:8000/health/ready
```

`/health/ready` requires a configured authentication secret and a working
database connection. A ready response does not prove provider credentials,
ingestion freshness, Redis, or optional services are healthy.

## Ingestion and retention

The default Compose stack starts an ingestion worker after the API is healthy.
It polls active cities immediately, then every 45 minutes. OpenAQ and
Open-Meteo are polled at that cadence; slower or contextual providers retain
their own longer intervals. Provider timestamps can still lag when an upstream
service has not published newer observations. Do not run the optional Airflow
ingestion profile at the same time as this worker.

Run a bounded ingestion pass:

```powershell
docker compose exec api python -m data_engineering.ingest_once
docker compose exec api python -m data_engineering.ingest_once --city chennai --source open_meteo
```

Run configured database retention cleanup:

```powershell
docker compose exec api python -m backend.retention
```

Review source health and ingestion history through the authenticated API or
dashboard. Missing provider keys and empty provider results are expected
unavailable states, not successful data ingestion. Never replace missing
measurements with synthetic readings.

## Logs and troubleshooting

```powershell
docker compose logs --tail 200 ingestion
docker compose logs --tail 200 api
docker compose logs --tail 200 db
docker compose ps
```

- **API readiness returns 503:** verify `AUTH_SECRET_KEY`, PostgreSQL
  availability, and migrations. Do not expose the API until readiness is
  healthy.
- **Compose rejects configuration:** confirm `.env` contains a non-empty,
  unique `POSTGRES_PASSWORD`; a fallback is intentionally not provided.
- **No readings:** inspect provider configuration, source health, and
  ingestion history. OpenAQ and NASA FIRMS require credentials; provider
  failures remain explicit.
- **Dashboard reports a fetch error:** verify API readiness and
  `NEXT_PUBLIC_API_BASE_URL`; browser-visible readings require persisted API
  data.
- **SSE does not update:** check API logs and Redis reachability. Database
  polling is a fallback; Redis pub/sub and cross-process reliability have not
  been fully verified.

## Schema changes

Review Alembic revisions before applying them. The API container runs
`alembic upgrade head` during startup. For a controlled deployment, take a
database backup, apply migrations to a disposable/staging database first, and
verify application startup and CRUD before production rollout. See
[DATABASE_SETUP.md](./DATABASE_SETUP.md).

Do not use a downgrade as a routine rollback after writes have occurred;
some migrations drop tables or columns. Restore from a tested backup or use a
reviewed forward migration when preserving data is required.

## Backups, volume, and shutdown

Use managed PostgreSQL backups for hosted deployments. For local operations,
follow the dump and restore guidance in [DATABASE_SETUP.md](./DATABASE_SETUP.md).
The Compose database volume persists independently of the source directory.
Monitor its size and data retention.

Stop the local stack without deleting persistent data:

```powershell
docker compose down
```

Do not add `-v` unless intentional database and Kafka volume deletion has been
approved and the data is backed up.

## Security and optional services

- Keep `.env`, database dumps, and API keys out of version control.
- Use HTTPS, secure cookies, a secret manager, restricted CORS, and private
  database/Redis networks in production.
- The local Compose Kafka listener is PLAINTEXT and is for loopback-only
  development; do not expose it as a production broker.
- Airflow and Kafka are optional. The documented Airflow profile uses SQLite
  metadata and `SequentialExecutor` for local single-process development only.
  Airflow and application dependencies are installed in isolated environments
  because their SQLAlchemy major versions differ.
- The pinned Airflow profile currently has known OSV advisories. Do not start
  it in a public or production environment until its dependencies and image
  are upgraded, audited, and runtime-tested.
- Python dependencies use hash-pinned profile locks. Regenerate and audit
  them using the commands in [README.md](./README.md); do not hand-edit lock
  outputs or install the Airflow lock into the application environment.
- No live PostgreSQL, Docker, backup/restore, production TLS, or independent
  penetration test was verified in this environment.
