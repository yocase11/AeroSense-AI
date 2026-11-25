from __future__ import annotations

import json
import math
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CITIES: dict[str, dict[str, float]] = {
    "Chennai": {"latitude": 13.0827, "longitude": 80.2707},
    "Delhi": {"latitude": 28.6139, "longitude": 77.2090},
    "Mumbai": {"latitude": 19.0760, "longitude": 72.8777},
    "Bengaluru": {"latitude": 12.9716, "longitude": 77.5946},
}
DB_PATH = Path(__file__).parent / "data" / "air_quality.sqlite3"
RETENTION_DAYS = 7
MAX_ROWS_PER_SERIES = 1500
AQI_THRESHOLD = 100
INDIA_AQI_BREAKPOINTS = {
    "PM2.5": (
        (0, 30, 0, 50),
        (30, 60, 50, 100),
        (60, 90, 100, 200),
        (90, 120, 200, 300),
        (120, 250, 300, 400),
        (250, 500, 400, 500),
    ),
    "PM10": (
        (0, 50, 0, 50),
        (50, 100, 50, 100),
        (100, 250, 100, 200),
        (250, 350, 200, 300),
        (350, 430, 300, 400),
        (430, 600, 400, 500),
    ),
}


class DataSourceError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _get_json(url: str, api_key: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "AirQualityMonitor/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 401:
            message = "OpenAQ rejected the API key. Check that it is valid."
        elif error.code == 429:
            message = "The API rate limit was reached. Try again in a few minutes."
        else:
            message = f"The API returned HTTP {error.code}."
        raise DataSourceError(message) from error
    except URLError as error:
        raise DataSourceError(f"Could not reach the API: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise DataSourceError("The API returned an invalid JSON response.") from error


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    value = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(value))


def _pollutant_name(name: str) -> str | None:
    normalized = name.lower().replace(".", "").replace("_", "").replace("-", "")
    if normalized in {"pm25", "pm2.5"}:
        return "PM2.5"
    if normalized == "pm10":
        return "PM10"
    return None


def fetch_openaq(city: str, api_key: str) -> list[dict[str, Any]]:
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}")
    if not api_key.strip():
        raise DataSourceError("Add an OpenAQ API key to load air quality readings.")

    coordinates = CITIES[city]
    query = urlencode(
        {
            "coordinates": (
                f"{coordinates['latitude']},{coordinates['longitude']}"
            ),
            "radius": 25000,
            "limit": 100,
        }
    )
    payload = _get_json(f"https://api.openaq.org/v3/locations?{query}", api_key)
    locations = []
    for location in payload.get("results", []):
        sensors = location.get("sensors", [])
        if not any(
            _pollutant_name(sensor.get("parameter", {}).get("name", ""))
            for sensor in sensors
        ):
            continue
        point = location.get("coordinates") or {}
        lat, lon = point.get("latitude"), point.get("longitude")
        if lat is None or lon is None:
            continue
        distance = _distance_km(
            coordinates["latitude"], coordinates["longitude"], lat, lon
        )
        if distance <= 25:
            locations.append((distance, location))

    locations.sort(key=lambda item: item[0])
    latest_by_pollutant: dict[str, dict[str, Any]] = {}
    for _, location in locations[:5]:
        sensor_names = {
            sensor.get("id"): _pollutant_name(
                sensor.get("parameter", {}).get("name", "")
            )
            for sensor in location.get("sensors", [])
        }
        location_payload = _get_json(
            f"https://api.openaq.org/v3/locations/{location['id']}/latest",
            api_key,
        )
        for reading in location_payload.get("results", []):
            pollutant = sensor_names.get(reading.get("sensorsId"))
            value = reading.get("value")
            observed_at = (reading.get("datetime") or {}).get("utc")
            if pollutant is None or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value) or not observed_at:
                continue
            existing = latest_by_pollutant.get(pollutant)
            if existing is None or observed_at > existing["observed_at"]:
                latest_by_pollutant[pollutant] = {
                    "city": city,
                    "source": "OpenAQ",
                    "metric": pollutant,
                    "value": float(value),
                    "unit": "µg/m³",
                    "observed_at": observed_at,
                    "location": location.get("name", city),
                }

    if not latest_by_pollutant:
        raise DataSourceError(f"No recent PM2.5 or PM10 stations found near {city}.")
    return list(latest_by_pollutant.values())


