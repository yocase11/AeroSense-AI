from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    cors_origins: tuple[str, ...]
    auth_secret_key: str | None
    auth_cookie_secure: bool
    auth_cookie_samesite: str
    openaq_api_key: str | None
    firms_map_key: str | None
    cams_api_key: str | None
    gemini_api_key: str | None
    gemini_provider: str
    google_cloud_project: str | None
    google_cloud_location: str
    redis_url: str | None
    allow_document_uploads: bool
    aqi_alert_threshold: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    origins = tuple(
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:3000"
        ).split(",")
        if origin.strip()
    )
    threshold = int(os.environ.get("AQI_ALERT_THRESHOLD", "100"))
    if not 0 <= threshold <= 500:
        raise ValueError("AQI_ALERT_THRESHOLD must be between 0 and 500.")
    auth_secret_key = os.environ.get("AUTH_SECRET_KEY") or None
    if auth_secret_key is not None and len(auth_secret_key) < 32:
        raise ValueError("AUTH_SECRET_KEY must contain at least 32 characters.")
    auth_cookie_samesite = os.environ.get(
        "AUTH_COOKIE_SAMESITE", "lax"
    ).strip().lower()
    if auth_cookie_samesite not in {"lax", "strict", "none"}:
        raise ValueError("AUTH_COOKIE_SAMESITE must be lax, strict, or none.")
    auth_cookie_secure_value = os.environ.get("AUTH_COOKIE_SECURE", "true").lower()
    if auth_cookie_secure_value not in {"true", "false"}:
        raise ValueError("AUTH_COOKIE_SECURE must be true or false.")
    auth_cookie_secure = auth_cookie_secure_value == "true"
    if auth_cookie_samesite == "none" and not auth_cookie_secure:
        raise ValueError("AUTH_COOKIE_SECURE must be true when SameSite=None.")
    gemini_provider = os.environ.get("GEMINI_PROVIDER", "api_key").strip().lower()
    if gemini_provider not in {"api_key", "vertex_ai"}:
        raise ValueError("GEMINI_PROVIDER must be api_key or vertex_ai.")
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://localhost:5432/aerosense",
        ),
        cors_origins=origins,
        auth_secret_key=auth_secret_key,
        auth_cookie_secure=auth_cookie_secure,
        auth_cookie_samesite=auth_cookie_samesite,
        openaq_api_key=os.environ.get("OPENAQ_API_KEY") or None,
        firms_map_key=os.environ.get("NASA_FIRMS_MAP_KEY") or None,
        cams_api_key=os.environ.get("CAMS_API_KEY") or None,
        gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
        gemini_provider=gemini_provider,
        google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
        google_cloud_location=os.environ.get(
            "GOOGLE_CLOUD_LOCATION", "us-central1"
        ).strip(),
        redis_url=os.environ.get("REDIS_URL") or None,
        allow_document_uploads=os.environ.get(
            "ALLOW_DOCUMENT_UPLOADS", "false"
        ).lower()
        == "true",
        aqi_alert_threshold=threshold,
    )
