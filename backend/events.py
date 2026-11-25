from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

from backend.config import get_settings


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _redis_client():
    url = get_settings().redis_url
    if not url:
        return None
    from redis import Redis

    return Redis.from_url(url, decode_responses=True, socket_timeout=2)


def publish_event(event_type: str, payload: dict[str, Any]) -> None:
    event = json.dumps(
        {"type": event_type, "payload": payload}, ensure_ascii=False, default=str
    )
    client = _redis_client()
    if client is not None:
        try:
            client.publish("aerosense:events", event)
        except Exception:
            logger.exception("Redis event publish failed: type=%s", event_type)

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    if bootstrap_servers:
        try:
            from kafka import KafkaProducer

            producer = KafkaProducer(
                bootstrap_servers=bootstrap_servers.split(","),
                value_serializer=lambda value: json.dumps(
                    value, ensure_ascii=False, default=str
                ).encode("utf-8"),
                request_timeout_ms=3000,
                max_block_ms=3000,
            )
            source_id = payload.get("source_id")
            topic = {
                "anomaly_detected": "environmental-anomalies",
                "investigation_completed": "agent-investigations",
                "alert_created": "environmental-alerts",
            }.get(event_type, "environmental-events")
            if event_type == "observation_ingested":
                topic = (
                    "weather-observations"
                    if source_id in {"open_meteo", "nasa_power"}
                    else "air-quality-observations"
                )
            elif event_type == "environmental_event":
                topic = (
                    "satellite-events"
                    if source_id == "nasa_firms"
                    else "environmental-events"
                )
            producer.send(topic, {"type": event_type, "payload": payload})
            producer.flush(timeout=3)
            producer.close(timeout=3)
        except Exception:
            logger.exception("Kafka event publish failed: type=%s", event_type)
