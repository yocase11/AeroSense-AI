from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
PARAMETERS = {
    "pm2_5": ("PM2.5", "µg/m³"),
    "pm25": ("PM2.5", "µg/m³"),
    "pm2.5": ("PM2.5", "µg/m³"),
    "pm10": ("PM10", "µg/m³"),
    "no2": ("NO2", "µg/m³"),
    "so2": ("SO2", "µg/m³"),
    "co": ("CO", "µg/m³"),
    "o3": ("O3", "µg/m³"),
    "temperature_2m": ("Temperature", "°C"),
    "relative_humidity_2m": ("Humidity", "%"),
    "wind_speed_10m": ("Wind speed", "km/h"),
    "wind_direction_10m": ("Wind direction", "°"),
    "precipitation": ("Precipitation", "mm"),
    "surface_pressure": ("Surface pressure", "hPa"),
}
GAS_MOLECULAR_WEIGHTS = {"CO": 28.01, "NO2": 46.0055, "SO2": 64.066, "O3": 48.0}


class ProviderError(RuntimeError):
    def __init__(self, source: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.source = source
        self.status_code = status_code


@dataclass(frozen=True)
class CityPoint:
    city_id: str
    name: str
    latitude: float
    longitude: float


@dataclass
class ProviderBatch:
    source: str
    city_id: str
    fetched_at: datetime
    raw_payloads: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class HttpClient:
    def __init__(self, timeout: float = 15, attempts: int = 3):
        self.timeout = timeout
        self.attempts = attempts

    def request(
        self,
        url: str,
        *,
        source: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[bytes, dict[str, str], int]:
        request = Request(
            url,
            data=body,
            headers={
                "Accept": "application/json, text/csv;q=0.9, */*;q=0.8",
                "User-Agent": "AeroSenseAI/2.0 (environmental monitoring)",
                **(headers or {}),
            },
            method="POST" if body is not None else "GET",
        )
        for attempt in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ProviderError(source, "Response exceeded the 10 MB limit.")
                    status_code = response.getcode()
                    if status_code is None:
                        raise ProviderError(source, "Provider response had no HTTP status.")
                    return payload, dict(response.headers.items()), status_code
            except HTTPError as error:
                if error.code not in TRANSIENT_STATUSES or attempt + 1 == self.attempts:
                    logger.warning(
                        "Provider request failed: source=%s status=%s",
                        source,
                        error.code,
                    )
                    raise ProviderError(
                        source, f"Provider returned HTTP {error.code}.", error.code
                    ) from error
                retry_after = self._retry_after(error.headers.get("Retry-After"))
                time.sleep(
                    min(retry_after if retry_after is not None else 0.5 * (2**attempt), 8)
                )
            except (TimeoutError, URLError) as error:
                if attempt + 1 == self.attempts:
                    logger.warning("Provider request timed out or failed: source=%s", source)
                    raise ProviderError(
                        source, "Provider request failed after bounded retries."
                    ) from error
                time.sleep(0.5 * (2**attempt))
        raise ProviderError(source, "Provider request failed.")

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def get_json(
        self, url: str, *, source: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        decoded, _ = self.get_json_with_status(url, source=source, headers=headers)
        return decoded

    def get_json_with_status(
        self, url: str, *, source: str, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], int]:
        payload, _, status_code = self.request(url, source=source, headers=headers)
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderError(source, "Provider returned invalid JSON.") from error
        if not isinstance(decoded, dict):
            raise ProviderError(source, "Provider returned an unexpected JSON shape.")
        return decoded, status_code


def _utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _canonical_unit(original: str | None, fallback: str) -> str:
    if not original:
        return fallback
    normalized = original.strip().lower().replace(" ", "").replace("μ", "µ")
    aliases = {
        "ug/m3": "µg/m³",
        "µg/m3": "µg/m³",
        "µg/m^3": "µg/m³",
        "°c": "°C",
        "celsius": "°C",
        "km/h": "km/h",
        "kph": "km/h",
        "m/s": "m/s",
        "%": "%",
        "mm": "mm",
        "hpa": "hPa",
        "pa": "Pa",
        "kpa": "kPa",
        "ppm": "ppm",
        "ppb": "ppb",
    }
    return aliases.get(normalized, original.strip())


def _normalize_value(
    metric: str, value: float, original_unit: str | None, fallback_unit: str
) -> tuple[float, str] | None:
    unit = _canonical_unit(original_unit, fallback_unit)
    if metric in {"PM2.5", "PM10", "CO", "NO2", "SO2", "O3"}:
        normalized = unit.lower().replace(" ", "").replace("μ", "µ")
        if normalized in {"µg/m³", "ug/m³", "µg/m3"}:
            return value, "µg/m³"
        if normalized in {"mg/m³", "mg/m3"}:
            return value * 1000, "µg/m³"
        if normalized in {"kgm-3", "kg/m³", "kg/m3", "kg/m^3"}:
            return value * 1_000_000_000, "µg/m³"
        molecular_weight = GAS_MOLECULAR_WEIGHTS.get(metric)
        if molecular_weight is not None and normalized == "ppm":
            return value * molecular_weight * 1000 / 24.45, "µg/m³"
        if molecular_weight is not None and normalized == "ppb":
            return value * molecular_weight / 24.45, "µg/m³"
        return None
    if metric == "Temperature":
        if unit == "°C":
            return value, "°C"
        if unit.lower() in {"°f", "fahrenheit"}:
            return (value - 32) * 5 / 9, "°C"
        return None
    if metric == "Wind speed":
        if unit == "km/h":
            return value, "km/h"
        if unit == "m/s":
            return value * 3.6, "km/h"
        return None
    if metric == "Surface pressure":
        if unit == "hPa":
            return value, "hPa"
        if unit == "Pa":
            return value / 100, "hPa"
        if unit == "kPa":
            return value * 10, "hPa"
        return None
    expected = {
        "Humidity": "%",
        "Wind direction": "°",
        "Precipitation": "mm",
        "European AQI": "index",
        "US AQI": "index",
        "Solar radiation": "kWh/m²/day",
    }.get(metric)
    if expected is not None and unit.lower() == expected.lower():
        return value, expected
    return None


def _canonical_metric(name: str) -> tuple[str, str] | None:
    normalized = name.strip().lower().replace(" ", "").replace("-", "_")
    if normalized in PARAMETERS:
        return PARAMETERS[normalized]
    return None


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    value = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(
        math.radians(lat2)
    ) * math.sin(d_lon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(value))


class OpenAQAdapter:
    source = "openaq"
    MAX_STATIONS_PER_CITY = 8
    MAX_HISTORY_PAGES_PER_SENSOR = 10

    def __init__(self, api_key: str | None, client: HttpClient | None = None):
        self.api_key = api_key
        self.client = client or HttpClient()

    def fetch(self, city: CityPoint) -> ProviderBatch:
        if not self.api_key:
            raise ProviderError(self.source, "OpenAQ API key is not configured.")
        query = urlencode(
            {
                "coordinates": f"{city.latitude},{city.longitude}",
                "radius": 25000,
                "limit": 100,
                "page": 1,
            }
        )
        headers = {"X-API-Key": self.api_key}
        locations, locations_status = self.client.get_json_with_status(
            f"https://api.openaq.org/v3/locations?{query}",
            source=self.source,
            headers=headers,
        )
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "locations",
                "request_url": f"https://api.openaq.org/v3/locations?{query}",
                "response_status": locations_status,
                "payload": locations,
            }
        )
        candidates = []
        for location in locations.get("results", []):
            if not isinstance(location, dict) or not isinstance(location.get("id"), int):
                continue
            sensors = location.get("sensors", [])
            if not isinstance(sensors, list):
                continue
            sensor_by_id = {
                sensor.get("id"): sensor
                for sensor in sensors
                if isinstance(sensor, dict) and isinstance(sensor.get("id"), int)
            }
            if not any(
                _canonical_metric(str((sensor.get("parameter") or {}).get("name", "")))
                in {("PM2.5", "µg/m³"), ("PM10", "µg/m³")}
                for sensor in sensor_by_id.values()
            ):
                continue
            point = location.get("coordinates") or {}
            latitude, longitude = point.get("latitude"), point.get("longitude")
            if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                continue
            distance = _distance_km(
                city.latitude, city.longitude, float(latitude), float(longitude)
            )
            if distance <= 25:
                candidates.append((distance, location, sensor_by_id))

        candidates.sort(key=lambda item: item[0])
        for _, location, sensor_by_id in candidates[: self.MAX_STATIONS_PER_CITY]:
            latest, latest_status = self.client.get_json_with_status(
                f"https://api.openaq.org/v3/locations/{location['id']}/latest",
                source=self.source,
                headers=headers,
            )
            batch.raw_payloads.append(
                {
                    "resource": "latest",
                    "location_id": location["id"],
                    "response_status": latest_status,
                    "payload": latest,
                }
            )
            for record in latest.get("results", []):
                if not isinstance(record, dict):
                    continue
                sensor = sensor_by_id.get(record.get("sensorsId"))
                if sensor is None:
                    continue
                parameter = sensor.get("parameter") or {}
                metric = _canonical_metric(str(parameter.get("name", "")))
                value = _finite_number(record.get("value"))
                time_data = record.get("datetime") or {}
                observed_at = time_data.get("utc")
                if metric is None or value is None or not isinstance(observed_at, str):
                    continue
                normalized = _normalize_value(
                    metric[0], value, parameter.get("units"), metric[1]
                )
                if normalized is None:
                    continue
                value, canonical_unit = normalized
                point = location.get("coordinates") or {}
                batch.observations.append(
                    {
                        "city_id": city.city_id,
                        "source": self.source,
                        "provider_record_id": (
                            f"{location['id']}:{sensor['id']}:{observed_at}"
                        ),
                        "station_provider_id": str(location["id"]),
                        "station_name": str(location.get("name") or location.get("locality") or city.name),
                        "sensor_provider_id": str(sensor["id"]),
                        "metric": metric[0],
                        "value": value,
                        "original_unit": str(parameter.get("units") or ""),
                        "unit": canonical_unit,
                        "observed_at": _utc_datetime(observed_at),
                        "latitude": point.get("latitude"),
                        "longitude": point.get("longitude"),
                        "quality_status": "valid",
                        "metadata": {
                            "provider": location.get("provider"),
                            "license": location.get("licenses"),
                        },
                    }
                )
        return batch

    def fetch_history(
        self, city: CityPoint, start: datetime, end: datetime, limit: int = 1000
    ) -> ProviderBatch:
        if not self.api_key:
            raise ProviderError(self.source, "OpenAQ API key is not configured.")
        start = _utc_datetime(start)
        end = _utc_datetime(end)
        if end <= start:
            raise ValueError("Historical range end must follow its start.")
        if end - start > timedelta(days=30):
            raise ValueError("OpenAQ backfills are limited to 30 days per request.")
        location_query = urlencode(
            {
                "coordinates": f"{city.latitude},{city.longitude}",
                "radius": 25000,
                "limit": 100,
                "page": 1,
            }
        )
        headers = {"X-API-Key": self.api_key}
        locations, locations_status = self.client.get_json_with_status(
            f"https://api.openaq.org/v3/locations?{location_query}",
            source=self.source,
            headers=headers,
        )
        candidates = []
        for location in locations.get("results", []):
            if not isinstance(location, dict) or not isinstance(location.get("id"), int):
                continue
            point = location.get("coordinates") or {}
            latitude, longitude = point.get("latitude"), point.get("longitude")
            if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                continue
            distance = _distance_km(
                city.latitude, city.longitude, float(latitude), float(longitude)
            )
            if distance > 25:
                continue
            for sensor in location.get("sensors", []):
                if not isinstance(sensor, dict) or not isinstance(sensor.get("id"), int):
                    continue
                parameter = sensor.get("parameter") or {}
                metric = _canonical_metric(str(parameter.get("name", "")))
                if metric is not None and metric[0] == "PM2.5":
                    candidates.append((distance, location, sensor))
        candidates.sort(key=lambda item: item[0])

        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.metadata = {
            "history_metrics": ["PM2.5"],
            "history_pages": 0,
            "truncated_sensors": [],
        }
        batch.raw_payloads.append(
            {
                "resource": "locations",
                "request_url": f"https://api.openaq.org/v3/locations?{location_query}",
                "response_status": locations_status,
                "payload": locations,
            }
        )
        unique_sensors = set()
        selected_locations: set[int] = set()
        for _, location, sensor in candidates:
            location_id = location["id"]
            if (
                location_id not in selected_locations
                and len(selected_locations) >= self.MAX_STATIONS_PER_CITY
            ):
                continue
            selected_locations.add(location_id)
            if sensor["id"] in unique_sensors:
                continue
            unique_sensors.add(sensor["id"])
            parameter = sensor.get("parameter") or {}
            metric = _canonical_metric(str(parameter.get("name", "")))
            if metric is None:
                continue
            point = location.get("coordinates") or {}
            page_limit = max(1, min(limit, 1000))
            for page in range(1, self.MAX_HISTORY_PAGES_PER_SENSOR + 1):
                query = urlencode(
                    {
                        "datetime_from": start.isoformat().replace("+00:00", "Z"),
                        "datetime_to": end.isoformat().replace("+00:00", "Z"),
                        "limit": page_limit,
                        "page": page,
                    }
                )
                request_url = (
                    f"https://api.openaq.org/v3/sensors/{sensor['id']}/measurements?"
                    f"{query}"
                )
                payload, response_status = self.client.get_json_with_status(
                    request_url,
                    source=self.source,
                    headers=headers,
                )
                records = payload.get("results")
                if not isinstance(records, list):
                    raise ProviderError(
                        self.source,
                        f"OpenAQ returned invalid measurement results for sensor {sensor['id']}.",
                    )
                batch.metadata["history_pages"] += 1
                batch.raw_payloads.append(
                    {
                        "resource": "measurements",
                        "request_url": request_url,
                        "location_id": location["id"],
                        "sensor_id": sensor["id"],
                        "page": page,
                        "response_status": response_status,
                        "payload": payload,
                    }
                )
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    value = _finite_number(record.get("value"))
                    datetime_data = record.get("datetime")
                    timestamp = (
                        datetime_data.get("utc")
                        if isinstance(datetime_data, dict)
                        else None
                    )
                    if not isinstance(timestamp, str):
                        period = record.get("period")
                        datetime_from = (
                            period.get("datetimeFrom")
                            if isinstance(period, dict)
                            else None
                        )
                        timestamp = (
                            datetime_from.get("utc")
                            if isinstance(datetime_from, dict)
                            else None
                        )
                    if value is None or not isinstance(timestamp, str):
                        continue
                    normalized = _normalize_value(
                        metric[0], value, parameter.get("units"), metric[1]
                    )
                    if normalized is None:
                        continue
                    observed_at = _utc_datetime(timestamp)
                    batch.observations.append(
                        {
                            "city_id": city.city_id,
                            "source": self.source,
                            "provider_record_id": (
                                f"{location['id']}:{sensor['id']}:{observed_at.isoformat()}"
                            ),
                            "raw_payload_page": page,
                            "station_provider_id": str(location["id"]),
                            "station_name": str(location.get("name") or city.name),
                            "sensor_provider_id": str(sensor["id"]),
                            "metric": metric[0],
                            "value": normalized[0],
                            "original_unit": str(parameter.get("units") or metric[1]),
                            "unit": normalized[1],
                            "observed_at": observed_at,
                            "latitude": point.get("latitude"),
                            "longitude": point.get("longitude"),
                            "quality_status": "valid",
                            "metadata": {
                                "provider": location.get("provider"),
                                "license": location.get("licenses"),
                                "historical_backfill": True,
                            },
                        }
                    )
                if len(records) < page_limit:
                    break
                if page == self.MAX_HISTORY_PAGES_PER_SENSOR:
                    batch.metadata["truncated_sensors"].append(sensor["id"])
        return batch


