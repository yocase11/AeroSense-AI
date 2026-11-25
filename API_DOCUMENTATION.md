# FastAPI reference

Start the backend after database migrations and seeding:

```powershell
uvicorn backend.main:app --reload
```

OpenAPI UI: `http://localhost:8000/docs`. All application endpoints are
prefixed with `/api/v1`; liveness/readiness are `/health/live` and
`/health/ready`.

## Implemented endpoints

| Area | Endpoint |
|---|---|
| Dashboard | `GET /dashboard/overview`, `GET /dashboard/live-events` (SSE) |
| Cities | `GET /cities`, `GET /cities/{city_id}` |
| Air | `GET /air-quality/current`, `GET /air-quality/history`, `GET /air-quality/stations` |
| Weather | `GET /weather/current`, `GET /weather/history` |
| Forecasts | `GET /forecasts`, `POST /forecasts/generate`, `GET /models/performance` |
| Sources | `GET /sources`, `GET /sources/health`, `GET /sources/ingestion-history` |
| Agents | `GET /agents`, `GET /agents/status`, `GET /agents/tasks`, `POST /agents/investigate`, `GET /agents/investigations/{id}`, investigation SSE |
| Events | `GET /anomalies`, `GET /events`, `GET /events/{id}/evidence` |
| Knowledge | `GET /knowledge/documents`, `POST /knowledge/documents`, `POST /knowledge/search` |
| Assistant | `POST /assistant/chat` |
| Alerts | `GET /alerts`, `POST /alerts/{id}/acknowledge` |
| Reports | `GET /reports`, `POST /reports/generate`, `GET /reports/{id}/pdf` |
| Authentication | `GET /auth/csrf`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me` |

History endpoints accept bounded time windows and page limits. Air-quality
history can be restricted to a `station_id`; station listings accept
`city_id` and `source_id` filters. Forecast generation accepts an optional
`station_id` and requires an OpenAQ ground station. Without one, the backend
selects a single OpenAQ station with the most valid recent records; it never
combines station or modeled-source observations for training. Forecast queries
return the latest generated run for the requested city, metric, and station.
`POST /agents/investigate` and `/assistant/chat` return HTTP 202 and an
investigation ID; poll its resource or subscribe to its SSE endpoint for
progress. In-process background execution is for the local profile and is not
a durable distributed job queue.

### Example requests

```powershell
$web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrf = Invoke-RestMethod "http://localhost:8000/api/v1/auth/csrf" -WebSession $web
$credential = Get-Credential -Message "AeroSense sign-in"
$body = @{
  email = $credential.UserName
  password = $credential.GetNetworkCredential().Password
} | ConvertTo-Json
$login = Invoke-RestMethod "http://localhost:8000/api/v1/auth/login" `
  -Method Post -WebSession $web -ContentType "application/json" `
  -Headers @{ "X-CSRF-Token" = $csrf.csrf_token } `
  -Body $body
$headers = @{ "X-CSRF-Token" = $login.csrf_token }
Invoke-RestMethod "http://localhost:8000/api/v1/dashboard/overview?city_id=chennai" `
  -WebSession $web
Invoke-RestMethod "http://localhost:8000/api/v1/forecasts/generate?city_id=chennai&metric=PM2.5&station_id=1" `
  -Method Post -WebSession $web -Headers $headers
```

## Authentication and authorization

The API uses database-backed session cookies. All `/api/v1` routes require an
authenticated session; call `GET /api/v1/auth/csrf` to receive the CSRF cookie
and token, then `POST /api/v1/auth/login` with the token in `X-CSRF-Token`.
The login response also returns the rotated CSRF token. The browser client
sends cookies and CSRF headers automatically. `POST /api/v1/auth/refresh`
rotates session tokens, `POST /api/v1/auth/logout`
revokes the current session, and `GET /api/v1/auth/me` returns the current
account and role.

Roles are `viewer`, `analyst`, and `admin`. All roles can use operational
features, including forecast generation, investigations, report generation,
alert acknowledgement, knowledge search, and document uploads when enabled.
Investigation and report records are visible only to their owner or an
administrator. Accounts are provisioned from the API host with
`python -m backend.auth bootstrap-admin` and
`python -m backend.auth create-user --role viewer|analyst|admin`.

## Status and security

Database failures produce HTTP 503 on readiness. Invalid or missing resource
IDs produce 404; invalid query/form values produce 422; document uploads are
403 while disabled and have 5 MB/250-page limits when enabled. Provider failures
are recorded in ingestion history and source health.

CORS is configurable but is not an access-control mechanism. Set a unique
`AUTH_SECRET_KEY`, use HTTPS so secure cookies are enabled, and keep the API
behind a trusted deployment boundary.
