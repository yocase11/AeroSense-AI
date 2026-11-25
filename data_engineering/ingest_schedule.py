from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from backend.database import City, IngestionRun, SessionLocal
from backend.ingestion import SOURCE_POLL_INTERVALS, ingest_city


logger = logging.getLogger(__name__)
POLL_INTERVAL = timedelta(minutes=45)
LIVE_SOURCES = ("open_meteo", "open_meteo_air_quality")


def ingest_active_cities_once() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        city_ids = session.scalars(
            select(City.id).where(City.active.is_(True)).order_by(City.id)
        ).all()

    results: list[dict[str, Any]] = []
    for city_id in city_ids:
        try:
            city_results = ingest_city(city_id)
        except Exception as error:
            logger.exception("Scheduled ingestion failed for city=%s", city_id)
            results.append(
                {
                    "city_id": city_id,
                    "status": "failed",
                    "error": str(error),
                }
            )
            continue

        results.extend(city_results)
        for result in city_results:
            if result.get("status") == "failed":
                logger.warning(
                    "Scheduled provider ingestion failed: city=%s source=%s error=%s",
                    city_id,
                    result.get("source"),
                    result.get("error"),
                )
            else:
                logger.info(
                    "Scheduled provider ingestion completed: city=%s source=%s",
                    city_id,
                    result.get("source"),
                )
    return results


def seconds_until_next_poll(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as session:
        city_ids = session.scalars(
            select(City.id).where(City.active.is_(True)).order_by(City.id)
        ).all()
        if not city_ids:
            return POLL_INTERVAL.total_seconds()

        attempts = session.execute(
            select(
                IngestionRun.city_id,
                IngestionRun.source_id,
                func.max(IngestionRun.started_at),
            )
            .where(
                IngestionRun.city_id.in_(city_ids),
                IngestionRun.source_id.in_(LIVE_SOURCES),
            )
            .group_by(IngestionRun.city_id, IngestionRun.source_id)
        ).all()

    last_attempts = {
        (city_id, source_id): started_at
        for city_id, source_id, started_at in attempts
    }
    next_attempts = []
    for city_id in city_ids:
        for source_id in LIVE_SOURCES:
            started_at = last_attempts.get((city_id, source_id))
            if started_at is None:
                return 0
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            next_attempts.append(
                started_at + SOURCE_POLL_INTERVALS[source_id]
            )

    return max((min(next_attempts) - now).total_seconds(), 0)


def run_forever() -> None:
    logger.info("Starting scheduled ingestion every %s minutes", POLL_INTERVAL.total_seconds() / 60)
    while True:
        try:
            results = ingest_active_cities_once()
            failed = sum(result.get("status") == "failed" for result in results)
            logger.info(
                "Scheduled ingestion cycle completed: results=%s failures=%s",
                len(results),
                failed,
            )
            wait_seconds = seconds_until_next_poll()
        except Exception:
            logger.exception("Scheduled ingestion cycle failed")
            wait_seconds = POLL_INTERVAL.total_seconds()

        logger.info("Next scheduled ingestion in %.1f minutes", wait_seconds / 60)
        time.sleep(max(wait_seconds, 1))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()
