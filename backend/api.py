from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from air_quality import aqi_category, indian_aqi
from backend.agents import AGENT_REGISTRY, run_investigation
from backend.auth import get_current_user, require_csrf, require_roles
from backend.config import get_settings
from backend.database import (
    Alert,
    AgentTask,
    Anomaly,
    AuthSession,
    ApiSource,
    City,
    Document,
    EnvironmentalEvent,
    Forecast,
    IngestionRun,
    Investigation,
    MonitoringStation,
    ModelEvaluation,
    Report,
    SessionLocal,
    SourceObservation,
    UserAccount,
    get_session,
)
from backend.ml import forecast_metric
from backend.rag import MAX_DOCUMENT_BYTES, ingest_document, search_documents
from backend.schemas import (
    ChatRequest,
    DocumentSearchRequest,
    InvestigationRequest,
)


router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(get_current_user), Depends(require_csrf)],
)
logger = logging.getLogger(__name__)


def _auth_session_active(session_id: str) -> bool:
    with SessionLocal() as session:
        auth_session = session.get(AuthSession, session_id)
        if auth_session is None or auth_session.revoked_at is not None:
            return False
        expires_at = auth_session.access_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > datetime.now(timezone.utc)


def _require_owner(owner_user_id: int | None, user: UserAccount) -> None:
    if user.role != "admin" and owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="Resource not found.")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _observation(row: SourceObservation) -> dict[str, Any]:
    return {
        "id": row.id,
        "city_id": row.city_id,
        "source_id": row.source_id,
        "station_id": row.station_id,
        "sensor_id": row.sensor_id,
        "metric": row.metric,
        "value": row.value,
        "unit": row.canonical_unit,
        "original_unit": row.original_unit,
        "quality_status": row.quality_status,
        "observed_at": _iso(row.observed_at),
        "ingested_at": _iso(row.ingested_at),
        "latitude": row.latitude,
        "longitude": row.longitude,
        "metadata": row.metadata_json,
    }


def _city_rows(session: Session, city_id: str | None):
    statement = select(City).where(City.active.is_(True)).order_by(City.name)
    if city_id:
        statement = statement.where(City.id == city_id)
    cities = session.scalars(statement).all()
    if city_id and not cities:
        raise HTTPException(status_code=404, detail="City not found.")
    return cities


def _aqi_summary(
    session: Session, city_id: str, now: datetime
) -> dict[str, Any]:
    cutoff = now - timedelta(hours=24)
    rows = session.scalars(
        select(SourceObservation)
        .where(
            SourceObservation.city_id == city_id,
            SourceObservation.source_id == "openaq",
            SourceObservation.metric.in_(["PM2.5", "PM10"]),
            SourceObservation.quality_status == "valid",
            SourceObservation.observed_at >= cutoff,
            SourceObservation.observed_at <= now,
        )
        .order_by(SourceObservation.observed_at)
    ).all()
    by_metric: dict[str, list[SourceObservation]] = {}
    for row in rows:
        by_metric.setdefault(row.metric, []).append(row)

    means: dict[str, float | None] = {"PM2.5": None, "PM10": None}
    valid_metrics = []
    sample_count = 0
    station_ids: set[int] = set()
    for metric, readings in by_metric.items():
        if len(readings) < 16:
            continue
        first_observed = readings[0].observed_at
        last_observed = readings[-1].observed_at
        if first_observed.tzinfo is None:
            first_observed = first_observed.replace(tzinfo=timezone.utc)
        if last_observed.tzinfo is None:
            last_observed = last_observed.replace(tzinfo=timezone.utc)
        if last_observed - first_observed < timedelta(hours=18):
            continue

        station_values: dict[int | None, list[float]] = {}
        for reading in readings:
            station_values.setdefault(reading.station_id, []).append(reading.value)
            if reading.station_id is not None:
                station_ids.add(reading.station_id)
        station_means = [
            sum(values) / len(values) for values in station_values.values()
        ]
        means[metric] = sum(station_means) / len(station_means)
        valid_metrics.append(metric)
        sample_count += len(readings)

    if valid_metrics:
        value = indian_aqi(means)
        return {
            "value": value,
            "category": aqi_category(value),
            "validity_status": "citywide_24h_ground_estimate",
            "basis": "citywide_ground_24h",
            "source_id": "openaq",
            "station_id": None,
            "station_count": len(station_ids),
            "pollutants_used": valid_metrics,
            "pm_24h_means": means,
            "sample_count": sample_count,
            "minimum_samples_per_pollutant": 16,
        }

    model_cutoff = now - timedelta(hours=3)
    model_rows = session.scalars(
        select(SourceObservation)
        .where(
            SourceObservation.city_id == city_id,
            SourceObservation.source_id == "open_meteo_air_quality",
            SourceObservation.metric.in_(["PM2.5", "PM10"]),
            SourceObservation.quality_status == "valid",
            SourceObservation.canonical_unit == "µg/m³",
            SourceObservation.observed_at >= model_cutoff,
            SourceObservation.observed_at <= now,
        )
        .order_by(SourceObservation.observed_at.desc())
    ).all()
    current_by_metric: dict[str, SourceObservation] = {}
    for row in model_rows:
        current_by_metric.setdefault(row.metric, row)
    concentrations = {
        metric: current_by_metric[metric].value if metric in current_by_metric else None
        for metric in ("PM2.5", "PM10")
    }
    valid_metrics = [metric for metric, value in concentrations.items() if value is not None]
    if valid_metrics:
        value = indian_aqi(concentrations)
        return {
            "value": value,
            "category": aqi_category(value),
            "validity_status": "modeled_city_estimate_current",
            "basis": "modeled_current",
            "source_id": "open_meteo_air_quality",
            "station_id": None,
            "station_count": 0,
            "pollutants_used": valid_metrics,
            "pm_concentrations": concentrations,
            "observed_at": max(
                row.observed_at for row in current_by_metric.values()
            ).isoformat(),
            "sample_count": len(valid_metrics),
            "minimum_samples_per_pollutant": 16,
        }

    return {
        "value": None,
        "category": "No data",
        "validity_status": "no_recent_city_pm_data",
        "basis": "unavailable",
        "source_id": None,
        "station_id": None,
        "station_count": 0,
        "sample_count": len(rows),
        "minimum_samples_per_pollutant": 16,
    }


