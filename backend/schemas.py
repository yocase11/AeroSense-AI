from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    city_id: str = Field(min_length=1, max_length=80)
    metric: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    observed_at: datetime
    station_provider_id: str | None = None
    station_name: str | None = None
    sensor_provider_id: str | None = None
    original_unit: str | None = None
    provider_record_id: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    quality_status: Literal["valid", "suspect", "rejected"] = "valid"
    raw_payload_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    run_id: int
    source: str
    city_id: str
    status: str
    accepted: int
    rejected: int
    started_at: datetime
    finished_at: datetime


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    city_id: str | None = None


class InvestigationResponse(BaseModel):
    investigation_id: int
    status: str
    agents: list[str]
    answer: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    city_id: str | None = None


class DocumentSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
