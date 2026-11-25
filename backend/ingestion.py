from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import (
    Alert,
    ApiSource,
    Anomaly,
    City,
    EnvironmentalEvent,
    EnvironmentalSensor,
    IngestionRun,
    MonitoringStation,
    RawApiPayload,
    SourceObservation,
    SessionLocal,
)
from backend.integrations import (
    CopernicusCamsAdapter,
    FirmsAdapter,
    GdeltAdapter,
    NasaPowerAdapter,
    OpenAQAdapter,
    OpenMeteoAdapter,
    OverpassAdapter,
    ProviderBatch,
    ProviderError,
    CityPoint,
    adapter_catalog,
)
from backend.ml import detect_spike
from backend.events import publish_event
from air_quality import india_aqi_concentration_threshold


logger = logging.getLogger(__name__)

METRIC_RANGES = {
    "PM2.5": (0, 2000),
    "PM10": (0, 3000),
    "NO2": (0, 5000),
    "SO2": (0, 5000),
    "CO": (0, 100000),
    "O3": (0, 10000),
    "Temperature": (-90, 65),
    "Humidity": (0, 100),
    "Wind speed": (0, 250),
    "Wind direction": (0, 360),
    "Precipitation": (0, 1500),
    "Surface pressure": (300, 1200),
}
SOURCE_FRESHNESS_HOURS = {
    "openaq": 3,
    "open_meteo": 3,
    "open_meteo_air_quality": 6,
    "nasa_power": 48,
    "nasa_firms": 48,
    "gdelt": 48,
    "openstreetmap": 24 * 30,
    "copernicus_cams": 36,
}
SOURCE_POLL_INTERVALS = {
    "openaq": timedelta(minutes=45),
    "open_meteo": timedelta(minutes=45),
    "open_meteo_air_quality": timedelta(minutes=45),
    "nasa_power": timedelta(hours=24),
    "nasa_firms": timedelta(hours=48),
    "gdelt": timedelta(hours=48),
    "openstreetmap": timedelta(days=30),
    "copernicus_cams": timedelta(hours=12),
}


def _city_point(city: City) -> CityPoint:
    return CityPoint(city.id, city.name, city.latitude, city.longitude)


def _get_batch(
    source_id: str,
    city: City,
    historical_days: int | None = None,
    history_start: datetime | None = None,
    history_end: datetime | None = None,
) -> ProviderBatch:
    settings = get_settings()
    city_point = _city_point(city)
    if source_id == "openaq":
        adapter = OpenAQAdapter(settings.openaq_api_key)
        if history_start is not None or history_end is not None:
            if history_start is None or history_end is None or historical_days is not None:
                raise ValueError("Provide either historical_days or a complete history range.")
            return adapter.fetch_history(city_point, history_start, history_end)
        if historical_days is not None:
            now = datetime.now(timezone.utc)
            return adapter.fetch_history(
                city_point, now - timedelta(days=historical_days), now
            )
        return adapter.fetch(city_point)
    if source_id == "open_meteo":
        return OpenMeteoAdapter().fetch_weather(city_point)
    if source_id == "open_meteo_air_quality":
        return OpenMeteoAdapter().fetch_air_quality(city_point)
    if source_id == "copernicus_cams":
        return CopernicusCamsAdapter(settings.cams_api_key).fetch(city_point)
    if source_id == "nasa_power":
        now = datetime.now(timezone.utc)
        return NasaPowerAdapter().fetch(
            city_point, now - timedelta(days=2), now
        )
    if source_id == "nasa_firms":
        return FirmsAdapter(settings.firms_map_key).fetch(city_point)
    if source_id == "gdelt":
        return GdeltAdapter().fetch(city_point)
    if source_id == "openstreetmap":
        return OverpassAdapter().fetch(city_point)
    raise ProviderError(source_id, "Provider is not enabled or supported.")