class OpenMeteoAdapter:
    source = "open_meteo"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()

    def fetch_weather(self, city: CityPoint) -> ProviderBatch:
        query = urlencode(
            {
                "latitude": city.latitude,
                "longitude": city.longitude,
                "current": (
                    "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                    "wind_direction_10m,precipitation,surface_pressure"
                ),
                "hourly": (
                    "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                    "wind_direction_10m,precipitation,surface_pressure"
                ),
                "past_days": 1,
                "forecast_days": 2,
                "timezone": "UTC",
            }
        )
        payload, response_status = self.client.get_json_with_status(
            f"https://api.open-meteo.com/v1/forecast?{query}", source=self.source
        )
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "forecast",
                "response_status": response_status,
                "payload": payload,
            }
        )
        current = payload.get("current")
        if isinstance(current, dict) and isinstance(current.get("time"), str):
            units = payload.get("current_units", {})
            for key, raw_value in current.items():
                metric = _canonical_metric(key)
                value = _finite_number(raw_value)
                if metric is None or value is None or key == "time":
                    continue
                normalized = _normalize_value(metric[0], value, units.get(key), metric[1])
                if normalized is None:
                    continue
                batch.observations.append(
                    self._observation(
                        city,
                        metric,
                        normalized[0],
                        normalized[1],
                        current["time"],
                        self.source,
                        units.get(key),
                    )
                )
        return batch

    def fetch_air_quality(self, city: CityPoint) -> ProviderBatch:
        query = urlencode(
            {
                "latitude": city.latitude,
                "longitude": city.longitude,
                "current": "european_aqi,us_aqi,pm10,pm2_5,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,ozone",
                "hourly": "pm10,pm2_5,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,ozone",
                "timezone": "UTC",
            }
        )
        source = "open_meteo_air_quality"
        payload, response_status = self.client.get_json_with_status(
            f"https://air-quality-api.open-meteo.com/v1/air-quality?{query}",
            source=source,
        )
        batch = ProviderBatch(source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "air-quality",
                "response_status": response_status,
                "payload": payload,
            }
        )
        current = payload.get("current")
        units = payload.get("current_units", {})
        if isinstance(current, dict) and isinstance(current.get("time"), str):
            for key, raw_value in current.items():
                if key in {"european_aqi", "us_aqi"}:
                    metric = ("European AQI" if key == "european_aqi" else "US AQI", "index")
                else:
                    name = {
                        "pm2_5": "PM2.5",
                        "nitrogen_dioxide": "NO2",
                        "sulphur_dioxide": "SO2",
                        "carbon_monoxide": "CO",
                        "ozone": "O3",
                        "pm10": "PM10",
                    }.get(key)
                    if name is None:
                        continue
                    metric = (name, "µg/m³")
                value = _finite_number(raw_value)
                if value is not None:
                    normalized = _normalize_value(
                        metric[0], value, units.get(key), metric[1]
                    )
                    if normalized is None:
                        continue
                    batch.observations.append(
                        self._observation(
                            city,
                            metric,
                            normalized[0],
                            normalized[1],
                            current["time"],
                            source,
                            units.get(key),
                        )
                    )
        return batch

    @staticmethod
    def _observation(
        city: CityPoint,
        metric: tuple[str, str],
        value: float,
        unit: str | None,
        time_value: str,
        source: str,
        original_unit: str | None = None,
    ) -> dict[str, Any]:
        observed_at = _utc_datetime(time_value)
        return {
            "city_id": city.city_id,
            "source": source,
            "provider_record_id": f"{city.city_id}:{metric[0]}:{observed_at.isoformat()}",
            "station_provider_id": None,
            "station_name": city.name,
            "sensor_provider_id": None,
            "metric": metric[0],
            "value": value,
            "original_unit": original_unit or unit or metric[1],
            "unit": unit or metric[1],
            "observed_at": observed_at,
            "latitude": city.latitude,
            "longitude": city.longitude,
            "quality_status": "valid",
            "metadata": {"measurement_kind": "modelled_current"},
        }


