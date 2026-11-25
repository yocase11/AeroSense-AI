from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy import update

from backend.database import (
    EnvironmentalEvent,
    RawApiPayload,
    SourceObservation,
    SessionLocal,
)


def prune_history() -> dict[str, int]:
    observation_days = max(1, int(os.environ.get("OBSERVATION_RETENTION_DAYS", "365")))
    raw_days = max(1, int(os.environ.get("RAW_PAYLOAD_RETENTION_DAYS", "30")))
    event_days = max(1, int(os.environ.get("EVENT_RETENTION_DAYS", "180")))
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        deleted_observations = session.execute(
            delete(SourceObservation).where(
                SourceObservation.observed_at < now - timedelta(days=observation_days)
            )
        ).rowcount or 0
        deleted_events = session.execute(
            delete(EnvironmentalEvent).where(
                EnvironmentalEvent.observed_at < now - timedelta(days=event_days)
            )
        ).rowcount or 0
        expired_raw_ids = select(RawApiPayload.id).where(
            RawApiPayload.fetched_at < now - timedelta(days=raw_days)
        )
        session.execute(
            update(SourceObservation)
            .where(SourceObservation.raw_payload_id.in_(expired_raw_ids))
            .values(raw_payload_id=None)
        )
        expired_raw = session.execute(
            delete(RawApiPayload).where(RawApiPayload.id.in_(expired_raw_ids))
        ).rowcount or 0
        return {
            "observations": deleted_observations,
            "events": deleted_events,
            "raw_payloads": expired_raw,
        }


if __name__ == "__main__":
    print(prune_history())
