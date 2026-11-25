# Environmental data contract

## Normalized observation

Each persisted observation includes:

| Field | Meaning |
|---|---|
| `id` | Database observation identifier. |
| `source_id` | Provider key, such as `openaq` or `open_meteo_air_quality`. |
| `provider_record_id` | Provider/sensor/time identity used for idempotent ingestion. |
| `city_id` | Seeded city foreign key. |
| `station_id`, `sensor_id` | Provider station/sensor foreign keys where available; null for gridded model products. |
| `metric`, `value` | Canonical parameter name and finite numeric value. |
| `original_unit`, `canonical_unit` | Provider-declared unit and normalized display/storage unit. |
| `observed_at` | Provider measurement/model time normalized to UTC. |
| `ingested_at` | Database insertion time in UTC. |
| `latitude`, `longitude` | WGS84 position when the provider supplies one. |
| `quality_status`, `quality_score` | Acceptance flag and transparent weighted validation checks. |
| `raw_payload_id` | Link to stored provider response, when available. |
| `metadata_json` | Source-specific attributes, quality checks, and freshness status. |

`source_observations` is intentionally unified so additional parameter families
do not require duplicate storage logic. Station identity remains part of the
record; city-level averages must have an explicit station/aggregation policy.

## Raw payloads and privacy

Raw response JSON is stored separately with source, city, resource, fetch time,
response status, content hash, and optional sanitized request URL. Credentials
and authorization headers are never persisted. Response bodies are limited to
10 MB in the HTTP client. Raw payload retention defaults to 30 days; observation
retention defaults to 365 days; event retention defaults to 180 days. The
`python -m backend.retention` command applies these policies. Expired raw
payload references must be detached before deletion.

## Canonical units

- PM2.5, PM10, NO2, SO2, CO, O3: `µg/m³`. Gas ppm/ppb and mg/m³ values are
  converted using documented molecular-weight/standard-volume assumptions
  only for known gases; unsupported units are rejected by the adapter.
- Temperature: `°C`; Fahrenheit converted to Celsius.
- Wind speed: `km/h`; m/s converted.
- Humidity: `%`; direction: `°`; precipitation: `mm`; pressure: `hPa`.
- NASA POWER solar radiation is retained with the provider's declared
  `kWh/m²/day` unit.

Never compare values with different canonical units. Preserve the original
unit for provenance.

## Freshness and quality

Current-data freshness windows are provider-specific and recorded in
`metadata_json.freshness_status`. Historical backfills can be stale for a
current dashboard but remain valid historical records. Future timestamps more
than ten minutes ahead, non-finite values, impossible coordinate/value ranges,
and unknown units are rejected. The `quality_score` is a deterministic
weighted score of validation checks, not a statistical confidence value.

## Forecast provenance

Forecast training uses quality-valid OpenAQ measurements for one station and
metric within a bounded history window. If the caller does not specify a
station, the API chooses one station with the most valid recent records; it
does not merge other stations or gridded model products. Forecast and
evaluation records retain the station ID. Predictions remain unavailable
until the minimum sample count, cadence, and chronological evaluation-window
requirements are met.

## AQI and alerts

India AQI uses PM2.5/PM10 breakpoints already used by the legacy prototype.
The API calculates an estimated 24-hour station AQI only when a station has at
least 16 valid samples spanning at least 18 hours for a pollutant. Partial
pollutant results are marked `partial_pollutant_window`; values are estimates,
not official CPCB readings. Threshold alerts require a complete station-level
window and use `AQI_ALERT_THRESHOLD` (default 100). AQI concentration
breakpoints are not interchangeable with instantaneous concentration limits.

## Events and context

NASA FIRMS detections and GDELT stories are contextual event records, not
ground-level pollution observations. GDELT articles are not proof of
environmental causality. OSM features are geographic context only. Each is
stored with its source and citation URL when available.