class CopernicusCamsAdapter:
    source = "copernicus_cams"
    dataset = "cams-global-atmospheric-composition-forecasts"
    api_url = "https://ads.atmosphere.copernicus.eu/api"
    max_download_bytes = 20 * 1024 * 1024
    variables = {
        "PM2.5": "particulate_matter_2.5um",
        "PM10": "particulate_matter_10um",
    }

    def __init__(
        self,
        api_key: str | None,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.api_key = api_key
        self.client_factory = client_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, city: CityPoint) -> ProviderBatch:
        if not self.api_key:
            raise ProviderError(self.source, "CAMS API key is not configured.")

        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cycle = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        valid_time = now.replace(minute=0, second=0, microsecond=0)
        leadtime = int((valid_time - cycle).total_seconds() // 3600)
        request = {
            "variable": list(self.variables.values()),
            "date": [cycle.strftime("%Y-%m-%d")],
            "time": ["00:00"],
            "leadtime_hour": [str(leadtime)],
            "type": ["forecast"],
            "data_format": "netcdf_zip",
            "area": [
                min(90.0, city.latitude + 0.5),
                max(-180.0, city.longitude - 0.5),
                max(-90.0, city.latitude - 0.5),
                min(180.0, city.longitude + 0.5),
            ],
        }

        try:
            if self.client_factory is None:
                import cdsapi

                client = cdsapi.Client(
                    url=self.api_url, key=self.api_key, quiet=True
                )
            else:
                client = self.client_factory(
                    url=self.api_url, key=self.api_key, quiet=True
                )
            with tempfile.TemporaryDirectory(prefix="aerosense-cams-") as directory:
                target = Path(directory) / "cams-data.zip"
                client.retrieve(self.dataset, request, str(target))
                if not target.is_file():
                    raise ProviderError(
                        self.source, "CAMS completed without producing a data file."
                    )
                size = target.stat().st_size
                if size > self.max_download_bytes:
                    raise ProviderError(
                        self.source, "CAMS response exceeded the 20 MB limit."
                    )
                archive_bytes = target.read_bytes()
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                self.source,
                f"CAMS retrieval failed ({type(error).__name__}).",
            ) from error

        extracted = self._extract_measurements(archive_bytes, city)
        if set(extracted) != set(self.variables):
            raise ProviderError(
                self.source, "CAMS response did not contain both requested PM variables."
            )

        batch = ProviderBatch(self.source, city.city_id, now)
        batch.raw_payloads.append(
            {
                "resource": "global-atmospheric-composition-forecast",
                "response_status": 200,
                "payload": {
                    "dataset": self.dataset,
                    "request": request,
                    "file_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                    "file_size": len(archive_bytes),
                    "forecast_cycle": cycle.isoformat(),
                    "forecast_valid_time": valid_time.isoformat(),
                },
            }
        )
        for metric, data in extracted.items():
            batch.observations.append(
                {
                    "city_id": city.city_id,
                    "source": self.source,
                    "provider_record_id": (
                        f"{city.city_id}:{metric}:{valid_time.isoformat()}"
                    ),
                    "station_provider_id": None,
                    "station_name": city.name,
                    "sensor_provider_id": None,
                    "metric": metric,
                    "value": data["value"],
                    "original_unit": data["unit"],
                    "unit": data["unit"],
                    "observed_at": valid_time,
                    "latitude": data["latitude"],
                    "longitude": data["longitude"],
                    "quality_status": "valid",
                    "metadata": {
                        "measurement_kind": "modelled_forecast",
                        "forecast_cycle": cycle.isoformat(),
                        "forecast_lead_hours": leadtime,
                        "grid_latitude": data["latitude"],
                        "grid_longitude": data["longitude"],
                    },
                }
            )
        return batch

    def _extract_measurements(
        self, archive_bytes: bytes, city: CityPoint
    ) -> dict[str, dict[str, float | str]]:
        import numpy as np
        from netCDF4 import Dataset

        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                members = [
                    item
                    for item in archive.infolist()
                    if item.filename.lower().endswith(".nc")
                ]
                if not members or sum(item.file_size for item in members) > self.max_download_bytes:
                    raise ProviderError(
                        self.source, "CAMS NetCDF archive is empty or too large."
                    )

                extracted: dict[str, dict[str, float | str]] = {}
                for member in members:
                    content = archive.read(member)
                    with Dataset("cams.nc", mode="r", memory=content) as dataset:
                        latitude = self._coordinate(dataset, "latitude")
                        longitude = self._coordinate(dataset, "longitude")
                        latitudes = np.asarray(latitude[:])
                        longitudes = np.asarray(longitude[:])
                        if latitudes.ndim != 1 or longitudes.ndim != 1:
                            raise ProviderError(
                                self.source,
                                "CAMS latitude/longitude coordinates must be one-dimensional.",
                            )
                        lat_index, lon_index = self._nearest_grid_indices(
                            latitudes, longitudes, city.latitude, city.longitude
                        )
                        lat_dim = latitude.dimensions[0]
                        lon_dim = longitude.dimensions[0]
                        grid_lat = float(latitudes[lat_index])
                        grid_lon = float(longitudes[lon_index])
                        for name, variable in dataset.variables.items():
                            metric = self._metric_for_variable(name, variable)
                            if metric is None or metric in extracted:
                                continue
                            selectors = []
                            for dimension in variable.dimensions:
                                if dimension == lat_dim:
                                    selectors.append(lat_index)
                                elif dimension == lon_dim:
                                    selectors.append(lon_index)
                                else:
                                    selectors.append(0)
                            sample = np.ma.asarray(variable[tuple(selectors)]).squeeze()
                            if np.ma.is_masked(sample):
                                continue
                            value = float(sample)
                            if not math.isfinite(value) or value < 0:
                                continue
                            unit = str(getattr(variable, "units", ""))
                            if _normalize_value(metric, value, unit, unit) is None:
                                raise ProviderError(
                                    self.source,
                                    f"CAMS returned an unsupported unit for {metric}.",
                                )
                            extracted[metric] = {
                                "value": value,
                                "unit": unit,
                                "latitude": grid_lat,
                                "longitude": grid_lon,
                            }
                return extracted
        except ProviderError:
            raise
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
            raise ProviderError(
                self.source, "CAMS returned an unreadable NetCDF archive."
            ) from error

    @staticmethod
    def _coordinate(dataset: Any, coordinate: str) -> Any:
        for name, variable in dataset.variables.items():
            standard_name = str(getattr(variable, "standard_name", "")).lower()
            if name.lower() in {coordinate, coordinate[:3]} or standard_name == coordinate:
                return variable
        raise ProviderError(
            CopernicusCamsAdapter.source,
            f"CAMS response omitted its {coordinate} coordinate.",
        )

    @staticmethod
    def _nearest_grid_indices(
        latitudes: Any,
        longitudes: Any,
        latitude: float,
        longitude: float,
    ) -> tuple[int, int]:
        import numpy as np

        if latitudes.ndim != 1 or longitudes.ndim != 1:
            raise ProviderError(
                CopernicusCamsAdapter.source,
                "CAMS latitude/longitude coordinates have an unsupported shape.",
            )
        distances = (
            np.abs(latitudes[:, None] - latitude)
            + np.abs(longitudes[None, :] - longitude)
        )
        return tuple(int(index) for index in np.unravel_index(np.argmin(distances), distances.shape))

    @staticmethod
    def _metric_for_variable(name: str, variable: Any) -> str | None:
        description = " ".join(
            (
                name,
                str(getattr(variable, "long_name", "")),
                str(getattr(variable, "standard_name", "")),
            )
        ).lower()
        if "pm2.5" in description or "particulate_matter_2.5um" in description:
            return "PM2.5"
        if "pm10" in description or "particulate_matter_10um" in description:
            return "PM10"
        return None


