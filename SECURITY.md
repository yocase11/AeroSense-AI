# Security

## Authentication

All `/api/v1` endpoints require a database-backed session. Access and refresh
tokens are opaque random values stored only as keyed hashes in PostgreSQL.
Refresh rotates both tokens; logout revokes the session. Passwords are stored
as salted scrypt hashes. Login failures are throttled per client and account.

The `viewer`, `analyst`, and `admin` roles are accepted for operational API
features, including forecasts/reports, investigations, knowledge search, alert
acknowledgement, and document uploads when uploads are enabled. Investigation
and report records are owner-scoped; administrators can access all owners'
records. The account-provisioning CLI remains a trusted-host operation.

No default account or password is created. After migrations and API startup,
bootstrap the first administrator from a trusted host:

```powershell
docker compose exec api python -m backend.auth bootstrap-admin
```

Create additional users with:

```powershell
docker compose exec api python -m backend.auth create-user --role viewer
```

The command prompts for credentials and does not accept passwords as command
line arguments. Administrators can reset a password and revoke its sessions:

```powershell
docker compose exec api python -m backend.auth reset-password --email user@example.com
```

Local development requires a random `AUTH_SECRET_KEY` in `.env`
and may set `AUTH_COOKIE_SECURE=false` for HTTP on loopback. Use a unique
secret per environment and `AUTH_COOKIE_SECURE=true` behind HTTPS. The API
readiness check fails until the secret is configured.
For a frontend hosted on a different site from the API, use
`AUTH_COOKIE_SAMESITE=none` together with `AUTH_COOKIE_SECURE=true` and exact
CORS origins.

Mutating routes require the double-submit CSRF cookie/header pair. The browser
client bootstraps the CSRF cookie, sends credentials, and refreshes sessions
after access-token expiry. Do not broaden CORS origins beyond trusted browser
origins.

## Database credentials

Docker Compose has no default PostgreSQL password and refuses to start when
`POSTGRES_PASSWORD` is empty. Generate a unique URL-safe value for each
environment and keep it in the ignored `.env` file or a deployment secret
store. Compose uses this value to build the backend database URL.

## Supply-chain reproducibility

Container base images and Compose services are pinned to verified registry
manifest digests. The pgvector source checkout is pinned to commit
`2627c5ff775ae6d7aef0c430121ccf857842d2f2` (release `v0.8.0`).
Python package manifests still use version ranges without a resolved
hash-pinned lock. Container digest pinning alone does not make the complete
build reproducible; add and validate a Python lock before production release.

## Operational limits

This application has not undergone an independent penetration test. Protect
PostgreSQL and Redis with network controls and managed credentials in
deployment. Keep local Compose ports loopback-only, restrict document uploads,
and monitor database volume usage and audit/log retention. CORS and loopback
binding are not substitutes for authentication or network policy.
