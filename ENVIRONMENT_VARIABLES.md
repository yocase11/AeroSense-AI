# Environment variables

Copy `.env.example` to `.env` for local Compose use. Generate a unique,
URL-safe `POSTGRES_PASSWORD` and `AUTH_SECRET_KEY` before starting Compose.
Never commit `.env` or real API keys. Compose fails if the database password
is missing or empty.

| Name | Required | Purpose |
|---|---:|---|
| `DATABASE_URL` | Yes for backend | SQLAlchemy PostgreSQL DSN; Compose builds it from the `POSTGRES_*` variables. For local Python development, supply the DSN separately and use a URL-safe password. Use a managed secret in deployment. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Compose | Database initialization; `POSTGRES_PASSWORD` must be a unique, URL-safe secret and has no default. |
| `AUTH_SECRET_KEY` | Required for API readiness | Random, environment-specific key of at least 32 characters used to protect session tokens. Never reuse the example or commit the secret. |
| `AUTH_COOKIE_SECURE` | Optional, default true | Set to `false` only for local HTTP development; use `true` behind HTTPS. |
| `AUTH_COOKIE_SAMESITE` | Optional, default `lax` | Use `none` with secure cookies only when the trusted frontend and API are cross-site. |
| `OPENAQ_API_KEY` | For OpenAQ | Sent only as the `X-API-Key` request header. |
| `NASA_FIRMS_MAP_KEY` | For NASA FIRMS | FIRMS map key; redacted from stored request URL. |
| `cams` | For Copernicus CAMS | ADS personal access token for the CAMS global atmospheric-composition forecast dataset. Accept that dataset's terms in ADS first. Compose passes it to the backend as `CAMS_API_KEY`; never expose it in frontend variables. |
| `GEMINI_API_KEY` | Optional | Server-side Gemini synthesis and embedding. Never expose as `NEXT_PUBLIC_*`. |
| `GEMINI_PROVIDER` | Optional, default `api_key` | Select `api_key` or `vertex_ai` for investigation answer generation. |
| `GOOGLE_CLOUD_PROJECT` | Required for Vertex AI | Google Cloud project ID with the Vertex AI API enabled and billing configured. |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI, default `us-central1` | Vertex AI region for Gemini generation. |
| `GOOGLE_APPLICATION_CREDENTIALS_FILE` | Vertex Compose profile | Host path to an ADC JSON file; mounted read-only by `docker-compose.vertex.yml`. Keep it outside the repository. |
| `REDIS_URL` | Optional | Redis pub/sub for dashboard SSE. Database polling is the fallback. |
| `KAFKA_BOOTSTRAP_SERVERS` | Optional | Kafka event mirror; install the `streaming` extra as well. |
| `CORS_ORIGINS` | Optional | Comma-separated exact browser origins. CORS does not replace authentication. |
| `NEXT_PUBLIC_API_BASE_URL` | Frontend build | Public API base URL; contains no secrets. |
| `ALLOW_DOCUMENT_UPLOADS` | Optional, default false | Enables PDF/TXT/Markdown uploads; upload is restricted to administrators. |
| `AQI_ALERT_THRESHOLD` | Optional, default 100 | Integer threshold from 0 to 500, applied to qualifying 24-hour station windows. |
| `OBSERVATION_RETENTION_DAYS` | Optional, default 365 | Historical observation retention. |
| `RAW_PAYLOAD_RETENTION_DAYS` | Optional, default 30 | Raw response retention. |
| `EVENT_RETENTION_DAYS` | Optional, default 180 | Environmental event retention. |

The OpenAQ and FIRMS adapters report unconfigured status when their keys are
absent. Open-Meteo, NASA POWER, GDELT, and Overpass do not use a configured API
key in this implementation. Copernicus CAMS remains inactive without the ADS
token and ingests the global forecast product at most every 12 hours per city.
