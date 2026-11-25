from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.api import router
from backend.auth import auth_router
from backend.config import get_settings
from backend.database import engine


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

settings = get_settings()
app = FastAPI(
    title="AeroSense AI",
    version="2.0.0",
    description="Source-attributed environmental monitoring and intelligence API.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)
app.include_router(auth_router)
app.include_router(router)


@app.get("/health/live", tags=["health"])
def liveness():
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
def readiness():
    if get_settings().auth_secret_key is None:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependency": "authentication_secret"},
        )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependency": "database"},
        ) from error
    return {
        "status": "ready",
        "database": "connected",
        "authentication": "configured",
    }


@app.get("/", include_in_schema=False)
def root():
    return {
        "name": "AeroSense AI",
        "version": app.version,
        "docs": "/docs",
        "health": "/health/live",
    }