class NasaPowerAdapter:
    source = "nasa_power"
    parameters = {
        "T2M": ("Temperature", "°C"),
        "RH2M": ("Humidity", "%"),
        "PRECTOTCORR": ("Precipitation", "mm"),
        "WS10M": ("Wind speed", "m/s"),
        "WD10M": ("Wind direction", "°"),
        "ALLSKY_SFC_SW_DWN": ("Solar radiation", "kWh/m²/day"),
    }

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()

    def fetch(self, city: CityPoint, start: datetime, end: datetime) -> ProviderBatch:
        query = urlencode(
            {
                "parameters": ",".join(self.parameters),
                "community": "RE",
                "longitude": city.longitude,
                "latitude": city.latitude,
                "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "format": "JSON",
                "time-standard": "UTC",
            }
        )
        payload, response_status = self.client.get_json_with_status(
            f"https://power.larc.nasa.gov/api/temporal/hourly/point?{query}",
            source=self.source,
        )
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "hourly-point",
                "response_status": response_status,
                "payload": payload,
            }
        )
        parameters = (payload.get("properties") or {}).get("parameter") or {}
        for key, values in parameters.items():
            metric = self.parameters.get(key)
            if metric is None or not isinstance(values, dict):
                continue
            for timestamp, raw_value in values.items():
                value = _finite_number(raw_value)
                if value is None or value == -999:
                    continue
                normalized = _normalize_value(
                    metric[0], value, metric[1], metric[1]
                )
                if normalized is None:
                    continue
                observed_at = datetime.strptime(timestamp, "%Y%m%d%H").replace(
                    tzinfo=timezone.utc
                )
                batch.observations.append(
                    {
                        "city_id": city.city_id,
                        "source": self.source,
                        "provider_record_id": f"{city.city_id}:{key}:{timestamp}",
                        "station_provider_id": None,
                        "station_name": city.name,
                        "sensor_provider_id": key,
                        "metric": metric[0],
                        "value": normalized[0],
                        "original_unit": metric[1],
                        "unit": normalized[1],
                        "observed_at": observed_at,
                        "latitude": city.latitude,
                        "longitude": city.longitude,
                        "quality_status": "valid",
                        "metadata": {"provider_parameter": key},
                    }
                )
        return batch