def _raw_payload_row(
    source_id: str,
    city_id: str,
    run_id: int,
    raw: dict[str, Any],
    fetched_at: datetime,
) -> RawApiPayload:
    serializable = json.dumps(raw.get("payload", {}), ensure_ascii=False, separators=(",", ":"))
    content_hash = hashlib.sha256(serializable.encode("utf-8")).hexdigest()
    payload = json.loads(serializable)
    return RawApiPayload(
        source_id=source_id,
        city_id=city_id,
        ingestion_run_id=run_id,
        resource=str(raw.get("resource", "unknown"))[:180],
        request_url=raw.get("request_url"),
        response_status=raw.get("response_status"),
        fetched_at=fetched_at,
        content_hash=content_hash,
        payload=payload,
    )


def _store_observations(
    session: Session,
    batch: ProviderBatch,
    run: IngestionRun,
    payload_ids_by_sensor: dict[tuple[str, str], int],
    payload_ids_by_station: dict[str, int],
    fallback_payload_id: int | None = None,
    payload_ids_by_sensor_page: dict[tuple[str, str, int], int] | None = None,
) -> tuple[int, int]:
    accepted = 0
    rejected = 0
    for data in batch.observations:
        value = data.get("value")
        observed_at = data.get("observed_at")
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        source_id = data.get("source")
        provider_record_id = data.get("provider_record_id")
        metric = data.get("metric")
        unit = data.get("unit")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not isinstance(observed_at, datetime)
            or not isinstance(source_id, str)
            or source_id != run.source_id
            or batch.city_id != run.city_id
            or data.get("city_id") != batch.city_id
            or not isinstance(provider_record_id, str)
            or not provider_record_id.strip()
            or len(provider_record_id) > 240
            or not isinstance(metric, str)
            or not metric.strip()
            or len(metric) > 100
            or not isinstance(unit, str)
            or not unit.strip()
            or len(unit) > 40
        ):
            rejected += 1
            continue
        value = float(value)
        if observed_at.tzinfo is None:
            rejected += 1
            continue
        age_hours = (
            datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)
        ).total_seconds() / 3600
        if age_hours < -0.17:
            rejected += 1
            continue
        plausible_range = METRIC_RANGES.get(metric)
        if plausible_range and not plausible_range[0] <= value <= plausible_range[1]:
            rejected += 1
            continue
        coordinates_valid = (
            latitude is None
            and longitude is None
        ) or (
            isinstance(latitude, (int, float))
            and not isinstance(latitude, bool)
            and math.isfinite(float(latitude))
            and -90 <= latitude <= 90
            and isinstance(longitude, (int, float))
            and not isinstance(longitude, bool)
            and math.isfinite(float(longitude))
            and -180 <= longitude <= 180
        )
        if not coordinates_valid:
            rejected += 1
            continue
        station_id = None
        sensor_id = None
        station_provider_id = data.get("station_provider_id")
        if station_provider_id:
            station = session.scalar(
                select(MonitoringStation).where(
                    MonitoringStation.source_id == source_id,
                    MonitoringStation.provider_id == str(station_provider_id),
                )
            )
            if station is None:
                station = MonitoringStation(
                    city_id=batch.city_id,
                    source_id=source_id,
                    provider_id=str(station_provider_id),
                    name=str(data.get("station_name") or station_provider_id)[:300],
                    latitude=latitude,
                    longitude=longitude,
                    metadata_json=data.get("metadata") or {},
                )
                session.add(station)
                session.flush()
            else:
                station.name = str(data.get("station_name") or station.name)[:300]
                station.latitude = latitude
                station.longitude = longitude
                station.metadata_json = data.get("metadata") or {}
            station_id = station.id
            sensor_provider_id = data.get("sensor_provider_id")
            if sensor_provider_id:
                sensor = session.scalar(
                    select(EnvironmentalSensor).where(
                        EnvironmentalSensor.station_id == station_id,
                        EnvironmentalSensor.provider_id == str(sensor_provider_id),
                    )
                )
                if sensor is None:
                    sensor = EnvironmentalSensor(
                        station_id=station_id,
                        provider_id=str(sensor_provider_id),
                        parameter=metric,
                        original_unit=data.get("original_unit"),
                        canonical_unit=unit,
                    )
                    session.add(sensor)
                    session.flush()
                sensor_id = sensor.id

        sensor_key = (
            str(station_provider_id),
            str(data.get("sensor_provider_id")),
        )
        raw_page = data.get("raw_payload_page")
        page_payload_id = (
            payload_ids_by_sensor_page.get((*sensor_key, raw_page))
            if payload_ids_by_sensor_page is not None
            and isinstance(raw_page, int)
            else None
        )
        raw_payload_id = (
            page_payload_id
            or payload_ids_by_sensor.get(sensor_key)
            or payload_ids_by_station.get(str(station_provider_id), fallback_payload_id)
        )
        freshness_limit = SOURCE_FRESHNESS_HOURS.get(source_id)
        freshness_status = (
            "stale"
            if freshness_limit is not None and age_hours > freshness_limit
            else "current"
        )
        quality_checks = {
            "finite_value": True,
            "plausible_range": plausible_range is None
            or plausible_range[0] <= value <= plausible_range[1],
            "unit_normalized": True,
            "timestamp_timezone_aware": True,
            "coordinates_valid": latitude is not None and longitude is not None,
            "station_identity_present": station_id is not None,
            "raw_payload_retained": raw_payload_id is not None,
        }
        quality_weights = {
            "finite_value": 0.2,
            "plausible_range": 0.2,
            "unit_normalized": 0.2,
            "timestamp_timezone_aware": 0.15,
            "coordinates_valid": 0.1,
            "station_identity_present": 0.075,
            "raw_payload_retained": 0.075,
        }
        quality_score = sum(
            weight for name, weight in quality_weights.items() if quality_checks[name]
        )
        metadata = dict(data.get("metadata") or {})
        metadata["quality_checks"] = quality_checks
        metadata["freshness_status"] = freshness_status
        statement_data = {
            "source_id": source_id,
            "city_id": batch.city_id,
            "station_id": station_id,
            "sensor_id": sensor_id,
            "ingestion_run_id": run.id,
            "raw_payload_id": raw_payload_id,
            "provider_record_id": provider_record_id,
            "metric": metric,
            "value": value,
            "original_unit": str(data.get("original_unit") or unit)[:40],
            "canonical_unit": unit,
            "observed_at": observed_at.astimezone(timezone.utc),
            "latitude": latitude,
            "longitude": longitude,
            "quality_status": str(data.get("quality_status") or "valid"),
            "quality_score": quality_score,
            "metadata_json": metadata,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = pg_insert(SourceObservation).values(**statement_data)
            statement = statement.on_conflict_do_nothing(
                constraint="uq_source_observation"
            ).returning(SourceObservation.id)
        elif dialect == "sqlite":
            statement = sqlite_insert(SourceObservation).values(**statement_data)
            statement = statement.on_conflict_do_nothing(
                index_elements=["source_id", "provider_record_id", "metric", "observed_at"]
            ).returning(SourceObservation.id)
        else:
            exists = session.scalar(
                select(SourceObservation.id).where(
                    SourceObservation.source_id == source_id,
                    SourceObservation.provider_record_id
                    == statement_data["provider_record_id"],
                    SourceObservation.metric == statement_data["metric"],
                    SourceObservation.observed_at == statement_data["observed_at"],
                )
            )
            if exists:
                continue
            session.add(SourceObservation(**statement_data))
            accepted += 1
            continue
        result = session.execute(statement)
        observation_id = result.scalar_one_or_none()
        if observation_id is not None:
            accepted += 1
        if observation_id is not None and station_id is not None:
            _evaluate_station_reading(
                session,
                observation_id,
                station_id,
                batch.city_id,
                statement_data["metric"],
                float(value),
                statement_data["canonical_unit"],
                statement_data["observed_at"],
            )
    return accepted, rejected


def _evaluate_station_reading(
    session: Session,
    observation_id: int | None,
    station_id: int,
    city_id: str,
    metric: str,
    value: float,
    unit: str,
    observed_at: datetime,
) -> None:
    if observation_id is None or metric not in {"PM2.5", "PM10"}:
        return
    window = session.scalars(
        select(SourceObservation)
        .where(
            SourceObservation.station_id == station_id,
            SourceObservation.metric == metric,
            SourceObservation.quality_status == "valid",
            SourceObservation.observed_at >= observed_at - timedelta(hours=24),
            SourceObservation.observed_at <= observed_at,
        )
        .order_by(SourceObservation.observed_at)
    ).all()
    existing = session.scalar(
        select(Anomaly.id).where(Anomaly.observation_id == observation_id)
    )
    if len(window) >= 7 and existing is None:
        check = detect_spike([row.value for row in window[:-1]], value)
        if check["detected"]:
            anomaly = Anomaly(
                city_id=city_id,
                observation_id=observation_id,
                metric=metric,
                severity="high" if value > check["threshold"] * 2 else "moderate",
                score=(value - check["median"]) / max(check["mad"], 1.0),
                detected_at=observed_at,
                description=f"{metric} exceeded its recent station baseline.",
                status="open",
                metadata_json=check,
            )
            session.add(anomaly)
            session.flush()
            dedupe_key = (
                f"spike:{station_id}:{metric}:{observed_at.strftime('%Y%m%d')}"
            )
            if not _alert_exists(session, dedupe_key):
                session.add(
                    Alert(
                        dedupe_key=dedupe_key,
                        city_id=city_id,
                        anomaly_id=anomaly.id,
                        severity=anomaly.severity,
                        title=f"{metric} spike detected",
                        description=anomaly.description,
                        created_at=observed_at,
                        status="active",
                    )
                )

    min_count = 16
    if (
        len(window) < min_count
        or window[-1].observed_at - window[0].observed_at < timedelta(hours=18)
    ):
        return
    settings = get_settings()
    threshold = india_aqi_concentration_threshold(
        metric, settings.aqi_alert_threshold
    )
    if threshold is None or unit != "µg/m³":
        return
    mean_value = sum(row.value for row in window) / len(window)
    if mean_value <= threshold:
        return
    dedupe_key = f"aqi100:{station_id}:{metric}:{observed_at.strftime('%Y%m%d')}"
    if _alert_exists(session, dedupe_key):
        return
    session.add(
        Alert(
            dedupe_key=dedupe_key,
            city_id=city_id,
            severity="high",
            title=(
                f"Estimated AQI sub-index above {settings.aqi_alert_threshold} "
                f"for {metric}"
            ),
            description=(
                f"The station's 24-hour {metric} mean is {mean_value:.1f} {unit}; "
                f"the AQI {settings.aqi_alert_threshold} breakpoint threshold is "
                f"{threshold:g} µg/m³."
            ),
            created_at=observed_at,
            status="active",
        )
    )


def _alert_exists(session: Session, dedupe_key: str) -> bool:
    if any(
        isinstance(item, Alert) and item.dedupe_key == dedupe_key
        for item in session.new
    ):
        return True
    return session.scalar(select(Alert.id).where(Alert.dedupe_key == dedupe_key)) is not None


def _store_events(
    session: Session,
    batch: ProviderBatch,
    source_id: str,
) -> int:
    created = 0
    for event in batch.events:
        provider_record_id = event.get("provider_record_id")
        observed_at = event.get("observed_at")
        latitude = event.get("latitude")
        longitude = event.get("longitude")
        coordinates_valid = (
            latitude is None
            and longitude is None
        ) or (
            isinstance(latitude, (int, float))
            and not isinstance(latitude, bool)
            and math.isfinite(float(latitude))
            and -90 <= latitude <= 90
            and isinstance(longitude, (int, float))
            and not isinstance(longitude, bool)
            and math.isfinite(float(longitude))
            and -180 <= longitude <= 180
        )
        if (
            not isinstance(provider_record_id, str)
            or not provider_record_id.strip()
            or len(provider_record_id) > 500
            or not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or not coordinates_valid
            or event.get("url") is not None
            and not isinstance(event["url"], str)
        ):
            continue
        values = {
            "source_id": source_id,
            "city_id": batch.city_id,
            "provider_record_id": provider_record_id,
            "event_type": str(event.get("event_type") or "environmental_event")[:100],
            "title": str(event.get("title") or "Environmental event")[:500],
            "description": str(event.get("description") or "")[:10000],
            "url": event.get("url"),
            "observed_at": observed_at.astimezone(timezone.utc),
            "latitude": latitude,
            "longitude": longitude,
            "confidence": str(event["confidence"])[:40]
            if event.get("confidence") is not None
            else None,
            "metadata_json": event.get("metadata") or {},
        }
        exists = session.scalar(
            select(EnvironmentalEvent.id).where(
                EnvironmentalEvent.source_id == source_id,
                EnvironmentalEvent.provider_record_id == values["provider_record_id"],
            )
        )
        if exists:
            continue
        session.add(EnvironmentalEvent(**values))
        created += 1
    return created


def ingest_source(
    source_id: str,
    city_id: str,
    historical_days: int | None = None,
    *,
    history_start: datetime | None = None,
    history_end: datetime | None = None,
) -> dict[str, Any]:
    if (history_start is None) != (history_end is None):
        raise ValueError("Both history_start and history_end are required.")
    if historical_days is not None and (
        source_id != "openaq" or not 1 <= historical_days <= 30
    ):
        raise ValueError("Direct historical-days ingestion is limited to 1-30 OpenAQ days.")
    if history_start is not None and (
        source_id != "openaq" or historical_days is not None
    ):
        raise ValueError("Explicit historical ranges are supported only for OpenAQ.")
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as session:
        city = session.get(City, city_id)
        source = session.get(ApiSource, source_id)
        if city is None:
            raise ValueError(f"Unknown city: {city_id}")
        if source is None or not source.active:
            raise ValueError(f"Unknown or inactive source: {source_id}")
        run = IngestionRun(
            source_id=source_id,
            city_id=city_id,
            status="running",
            started_at=started_at,
        )
        source.last_attempt_at = started_at
        source.last_error = None
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

        try:
            catalog = adapter_catalog(
                openaq_api_key=get_settings().openaq_api_key,
                firms_map_key=get_settings().firms_map_key,
                cams_api_key=get_settings().cams_api_key,
            )
            if not catalog.get(source_id, {}).get("configured", False):
                source.configured = False
                raise ProviderError(source_id, "Provider credentials are not configured.")
            source.configured = True
            batch = _get_batch(
                source_id,
                city,
                historical_days,
                history_start,
                history_end,
            )
            if batch.source != source_id or batch.city_id != city_id:
                raise ProviderError(
                    source_id, "Provider returned data for a different source or city."
                )
            raw_rows = [
                _raw_payload_row(
                    source_id, city_id, run_id, raw, batch.fetched_at
                )
                for raw in batch.raw_payloads
            ]
            session.add_all(raw_rows)
            session.flush()
            payload_ids_by_station = {
                str(raw.get("location_id")): row.id
                for raw, row in zip(batch.raw_payloads, raw_rows)
                if raw.get("location_id") is not None and raw.get("sensor_id") is None
            }
            payload_ids_by_sensor = {
                (str(raw["location_id"]), str(raw["sensor_id"])): row.id
                for raw, row in zip(batch.raw_payloads, raw_rows)
                if raw.get("location_id") is not None
                and raw.get("sensor_id") is not None
                and not isinstance(raw.get("page"), int)
            }
            payload_ids_by_sensor_page = {
                (
                    str(raw["location_id"]),
                    str(raw["sensor_id"]),
                    int(raw["page"]),
                ): row.id
                for raw, row in zip(batch.raw_payloads, raw_rows)
                if raw.get("location_id") is not None
                and raw.get("sensor_id") is not None
                and isinstance(raw.get("page"), int)
            }
            fallback_payload_id = raw_rows[-1].id if raw_rows else None
            accepted, rejected = _store_observations(
                session,
                batch,
                run,
                payload_ids_by_sensor,
                payload_ids_by_station,
                fallback_payload_id,
                payload_ids_by_sensor_page,
            )
            events_saved = _store_events(session, batch, source_id)
            finished_at = datetime.now(timezone.utc)
            run.status = "succeeded"
            run.finished_at = finished_at
            run.records_seen = len(batch.observations) + len(batch.events)
            run.records_accepted = accepted + events_saved
            run.records_rejected = rejected
            run.metadata_json = {
                "raw_payloads": len(raw_rows),
                "events_saved": events_saved,
                "provider_metadata": batch.metadata,
                "historical_range": (
                    {
                        "start": history_start.isoformat(),
                        "end": history_end.isoformat(),
                    }
                    if history_start is not None and history_end is not None
                    else None
                ),
            }
            source.last_success_at = finished_at
            source.last_error = None
            session.commit()
            if accepted:
                publish_event(
                    "observation_ingested",
                    {
                        "run_id": run_id,
                        "source_id": source_id,
                        "city_id": city_id,
                        "accepted": accepted,
                        "finished_at": finished_at.isoformat(),
                    },
                )
            if events_saved:
                publish_event(
                    "environmental_event",
                    {
                        "run_id": run_id,
                        "source_id": source_id,
                        "city_id": city_id,
                        "accepted": events_saved,
                        "finished_at": finished_at.isoformat(),
                    },
                )
            publish_event(
                "ingestion_completed",
                {
                    "run_id": run_id,
                    "source_id": source_id,
                    "city_id": city_id,
                    "status": "succeeded",
                    "accepted": accepted + events_saved,
                    "rejected": rejected,
                    "finished_at": finished_at.isoformat(),
                },
            )
            return {
                "run_id": run_id,
                "source": source_id,
                "city_id": city_id,
                "status": "succeeded",
                "accepted": accepted + events_saved,
                "rejected": rejected,
                "started_at": started_at,
                "finished_at": finished_at,
                "provider_metadata": batch.metadata,
                "history_start": (
                    history_start.isoformat() if history_start is not None else None
                ),
                "history_end": history_end.isoformat() if history_end is not None else None,
            }
        except ProviderError as error:
            session.rollback()
            run = session.get(IngestionRun, run_id)
            source = session.get(ApiSource, source_id)
            finished_at = datetime.now(timezone.utc)
            run.status = "failed"
            run.finished_at = finished_at
            run.error_message = str(error)[:2000]
            source.last_error = str(error)[:2000]
            session.commit()
            logger.warning(
                "Ingestion failed: source=%s city=%s status=%s",
                source_id,
                city_id,
                error.status_code,
            )
            raise
        except Exception as error:
            session.rollback()
            run = session.get(IngestionRun, run_id)
            source = session.get(ApiSource, source_id)
            finished_at = datetime.now(timezone.utc)
            run.status = "failed"
            run.finished_at = finished_at
            run.error_message = "Ingestion failed due to an internal processing error."
            source.last_error = "Ingestion failed due to an internal processing error."
            session.commit()
            logger.exception(
                "Ingestion processing failed: source=%s city=%s", source_id, city_id
            )
            raise RuntimeError("Ingestion processing failed.") from error


def _source_poll_due(source_id: str, city_id: str) -> bool:
    interval = SOURCE_POLL_INTERVALS.get(source_id)
    if interval is None:
        return True
    with SessionLocal() as session:
        last_attempt = session.scalar(
            select(IngestionRun.started_at)
            .where(
                IngestionRun.source_id == source_id,
                IngestionRun.city_id == city_id,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
    if last_attempt is None:
        return True
    if last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_attempt >= interval


def ingest_city(city_id: str) -> list[dict[str, Any]]:
    settings = get_settings()
    catalog = adapter_catalog(
        openaq_api_key=settings.openaq_api_key,
        firms_map_key=settings.firms_map_key,
        cams_api_key=settings.cams_api_key,
    )
    results = []
    for source_id, details in catalog.items():
        if not details["configured"]:
            continue
        if not _source_poll_due(source_id, city_id):
            continue
        try:
            results.append(ingest_source(source_id, city_id))
        except ProviderError as error:
            results.append(
                {
                    "source": source_id,
                    "city_id": city_id,
                    "status": "failed",
                    "error": str(error),
                }
            )
    return results