def fetch_open_meteo(city: str) -> list[dict[str, Any]]:
    if city not in CITIES:
        raise ValueError(f"Unknown city: {city}")
    coordinates = CITIES[city]
    query = urlencode(
        {
            "latitude": coordinates["latitude"],
            "longitude": coordinates["longitude"],
            "current": (
                "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            ),
            "timezone": "UTC",
        }
    )
    payload = _get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    current = payload.get("current")
    if not isinstance(current, dict) or not current.get("time"):
        raise DataSourceError(f"Open-Meteo returned no current weather for {city}.")
    observed_at = current["time"]
    if not observed_at.endswith("Z"):
        observed_at += "Z"
    units = payload.get("current_units", {})
    metric_names = {
        "temperature_2m": "Temperature",
        "relative_humidity_2m": "Humidity",
        "wind_speed_10m": "Wind speed",
        "weather_code": "Weather code",
    }
    return [
        {
            "city": city,
            "source": "Open-Meteo",
            "metric": metric_names[key],
            "value": float(current[key]),
            "unit": units.get(key, ""),
            "observed_at": observed_at,
            "location": city,
        }
        for key in metric_names
        if key in current and isinstance(current[key], (int, float))
    ]


def connect_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY,
            city TEXT NOT NULL,
            source TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            location TEXT NOT NULL,
            UNIQUE(city, source, metric, observed_at)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS measurements_city_time
        ON measurements(city, observed_at)
        """
    )
    return connection


def store_measurements(
    measurements: list[dict[str, Any]], db_path: Path = DB_PATH
) -> None:
    with closing(connect_db(db_path)) as connection:
        with connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO measurements
                    (city, source, metric, value, unit, observed_at, location)
                VALUES
                    (:city, :source, :metric, :value, :unit, :observed_at, :location)
                """,
                measurements,
            )
            cutoff = _utc_string(_utc_now() - timedelta(days=RETENTION_DAYS))
            connection.execute(
                "DELETE FROM measurements WHERE observed_at < ?", (cutoff,)
            )
            series = connection.execute(
                "SELECT DISTINCT city, source, metric FROM measurements"
            ).fetchall()
            for row in series:
                connection.execute(
                    """
                    DELETE FROM measurements
                    WHERE id IN (
                        SELECT id FROM measurements
                        WHERE city = ? AND source = ? AND metric = ?
                        ORDER BY observed_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (row["city"], row["source"], row["metric"], MAX_ROWS_PER_SERIES),
                )
        connection.execute("PRAGMA incremental_vacuum(64)")


def read_measurements(
    hours: int = 24, db_path: Path = DB_PATH
) -> list[dict[str, Any]]:
    cutoff = _utc_string(_utc_now() - timedelta(hours=hours))
    with closing(connect_db(db_path)) as connection:
        rows = connection.execute(
            """
            SELECT city, source, metric, value, unit, observed_at, location
            FROM measurements
            WHERE observed_at >= ?
            ORDER BY observed_at
            """,
            (cutoff,),
        ).fetchall()
    return [dict(row) for row in rows]


def rolling_average(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _sub_index(
    concentration: float,
    breakpoints: tuple[tuple[float, float, float, float], ...],
) -> int:
    for low, high, index_low, index_high in breakpoints:
        if concentration <= high:
            value = (index_high - index_low) / (high - low) * (
                concentration - low
            ) + index_low
            return round(max(0, value))
    return 500


def indian_aqi(pm_values: dict[str, float | None]) -> int | None:
    sub_indices = [
        _sub_index(value, INDIA_AQI_BREAKPOINTS[metric])
        for metric, value in pm_values.items()
        if metric in INDIA_AQI_BREAKPOINTS and value is not None and value >= 0
    ]
    return max(sub_indices) if sub_indices else None


def india_aqi_concentration_threshold(metric: str, aqi: int) -> float | None:
    breakpoints = INDIA_AQI_BREAKPOINTS.get(metric)
    if breakpoints is None or aqi < 0:
        return None
    for low, high, index_low, index_high in breakpoints:
        if aqi <= index_high:
            return low + (aqi - index_low) * (high - low) / (index_high - index_low)
    return None


def aqi_category(aqi: int | None) -> str:
    if aqi is None:
        return "No data"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Satisfactory"
    if aqi <= 200:
        return "Moderate"
    if aqi <= 300:
        return "Poor"
    if aqi <= 400:
        return "Very poor"
    return "Severe"


def is_anomaly(previous_values: list[float], latest_value: float) -> bool:
    if len(previous_values) < 6:
        return False
    median = statistics.median(previous_values)
    median_deviation = statistics.median(
        abs(value - median) for value in previous_values
    )
    threshold = median + max(10.0, 3 * 1.4826 * median_deviation)
    return latest_value > threshold