class FirmsAdapter:
    source = "nasa_firms"

    def __init__(self, map_key: str | None, client: HttpClient | None = None):
        self.map_key = map_key
        self.client = client or HttpClient()

    def fetch(self, city: CityPoint) -> ProviderBatch:
        if not self.map_key:
            raise ProviderError(self.source, "NASA FIRMS MAP_KEY is not configured.")
        delta = 0.35
        bounds = (
            city.longitude - delta,
            city.latitude - delta,
            city.longitude + delta,
            city.latitude + delta,
        )
        area = ",".join(f"{coordinate:.4f}" for coordinate in bounds)
        url = (
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{self.map_key}/VIIRS_SNPP_NRT/{area}/1"
        )
        payload, _, response_status = self.client.request(
            url,
            source=self.source,
            headers={"Accept": "text/csv"},
        )
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "VIIRS_SNPP_NRT/area/1",
                "response_status": response_status,
                "payload": rows,
                "request_url": url.replace(self.map_key, "[REDACTED]"),
            }
        )
        for row in rows:
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                observed_at = datetime.strptime(
                    f"{row['acq_date']} {row['acq_time'].zfill(4)}",
                    "%Y-%m-%d %H%M",
                ).replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            batch.events.append(
                {
                    "event_type": "satellite_fire_detection",
                    "source": self.source,
                    "city_id": city.city_id,
                    "provider_record_id": (
                        f"{row.get('satellite', '')}:{row.get('instrument', '')}:"
                        f"{row.get('latitude')}:{row.get('longitude')}:"
                        f"{row.get('acq_date')}:{row.get('acq_time')}"
                    ),
                    "title": "Satellite fire detection",
                    "description": "Satellite thermal anomaly; not proof of a local ground fire.",
                    "observed_at": observed_at,
                    "latitude": latitude,
                    "longitude": longitude,
                    "confidence": row.get("confidence"),
                    "metadata": {
                        "frp": row.get("frp"),
                        "brightness": row.get("bright_ti4"),
                        "daynight": row.get("daynight"),
                        "satellite": row.get("satellite"),
                    },
                }
            )
        return batch