@router.get("/cities")
def list_cities(session: Session = Depends(get_session)):
    cities = session.scalars(
        select(City).where(City.active.is_(True)).order_by(City.name)
    ).all()
    return [
        {
            "id": city.id,
            "name": city.name,
            "country_code": city.country_code,
            "latitude": city.latitude,
            "longitude": city.longitude,
        }
        for city in cities
    ]


@router.get("/cities/{city_id}")
def get_city(city_id: str, session: Session = Depends(get_session)):
    city = session.get(City, city_id)
    if city is None:
        raise HTTPException(status_code=404, detail="City not found.")
    return {
        "id": city.id,
        "name": city.name,
        "country_code": city.country_code,
        "latitude": city.latitude,
        "longitude": city.longitude,
    }


@router.get("/dashboard/overview")
def dashboard_overview(
    city_id: str | None = None,
    session: Session = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    cities = _city_rows(session, city_id)
    overview = []
    for city in cities:
        rows = session.scalars(
            select(SourceObservation)
            .where(
                SourceObservation.city_id == city.id,
                SourceObservation.quality_status == "valid",
                SourceObservation.value != -999,
            )
            .order_by(SourceObservation.observed_at.desc())
            .limit(500)
        ).all()
        latest: dict[tuple[str, int | None, str], SourceObservation] = {}
        for row in rows:
            latest.setdefault((row.source_id, row.station_id, row.metric), row)
        sources = session.scalars(
            select(ApiSource).order_by(ApiSource.display_name)
        ).all()
        relevant = [
            source for source in sources
            if source.last_attempt_at is not None
        ]
        anomalies = session.scalars(
            select(Anomaly)
            .where(Anomaly.city_id == city.id, Anomaly.status == "open")
            .order_by(Anomaly.detected_at.desc())
            .limit(10)
        ).all()
        alerts = session.scalars(
            select(Alert)
            .where(Alert.city_id == city.id, Alert.status == "active")
            .order_by(Alert.created_at.desc())
            .limit(10)
        ).all()
        overview.append(
            {
                "city": {
                    "id": city.id,
                    "name": city.name,
                    "latitude": city.latitude,
                    "longitude": city.longitude,
                },
                "measurements": [_observation(row) for row in latest.values()],
                "estimated_india_aqi": _aqi_summary(session, city.id, now),
                "anomalies": [
                    {
                        "id": item.id,
                        "metric": item.metric,
                        "severity": item.severity,
                        "description": item.description,
                        "detected_at": _iso(item.detected_at),
                    }
                    for item in anomalies
                ],
                "alerts": [
                    {
                        "id": item.id,
                        "severity": item.severity,
                        "title": item.title,
                        "created_at": _iso(item.created_at),
                    }
                    for item in alerts
                ],
                "source_health": [
                    {
                        "id": source.id,
                        "name": source.display_name,
                        "configured": source.configured,
                        "last_success_at": _iso(source.last_success_at),
                        "last_attempt_at": _iso(source.last_attempt_at),
                        "last_error": source.last_error,
                    }
                    for source in relevant
                ],
            }
        )
    return {"generated_at": now.isoformat(), "cities": overview}


@router.get("/dashboard/live-events")
async def dashboard_live_events(request: Request):
    session_id = request.state.auth_session_id
    models = {
        "observation": SourceObservation,
        "event": EnvironmentalEvent,
        "anomaly": Anomaly,
        "alert": Alert,
    }

    def read_cursors():
        with SessionLocal() as session:
            return {
                name: session.scalar(select(func.max(model.id))) or 0
                for name, model in models.items()
            }

    def read_updates(after_ids: dict[str, int]):
        with SessionLocal() as session:
            observations = session.scalars(
                select(SourceObservation)
                .where(SourceObservation.id > after_ids["observation"])
                .order_by(SourceObservation.id)
                .limit(100)
            ).all()
            events = session.scalars(
                select(EnvironmentalEvent)
                .where(EnvironmentalEvent.id > after_ids["event"])
                .order_by(EnvironmentalEvent.id)
                .limit(100)
            ).all()
            anomalies = session.scalars(
                select(Anomaly)
                .where(Anomaly.id > after_ids["anomaly"])
                .order_by(Anomaly.id)
                .limit(100)
            ).all()
            alerts = session.scalars(
                select(Alert)
                .where(Alert.id > after_ids["alert"])
                .order_by(Alert.id)
                .limit(100)
            ).all()
            return (
                [
                    ("observation", row.id, row.ingested_at, _observation(row))
                    for row in observations
                ]
                + [
                    (
                        "event",
                        row.id,
                        row.observed_at,
                        {
                            "id": row.id,
                            "city_id": row.city_id,
                            "title": row.title,
                            "type": row.event_type,
                            "observed_at": _iso(row.observed_at),
                        },
                    )
                    for row in events
                ]
                + [
                    (
                        "anomaly",
                        row.id,
                        row.detected_at,
                        {
                            "id": row.id,
                            "city_id": row.city_id,
                            "metric": row.metric,
                            "severity": row.severity,
                            "detected_at": _iso(row.detected_at),
                        },
                    )
                    for row in anomalies
                ]
                + [
                    (
                        "alert",
                        row.id,
                        row.created_at,
                        {
                            "id": row.id,
                            "city_id": row.city_id,
                            "title": row.title,
                            "severity": row.severity,
                            "created_at": _iso(row.created_at),
                        },
                    )
                    for row in alerts
                ]
            )

    async def stream():
        settings = get_settings()
        if settings.redis_url:
            redis_client = None
            pubsub = None
            try:
                from redis.asyncio import Redis

                redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
                pubsub = redis_client.pubsub()
                await pubsub.subscribe("aerosense:events")
                while not await request.is_disconnected():
                    if not await asyncio.to_thread(_auth_session_active, session_id):
                        break
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=5
                    )
                    if message and message.get("data"):
                        payload = json.loads(message["data"])
                        yield (
                            f"event: {payload.get('type', 'update')}\ndata: "
                            f"{json.dumps(payload.get('payload', {}), ensure_ascii=False)}\n\n"
                        )
                    else:
                        yield ": heartbeat\n\n"
                return
            except Exception:
                logger.exception("Redis SSE subscription failed; using database polling.")
            finally:
                if pubsub is not None:
                    await pubsub.aclose()
                if redis_client is not None:
                    await redis_client.aclose()

        last_ids = await asyncio.to_thread(read_cursors)
        while True:
            if (
                await request.is_disconnected()
                or not await asyncio.to_thread(_auth_session_active, session_id)
            ):
                break
            updates = await asyncio.to_thread(read_updates, last_ids)
            updates.sort(key=lambda item: item[2])
            for event_name, event_id, observed_at, data in updates:
                last_ids[event_name] = max(last_ids[event_name], event_id)
                yield (
                    f"id: {event_name}:{event_id}\nevent: {event_name}\ndata: "
                    f"{json.dumps(data, ensure_ascii=False)}\n\n"
                )
            yield ": heartbeat\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/air-quality/current")