class GdeltAdapter:
    source = "gdelt"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient()

    def fetch(self, city: CityPoint) -> ProviderBatch:
        query = urlencode(
            {
                "query": f'("air pollution" OR environment OR wildfire) "{city.name}"',
                "mode": "ArtList",
                "format": "json",
                "maxrecords": 20,
                "sort": "DateDesc",
            }
        )
        payload, response_status = self.client.get_json_with_status(
            f"https://api.gdeltproject.org/api/v2/doc/doc?{query}", source=self.source
        )
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "doc-api",
                "response_status": response_status,
                "payload": payload,
            }
        )
        for article in payload.get("articles", []):
            if not isinstance(article, dict) or not article.get("url"):
                continue
            date_text = article.get("seendate")
            try:
                observed_at = datetime.strptime(
                    date_text, "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                observed_at = batch.fetched_at
            batch.events.append(
                {
                    "event_type": "environmental_news",
                    "source": self.source,
                    "city_id": city.city_id,
                    "provider_record_id": str(article.get("url")),
                    "title": str(article.get("title") or "Environmental news"),
                    "description": str(article.get("title") or ""),
                    "observed_at": observed_at,
                    "latitude": None,
                    "longitude": None,
                    "url": str(article["url"]),
                    "confidence": None,
                    "metadata": {
                        "domain": article.get("domain"),
                        "language": article.get("language"),
                        "source_country": article.get("sourceCountry"),
                    },
                }
            )
        return batch


class OverpassAdapter:
    source = "openstreetmap"

    def __init__(self, client: HttpClient | None = None):
        self.client = client or HttpClient(timeout=30, attempts=2)

    def fetch(self, city: CityPoint, radius_m: int = 10000) -> ProviderBatch:
        query = (
            "[out:json][timeout:25];("
            f"node(around:{radius_m},{city.latitude},{city.longitude})[landuse=industrial];"
            f"way(around:{radius_m},{city.latitude},{city.longitude})[landuse=industrial];"
            f"node(around:{radius_m},{city.latitude},{city.longitude})[man_made=works];"
            f"way(around:{radius_m},{city.latitude},{city.longitude})[man_made=works];"
            ");out center tags;"
        )
        body = urlencode({"data": query}).encode("utf-8")
        payload_bytes, _, response_status = self.client.request(
            "https://overpass-api.de/api/interpreter",
            source=self.source,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderError(self.source, "Overpass returned invalid JSON.") from error
        if not isinstance(payload, dict):
            raise ProviderError(self.source, "Overpass returned an unexpected response.")
        batch = ProviderBatch(self.source, city.city_id, datetime.now(timezone.utc))
        batch.raw_payloads.append(
            {
                "resource": "overpass",
                "response_status": response_status,
                "payload": payload,
            }
        )
        for feature in payload.get("elements", []):
            tags = feature.get("tags") or {}
            center = feature.get("center") or feature
            batch.events.append(
                {
                    "event_type": "geographic_feature",
                    "source": self.source,
                    "city_id": city.city_id,
                    "provider_record_id": f"{feature.get('type')}:{feature.get('id')}",
                    "title": str(tags.get("name") or tags.get("landuse") or "OSM feature"),
                    "description": "OpenStreetMap geographic context; not an emissions measurement.",
                    "observed_at": batch.fetched_at,
                    "latitude": center.get("lat"),
                    "longitude": center.get("lon"),
                    "url": f"https://www.openstreetmap.org/{feature.get('type')}/{feature.get('id')}",
                    "confidence": None,
                    "metadata": {"tags": tags, "osm_type": feature.get("type")},
                }
            )
        return batch


def adapter_catalog(
    *,
    openaq_api_key: str | None,
    firms_map_key: str | None,
    cams_api_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "openaq": {
            "configured": bool(openaq_api_key),
            "adapter": OpenAQAdapter(openaq_api_key),
            "attribution": "OpenAQ; station and sensor license metadata retained.",
        },
        "open_meteo": {
            "configured": True,
            "adapter": OpenMeteoAdapter(),
            "attribution": "Weather data by Open-Meteo; modelled current conditions.",
        },
        "open_meteo_air_quality": {
            "configured": True,
            "adapter": OpenMeteoAdapter(),
            "attribution": "Open-Meteo CAMS model output; not ground-station data.",
        },
        "nasa_power": {
            "configured": True,
            "adapter": NasaPowerAdapter(),
            "attribution": "NASA POWER hourly meteorological data.",
        },
        "nasa_firms": {
            "configured": bool(firms_map_key),
            "adapter": FirmsAdapter(firms_map_key),
            "attribution": "NASA FIRMS VIIRS satellite detection.",
        },
        "gdelt": {
            "configured": True,
            "adapter": GdeltAdapter(),
            "attribution": "GDELT news context; does not establish causality.",
        },
        "openstreetmap": {
            "configured": True,
            "adapter": OverpassAdapter(),
            "attribution": "© OpenStreetMap contributors; geographic context only.",
        },
        "copernicus_cams": {
            "configured": bool(cams_api_key and cams_api_key.strip()),
            "adapter": CopernicusCamsAdapter(cams_api_key),
            "attribution": (
                "CAMS global forecast (CC-BY-4.0); modeled data, 0.4-degree grid, "
                "nearest-cell extraction."
            ),
        },
    }