def current_air_quality(
    city_id: str | None = None,
    session: Session = Depends(get_session),
):
    cities = _city_rows(session, city_id)
    result = []
    for city in cities:
        rows = session.scalars(
            select(SourceObservation)
            .where(
                SourceObservation.city_id == city.id,
                SourceObservation.metric.in_(["PM2.5", "PM10", "NO2", "SO2", "CO", "O3"]),
            )
            .order_by(SourceObservation.observed_at.desc())
            .limit(500)
        ).all()
        latest = {}
        for row in rows:
            latest.setdefault((row.source_id, row.station_id, row.metric), row)
        result.extend(_observation(row) for row in latest.values())
    return result


@router.get("/air-quality/history")
def air_quality_history(
    city_id: str,
    metric: str = Query(default="PM2.5", max_length=80),
    hours: int = Query(default=24, ge=1, le=24 * 90),
    station_id: int | None = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    statement = select(SourceObservation).where(
            SourceObservation.city_id == city_id,
            SourceObservation.metric == metric,
            SourceObservation.observed_at >= cutoff,
        )
    if station_id is not None:
        statement = statement.where(SourceObservation.station_id == station_id)
    rows = session.scalars(
        statement
        .order_by(SourceObservation.observed_at)
        .offset(offset)
        .limit(limit)
    ).all()
    return [_observation(row) for row in rows]


@router.get("/air-quality/stations")
def air_quality_stations(
    city_id: str | None = None,
    source_id: str | None = None,
    session: Session = Depends(get_session),
):
    statement = select(MonitoringStation).order_by(MonitoringStation.name)
    if city_id:
        statement = statement.where(MonitoringStation.city_id == city_id)
    if source_id:
        statement = statement.where(MonitoringStation.source_id == source_id)
    stations = session.scalars(statement).all()
    return [
        {
            "id": row.id,
            "city_id": row.city_id,
            "source_id": row.source_id,
            "provider_id": row.provider_id,
            "name": row.name,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "metadata": row.metadata_json,
        }
        for row in stations
    ]


@router.get("/weather/current")
def current_weather(city_id: str | None = None, session: Session = Depends(get_session)):
    cities = _city_rows(session, city_id)
    output = []
    for city in cities:
        rows = session.scalars(
            select(SourceObservation)
            .where(
                SourceObservation.city_id == city.id,
                SourceObservation.metric.in_(
                    ["Temperature", "Humidity", "Wind speed", "Wind direction", "Precipitation", "Surface pressure"]
                ),
            )
            .order_by(SourceObservation.observed_at.desc())
            .limit(100)
        ).all()
        latest = {}
        for row in rows:
            latest.setdefault((row.source_id, row.station_id, row.metric), row)
        output.extend(_observation(row) for row in latest.values())
    return output


@router.get("/weather/history")
def weather_history(
    city_id: str,
    metric: str = Query(default="Temperature", max_length=80),
    hours: int = Query(default=24, ge=1, le=24 * 90),
    limit: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.scalars(
        select(SourceObservation)
        .where(
            SourceObservation.city_id == city_id,
            SourceObservation.metric == metric,
            SourceObservation.observed_at >= cutoff,
        )
        .order_by(SourceObservation.observed_at)
        .limit(limit)
    ).all()
    return [_observation(row) for row in rows]


@router.get("/forecasts")
def list_forecasts(
    city_id: str | None = None,
    metric: str | None = None,
    station_id: int | None = None,
    session: Session = Depends(get_session),
):
    filters = []
    if city_id:
        filters.append(Forecast.city_id == city_id)
    if metric:
        filters.append(Forecast.metric == metric)
    if station_id is not None:
        filters.append(Forecast.station_id == station_id)
    latest_generation = (
        select(func.max(Forecast.generated_at)).where(*filters).scalar_subquery()
    )
    statement = (
        select(Forecast)
        .where(*filters, Forecast.generated_at == latest_generation)
        .order_by(Forecast.target_at)
    )
    rows = session.scalars(statement.limit(1000)).all()
    return [
        {
            "id": row.id,
            "city_id": row.city_id,
            "station_id": row.station_id,
            "metric": row.metric,
            "model": row.model_name,
            "model_version": row.model_version,
            "generated_at": _iso(row.generated_at),
            "target_at": _iso(row.target_at),
            "value": row.value,
            "lower_bound": row.lower_bound,
            "upper_bound": row.upper_bound,
            "metadata": row.metadata_json,
            "horizon_hours": (row.metadata_json or {}).get("horizon_hours"),
            "horizon_evaluation": (row.metadata_json or {}).get(
                "horizon_evaluation"
            ),
            "feature_importance": (row.metadata_json or {}).get(
                "feature_importance"
            ),
        }
        for row in rows
    ]


@router.post("/forecasts/generate")
def generate_forecasts(
    city_id: str,
    metric: str = Query(default="PM2.5", max_length=80),
    station_id: int | None = None,
    session: Session = Depends(get_session),
    _: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    if session.get(City, city_id) is None:
        raise HTTPException(status_code=404, detail="City not found.")
    if station_id is not None:
        station = session.get(MonitoringStation, station_id)
        if station is None or station.city_id != city_id:
            raise HTTPException(status_code=404, detail="Station not found for this city.")
        if station.source_id != "openaq":
            raise HTTPException(
                status_code=422,
                detail="Forecasting requires an OpenAQ ground monitoring station.",
            )
    try:
        result = forecast_metric(session, city_id, metric, station_id)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise


@router.get("/models/performance")
def model_performance(
    city_id: str | None = None,
    station_id: int | None = None,
    session: Session = Depends(get_session),
    _: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    statement = select(ModelEvaluation).order_by(ModelEvaluation.evaluated_at.desc())
    if city_id:
        statement = statement.where(ModelEvaluation.city_id == city_id)
    if station_id is not None:
        statement = statement.where(ModelEvaluation.station_id == station_id)
    rows = session.scalars(statement.limit(100)).all()
    return [
        {
            "city_id": row.city_id,
            "station_id": row.station_id,
            "metric": row.metric,
            "model": row.model_name,
            "version": row.model_version,
            "horizon_hours": row.horizon_hours,
            "mae": row.mae,
            "rmse": row.rmse,
            "r_squared": row.r_squared,
            "evaluated_at": _iso(row.evaluated_at),
            "evaluation_type": row.evaluation_period.get(
                "evaluation_type", "legacy"
            ),
            "sample_count": row.evaluation_period.get("sample_count"),
            "mae_improvement_percent": row.evaluation_period.get(
                "mae_improvement_percent"
            ),
            "persistence_baseline": row.evaluation_period.get(
                "persistence_baseline"
            ),
        }
        for row in rows
    ]


@router.get("/sources")
def list_sources(session: Session = Depends(get_session)):
    rows = session.scalars(select(ApiSource).order_by(ApiSource.display_name)).all()
    return [
        {
            "id": row.id,
            "name": row.display_name,
            "attribution": row.attribution,
            "configured": row.configured,
            "active": row.active,
            "last_success_at": _iso(row.last_success_at),
            "last_attempt_at": _iso(row.last_attempt_at),
            "last_error": row.last_error,
        }
        for row in rows
    ]


@router.get("/sources/health")
def source_health(session: Session = Depends(get_session)):
    return list_sources(session)


@router.get("/sources/ingestion-history")
def ingestion_history(
    source_id: str | None = None,
    city_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    statement = select(IngestionRun).order_by(IngestionRun.started_at.desc())
    if source_id:
        statement = statement.where(IngestionRun.source_id == source_id)
    if city_id:
        statement = statement.where(IngestionRun.city_id == city_id)
    rows = session.scalars(statement.offset(offset).limit(limit)).all()
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "city_id": row.city_id,
            "status": row.status,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "records_seen": row.records_seen,
            "records_accepted": row.records_accepted,
            "records_rejected": row.records_rejected,
            "error": row.error_message,
        }
        for row in rows
    ]


@router.get("/agents")
def list_agents():
    return AGENT_REGISTRY


@router.get("/agents/status")
def agent_status(
    user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    statement = select(Investigation).order_by(Investigation.created_at.desc())
    if user.role != "admin":
        statement = statement.where(Investigation.owner_user_id == user.id)
    rows = session.scalars(statement.limit(1)).all()
    tasks = []
    if rows:
        tasks = session.execute(
            text(
                "SELECT agent_name, status, started_at, completed_at "
                "FROM agent_tasks WHERE investigation_id = :id ORDER BY id"
            ),
            {"id": rows[0].id},
        ).mappings().all()
    return {
        "last_investigation_id": rows[0].id if rows else None,
        "tasks": [dict(task) for task in tasks],
    }


@router.get("/agents/tasks")
def agent_tasks(
    investigation_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    statement = (
        select(AgentTask)
        .join(Investigation, AgentTask.investigation_id == Investigation.id)
        .order_by(AgentTask.id.desc())
    )
    if current_user.role != "admin":
        statement = statement.where(Investigation.owner_user_id == current_user.id)
    if investigation_id is not None:
        statement = statement.where(AgentTask.investigation_id == investigation_id)
    rows = session.scalars(statement.limit(limit)).all()
    return [
        {
            "id": row.id,
            "investigation_id": row.investigation_id,
            "agent": row.agent_name,
            "status": row.status,
            "started_at": _iso(row.started_at),
            "completed_at": _iso(row.completed_at),
            "result": row.result,
            "error": row.error_message,
        }
        for row in rows
    ]


def _execute_investigation(investigation_id: int, question: str, city_id: str | None):
    from backend.database import SessionLocal, AgentTask

    try:
        with SessionLocal.begin() as session:
            investigation = session.get(Investigation, investigation_id)
            if investigation is None:
                return
            investigation.status = "running"
            investigation.completed_at = None
            supervisor_task = session.scalar(
                select(AgentTask)
                .where(
                    AgentTask.investigation_id == investigation_id,
                    AgentTask.agent_name == "environmental_supervisor",
                )
                .order_by(AgentTask.id.desc())
            )
            if supervisor_task is None:
                supervisor_task = AgentTask(
                    investigation_id=investigation_id,
                    agent_name="environmental_supervisor",
                )
                session.add(supervisor_task)
            supervisor_task.status = "running"
            supervisor_task.started_at = datetime.now(timezone.utc)
            supervisor_task.completed_at = None
            supervisor_task.result = {"question": question}
            supervisor_task.error_message = None
        run_investigation(investigation_id, question, city_id)
    except Exception as error:
        with SessionLocal.begin() as session:
            investigation = session.get(Investigation, investigation_id)
            if investigation is not None:
                investigation.status = "failed"
                investigation.completed_at = datetime.now(timezone.utc)
                investigation.answer = "Investigation failed; see server logs."
            task = session.scalar(
                select(AgentTask)
                .where(
                    AgentTask.investigation_id == investigation_id,
                    AgentTask.agent_name == "environmental_supervisor",
                )
                .order_by(AgentTask.id.desc())
            )
            if task is not None:
                task.status = "failed"
                task.completed_at = datetime.now(timezone.utc)
                task.error_message = str(error)[:1000]
        raise


@router.post("/agents/investigate", status_code=202)
def create_investigation(
    request: InvestigationRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    if request.city_id and session.get(City, request.city_id) is None:
        raise HTTPException(status_code=404, detail="City not found.")
    investigation = Investigation(
        owner_user_id=current_user.id,
        city_id=request.city_id,
        question=request.question,
        status="queued",
        created_at=datetime.now(timezone.utc),
        evidence=[],
    )
    session.add(investigation)
    session.commit()
    session.refresh(investigation)
    for agent in AGENT_REGISTRY:
        session.add(
            AgentTask(
                investigation_id=investigation.id,
                agent_name=agent["name"],
                status="queued",
                result={},
            )
        )
    session.commit()
    background_tasks.add_task(
        _execute_investigation,
        investigation.id,
        investigation.question,
        investigation.city_id,
    )
    return {
        "investigation_id": investigation.id,
        "status": investigation.status,
        "agents": [agent["name"] for agent in AGENT_REGISTRY],
    }


@router.post("/assistant/chat", status_code=202)
def assistant_chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    return create_investigation(
        InvestigationRequest(question=request.question, city_id=request.city_id),
        background_tasks,
        session,
        current_user,
    )


@router.get("/agents/investigations/{investigation_id}")
def get_investigation(
    investigation_id: int,
    current_user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    investigation = session.get(Investigation, investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found.")
    _require_owner(investigation.owner_user_id, current_user)
    tasks = session.scalars(
        select(AgentTask)
        .where(AgentTask.investigation_id == investigation_id)
        .order_by(AgentTask.id)
    ).all()
    return {
        "investigation_id": investigation.id,
        "city_id": investigation.city_id,
        "question": investigation.question,
        "status": investigation.status,
        "answer": investigation.answer,
        "evidence": investigation.evidence,
        "created_at": _iso(investigation.created_at),
        "completed_at": _iso(investigation.completed_at),
        "tasks": [
            {
                "agent": task.agent_name,
                "status": task.status,
                "started_at": _iso(task.started_at),
                "completed_at": _iso(task.completed_at),
                "result": task.result,
            }
            for task in tasks
        ],
    }


@router.get("/agents/investigations/{investigation_id}/events")
async def investigation_events(
    investigation_id: int,
    request: Request,
    current_user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    investigation = session.get(Investigation, investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found.")
    _require_owner(investigation.owner_user_id, current_user)

    async def stream():
        last_status = None
        while True:
            if not await asyncio.to_thread(
                _auth_session_active, request.state.auth_session_id
            ):
                yield "event: auth_expired\ndata: {}\n\n"
                break
            with SessionLocal() as current:
                investigation = current.get(Investigation, investigation_id)
                if investigation is None:
                    yield "event: error\ndata: {\"detail\":\"Investigation not found\"}\n\n"
                    break
                status = {
                    "status": investigation.status,
                    "answer": investigation.answer,
                    "completed_at": _iso(investigation.completed_at),
                }
                if status != last_status:
                    yield f"event: progress\ndata: {json.dumps(status)}\n\n"
                    last_status = status
                if investigation.status in {"completed", "failed"}:
                    break
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/anomalies")
def list_anomalies(
    city_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    statement = select(Anomaly).order_by(Anomaly.detected_at.desc())
    if city_id:
        statement = statement.where(Anomaly.city_id == city_id)
    rows = session.scalars(statement.limit(limit)).all()
    return [
        {
            "id": row.id,
            "city_id": row.city_id,
            "metric": row.metric,
            "severity": row.severity,
            "score": row.score,
            "description": row.description,
            "status": row.status,
            "detected_at": _iso(row.detected_at),
            "metadata": row.metadata_json,
        }
        for row in rows
    ]


@router.get("/events")
def list_events(
    city_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    statement = select(EnvironmentalEvent).order_by(EnvironmentalEvent.observed_at.desc())
    if city_id:
        statement = statement.where(EnvironmentalEvent.city_id == city_id)
    if event_type:
        statement = statement.where(EnvironmentalEvent.event_type == event_type)
    rows = session.scalars(statement.offset(offset).limit(limit)).all()
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "city_id": row.city_id,
            "event_type": row.event_type,
            "title": row.title,
            "description": row.description,
            "url": row.url,
            "observed_at": _iso(row.observed_at),
            "latitude": row.latitude,
            "longitude": row.longitude,
            "confidence": row.confidence,
        }
        for row in rows
    ]


@router.get("/events/{event_id}/evidence")
def event_evidence(event_id: int, session: Session = Depends(get_session)):
    event = session.get(EnvironmentalEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    related = []
    if event.latitude is not None and event.longitude is not None:
        rows = session.scalars(
            select(SourceObservation)
            .where(
                SourceObservation.city_id == event.city_id,
                SourceObservation.observed_at >= event.observed_at - timedelta(hours=24),
                SourceObservation.observed_at <= event.observed_at + timedelta(hours=24),
            )
            .limit(500)
        ).all()
        for row in rows:
            if row.latitude is None or row.longitude is None:
                continue
            from math import asin, cos, radians, sin, sqrt

            d_lat = radians(row.latitude - event.latitude)
            d_lon = radians(row.longitude - event.longitude)
            a = sin(d_lat / 2) ** 2 + cos(radians(event.latitude)) * cos(
                radians(row.latitude)
            ) * sin(d_lon / 2) ** 2
            distance_km = 6371 * 2 * asin(sqrt(a))
            if distance_km <= 50:
                related.append(
                    {
                        "observation_id": row.id,
                        "source_id": row.source_id,
                        "metric": row.metric,
                        "value": row.value,
                        "unit": row.canonical_unit,
                        "observed_at": _iso(row.observed_at),
                        "distance_km": round(distance_km, 2),
                    }
                )
    return {
        "event_id": event.id,
        "event": {
            "source_id": event.source_id,
            "title": event.title,
            "url": event.url,
            "observed_at": _iso(event.observed_at),
        },
        "nearby_observations": related,
        "interpretation": "Temporal or geographic proximity is not proof of causality.",
    }


@router.get("/knowledge/documents")
def list_documents(
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    rows = session.scalars(
        select(Document).order_by(Document.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "title": row.title,
            "source_url": row.source_url,
            "created_at": _iso(row.created_at),
            "metadata": row.metadata_json,
        }
        for row in rows
    ]


@router.post("/knowledge/documents", status_code=201)
async def upload_document(
    title: str = Form(..., min_length=1, max_length=500),
    source_url: str | None = Form(default=None, max_length=2000),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    if not get_settings().allow_document_uploads:
        raise HTTPException(
            status_code=403,
            detail="Document uploads are disabled; enable ALLOW_DOCUMENT_UPLOADS after configuring access controls.",
        )
    if file.content_type not in {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/octet-stream",
    }:
        raise HTTPException(status_code=415, detail="Unsupported document content type.")
    content = await file.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds the 5 MB limit.")
    try:
        result = ingest_document(
            session,
            title=title,
            filename=file.filename or "document.txt",
            content=content,
            source_url=source_url,
        )
        session.commit()
        return result
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        session.rollback()
        raise


@router.post("/knowledge/search")
def knowledge_search(
    request: DocumentSearchRequest,
    session: Session = Depends(get_session),
    _: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    try:
        return search_documents(session, request.query, request.limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: int,
    session: Session = Depends(get_session),
    _: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if alert.status == "active":
        alert.status = "acknowledged"
        alert.acknowledged_at = datetime.now(timezone.utc)
        session.commit()
    return {
        "id": alert.id,
        "status": alert.status,
        "acknowledged_at": _iso(alert.acknowledged_at),
    }


@router.get("/alerts")
def list_alerts(
    city_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    statement = select(Alert).order_by(Alert.created_at.desc())
    if city_id:
        statement = statement.where(Alert.city_id == city_id)
    if status:
        statement = statement.where(Alert.status == status)
    rows = session.scalars(statement.limit(limit)).all()
    return [
        {
            "id": row.id,
            "city_id": row.city_id,
            "severity": row.severity,
            "title": row.title,
            "description": row.description,
            "created_at": _iso(row.created_at),
            "acknowledged_at": _iso(row.acknowledged_at),
            "status": row.status,
        }
        for row in rows
    ]


@router.get("/reports")
def list_reports(
    current_user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    statement = select(Report).order_by(Report.created_at.desc())
    if current_user.role != "admin":
        statement = statement.where(Report.owner_user_id == current_user.id)
    rows = session.scalars(statement.limit(100)).all()
    return [
        {
            "id": row.id,
            "city_id": row.city_id,
            "type": row.report_type,
            "title": row.title,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


@router.post("/reports/generate", status_code=201)
def generate_report(
    city_id: str | None = None,
    report_type: str = Query(default="daily", pattern="^(daily|weekly)$"),
    session: Session = Depends(get_session),
    current_user: UserAccount = Depends(require_roles("viewer", "analyst", "admin")),
):
    start = datetime.now(timezone.utc) - timedelta(
        days=1 if report_type == "daily" else 7
    )
    observation_query = select(func.count(SourceObservation.id)).where(
        SourceObservation.observed_at >= start
    )
    event_query = select(func.count(EnvironmentalEvent.id)).where(
        EnvironmentalEvent.observed_at >= start
    )
    anomaly_query = select(func.count(Anomaly.id)).where(Anomaly.detected_at >= start)
    if city_id:
        if session.get(City, city_id) is None:
            raise HTTPException(status_code=404, detail="City not found.")
        observation_query = observation_query.where(SourceObservation.city_id == city_id)
        event_query = event_query.where(EnvironmentalEvent.city_id == city_id)
        anomaly_query = anomaly_query.where(Anomaly.city_id == city_id)
    content = {
        "period_start": start.isoformat(),
        "period_end": datetime.now(timezone.utc).isoformat(),
        "observations": session.scalar(observation_query) or 0,
        "events": session.scalar(event_query) or 0,
        "anomalies": session.scalar(anomaly_query) or 0,
        "status": "computed_from_persisted_records",
    }
    report = Report(
        owner_user_id=current_user.id,
        city_id=city_id,
        report_type=report_type,
        title=f"{report_type.title()} environmental report",
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return {
        "id": report.id,
        "type": report.report_type,
        "title": report.title,
        "created_at": _iso(report.created_at),
        "content": report.content,
    }


@router.get("/reports/{report_id}/pdf")
def download_report_pdf(
    report_id: int,
    current_user: UserAccount = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found.")
    _require_owner(report.owner_user_id, current_user)
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError as error:
        raise HTTPException(
            status_code=503, detail="PDF export dependency is not installed."
        ) from error
    import io

    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    pdf.setTitle(report.title)
    pdf.drawString(72, 740, report.title)
    y = 710
    for key, value in report.content.items():
        pdf.drawString(72, y, f"{key}: {value}")
        y -= 22
    pdf.save()
    return Response(
        output.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="aerosense-report-{report.id}.pdf"'
        },
    )
