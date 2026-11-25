import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import backend.agents as agents_module
import backend.api as api_module
import backend.auth as auth_module
import backend.config as config_module
import backend.database as database_module
import backend.ingestion as ingestion_module
import backend.main as main_module
from backend.integrations import ProviderBatch
from backend.database import (
    AgentTask,
    Alert,
    Anomaly,
    ApiSource,
    Base,
    City,
    Document,
    DocumentChunk,
    EnvironmentalEvent,
    Forecast,
    GeographyPoint,
    IngestionRun,
    Investigation,
    ModelEvaluation,
    MonitoringStation,
    Report,
    SourceObservation,
    UserAccount,
    get_session,
)
from backend.auth import hash_password


@compiles(GeographyPoint, "sqlite")
def _compile_geography_for_sqlite(type_, compiler, **kwargs):
    del type_, compiler, kwargs
    return "TEXT"


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.environment_patch = patch.dict(
            os.environ,
            {
                "AUTH_SECRET_KEY": "test-auth-secret-key-that-is-at-least-32-bytes",
                "AUTH_COOKIE_SECURE": "false",
            },
        )
        self.environment_patch.start()
        config_module.get_settings.cache_clear()
        self.original_main_settings = main_module.settings
        main_module.settings = config_module.get_settings()
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "api-test.sqlite"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.original_session_local = database_module.SessionLocal
        self.original_api_session_local = api_module.SessionLocal
        self.original_agents_session_local = agents_module.SessionLocal
        self.original_main_engine = main_module.engine
        database_module.SessionLocal = self.session_factory
        api_module.SessionLocal = self.session_factory
        agents_module.SessionLocal = self.session_factory
        main_module.engine = self.engine

        def override_session():
            with self.session_factory() as session:
                yield session

        main_module.app.dependency_overrides[get_session] = override_session
        self.now = datetime.now(timezone.utc)
        self._seed()
        self.client = TestClient(main_module.app)
        csrf = self.client.get("/api/v1/auth/csrf")
        self.assertEqual(csrf.status_code, 200, csrf.text)
        self.client.headers.update({"X-CSRF-Token": csrf.json()["csrf_token"]})
        login = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.test", "password": "test-admin-password"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.client.headers.update(
            {"X-CSRF-Token": self.client.cookies.get("aerosense_csrf")}
        )

    def tearDown(self):
        self.client.close()
        main_module.app.dependency_overrides.clear()
        database_module.SessionLocal = self.original_session_local
        api_module.SessionLocal = self.original_api_session_local
        agents_module.SessionLocal = self.original_agents_session_local
        main_module.engine = self.original_main_engine
        main_module.settings = self.original_main_settings
        self.environment_patch.stop()
        config_module.get_settings.cache_clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _seed(self):
        with self.session_factory.begin() as session:
            session.add(
                UserAccount(
                    email="admin@example.test",
                    password_hash=hash_password("test-admin-password"),
                    role="admin",
                    is_active=True,
                )
            )
            session.add_all(
                [
                    City(
                        id="chennai",
                        name="Chennai",
                        country_code="IN",
                        latitude=13.08,
                        longitude=80.27,
                        active=True,
                    ),
                    ApiSource(
                        id="openaq",
                        display_name="OpenAQ",
                        attribution="OpenAQ",
                        configured=True,
                        active=True,
                        last_success_at=self.now,
                        last_attempt_at=self.now,
                    ),
                    ApiSource(
                        id="open_meteo_weather",
                        display_name="Open-Meteo",
                        attribution="Open-Meteo",
                        configured=True,
                        active=True,
                    ),
                    ApiSource(
                        id="open_meteo_air_quality",
                        display_name="Open-Meteo Air Quality",
                        attribution="Open-Meteo",
                        configured=True,
                        active=True,
                    ),
                    ApiSource(
                        id="copernicus_cams",
                        display_name="Copernicus CAMS",
                        attribution="Copernicus Atmosphere Monitoring Service",
                        configured=False,
                        active=False,
                    ),
                ]
            )
            session.flush()
            station = MonitoringStation(
                city_id="chennai",
                source_id="openaq",
                provider_id="test-station",
                name="Test station",
                latitude=13.08,
                longitude=80.27,
                metadata_json={},
                active=True,
            )
            session.add(station)
            session.flush()
            self.station_id = station.id
            second_station = MonitoringStation(
                city_id="chennai",
                source_id="openaq",
                provider_id="test-station-2",
                name="Second test station",
                latitude=13.09,
                longitude=80.28,
                metadata_json={},
                active=True,
            )
            session.add(second_station)
            session.flush()
            self.second_station_id = second_station.id
            observation = SourceObservation(
                source_id="openaq",
                city_id="chennai",
                station_id=station.id,
                provider_record_id="pm25-1",
                metric="PM2.5",
                value=18.0,
                original_unit="µg/m³",
                canonical_unit="µg/m³",
                observed_at=self.now - timedelta(minutes=1),
                ingested_at=self.now,
                latitude=13.08,
                longitude=80.27,
                quality_status="valid",
                metadata_json={},
            )
            second_station_observation = SourceObservation(
                source_id="openaq",
                city_id="chennai",
                station_id=second_station.id,
                provider_record_id="pm25-2",
                metric="PM2.5",
                value=23.0,
                original_unit="µg/m³",
                canonical_unit="µg/m³",
                observed_at=self.now - timedelta(minutes=2),
                ingested_at=self.now,
                latitude=13.09,
                longitude=80.28,
                quality_status="valid",
                metadata_json={},
            )
            temperature = SourceObservation(
                source_id="open_meteo_weather",
                city_id="chennai",
                provider_record_id="temp-1",
                metric="Temperature",
                value=29.0,
                original_unit="°C",
                canonical_unit="°C",
                observed_at=self.now - timedelta(minutes=1),
                ingested_at=self.now,
                quality_status="valid",
                metadata_json={"measurement_kind": "modelled_current"},
            )
            session.add_all([observation, second_station_observation, temperature])
            session.flush()
            self.observation_id = observation.id
            anomaly = Anomaly(
                city_id="chennai",
                observation_id=observation.id,
                metric="PM2.5",
                severity="moderate",
                score=2.5,
                detected_at=self.now,
                description="Stored test anomaly.",
                status="open",
                metadata_json={},
            )
            session.add(anomaly)
            session.flush()
            session.add_all(
                [
                    Alert(
                        dedupe_key="test-alert",
                        city_id="chennai",
                        anomaly_id=anomaly.id,
                        severity="moderate",
                        title="Stored test alert",
                        description="Alert test record.",
                        created_at=self.now,
                        status="active",
                    ),
                    EnvironmentalEvent(
                        source_id="openaq",
                        city_id="chennai",
                        provider_record_id="event-1",
                        event_type="environmental_news",
                        title="Stored test event",
                        description="Context record.",
                        url="https://example.test/event",
                        observed_at=self.now,
                        latitude=13.08,
                        longitude=80.27,
                        confidence="medium",
                        metadata_json={},
                    ),
                    IngestionRun(
                        source_id="openaq",
                        city_id="chennai",
                        status="succeeded",
                        started_at=self.now - timedelta(minutes=2),
                        finished_at=self.now,
                        records_seen=1,
                        records_accepted=1,
                        records_rejected=0,
                        metadata_json={},
                    ),
                    Forecast(
                        city_id="chennai",
                        station_id=station.id,
                        metric="PM2.5",
                        model_name="test-model",
                        model_version="1",
                        generated_at=self.now,
                        target_at=self.now + timedelta(hours=6),
                        value=19.0,
                        metadata_json={},
                    ),
                    ModelEvaluation(
                        city_id="chennai",
                        station_id=station.id,
                        metric="PM2.5",
                        model_name="test-model",
                        model_version="1",
                        horizon_hours=6,
                        mae=1.0,
                        rmse=1.2,
                        r_squared=0.8,
                        training_period={},
                        evaluation_period={
                            "persistence_baseline": {
                                "name": "last_observation_persistence",
                                "mae": 2.0,
                                "rmse": 2.5,
                            }
                        },
                        evaluated_at=self.now,
                    ),
                    Investigation(
                        city_id="chennai",
                        question="Stored investigation",
                        status="completed",
                        answer="Stored answer",
                        evidence=[],
                        created_at=self.now,
                        completed_at=self.now,
                    ),
                    Report(
                        city_id="chennai",
                        report_type="daily",
                        title="Stored report",
                        content={"observations": 2},
                        created_at=self.now,
                    ),
                    Document(
                        title="Ground report",
                        source_url="https://example.test/report",
                        content_hash="a" * 64,
                        created_at=self.now,
                        metadata_json={},
                    ),
                ]
            )
            session.flush()
            investigation = session.scalar(
                select(Investigation).where(
                    Investigation.question == "Stored investigation"
                )
            )
            session.add(
                AgentTask(
                    investigation_id=investigation.id,
                    agent_name="environmental_supervisor",
                    status="completed",
                    started_at=self.now,
                    completed_at=self.now,
                    result={},
                )
            )
            document = session.scalar(
                select(Document).where(Document.title == "Ground report")
            )
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=0,
                    text="PM2.5 ground station measurements are stored with units.",
                    page_number=1,
                    embedding=None,
                    created_at=self.now,
                )
            )

    def test_database_backed_get_routes(self):
        paths = [
            "/",
            "/health/live",
            "/health/ready",
            "/api/v1/cities",
            "/api/v1/cities/chennai",
            "/api/v1/dashboard/overview?city_id=chennai",
            "/api/v1/air-quality/current?city_id=chennai",
            "/api/v1/air-quality/history?city_id=chennai&station_id="
            + str(self.station_id),
            "/api/v1/air-quality/stations?city_id=chennai",
            "/api/v1/weather/current?city_id=chennai",
            "/api/v1/weather/history?city_id=chennai",
            "/api/v1/forecasts?city_id=chennai",
            "/api/v1/models/performance?city_id=chennai",
            "/api/v1/sources",
            "/api/v1/sources/health",
            "/api/v1/sources/ingestion-history?city_id=chennai",
            "/api/v1/agents",
            "/api/v1/agents/status",
            "/api/v1/agents/tasks",
            "/api/v1/agents/investigations/1",
            "/api/v1/agents/investigations/1/events",
            "/api/v1/anomalies?city_id=chennai",
            "/api/v1/events?city_id=chennai",
            "/api/v1/events/1/evidence",
            "/api/v1/knowledge/documents",
            "/api/v1/alerts?city_id=chennai",
            "/api/v1/reports",
            "/api/v1/reports/1/pdf",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        overview = self.client.get(
            "/api/v1/dashboard/overview?city_id=chennai"
        ).json()
        pm25_rows = [
            row
            for row in overview["cities"][0]["measurements"]
            if row["metric"] == "PM2.5"
        ]
        self.assertEqual(len(pm25_rows), 2)
        performance = self.client.get(
            "/api/v1/models/performance?city_id=chennai"
        ).json()
        self.assertEqual(
            performance[0]["persistence_baseline"]["name"],
            "last_observation_persistence",
        )

    def test_city_aqi_aggregates_adequate_history_across_stations(self):
        with self.session_factory.begin() as session:
            for station_id, pm25 in (
                (self.station_id, 18.0),
                (self.second_station_id, 23.0),
            ):
                for index in range(9):
                    observed_at = self.now - timedelta(hours=23) + timedelta(
                        hours=2.5 * index
                    )
                    for metric, value in (("PM2.5", pm25), ("PM10", 40.0)):
                        session.add(
                            SourceObservation(
                                source_id="openaq",
                                city_id="chennai",
                                station_id=station_id,
                                provider_record_id=f"city-{station_id}-{metric}-{index}",
                                metric=metric,
                                value=value,
                                original_unit="µg/m³",
                                canonical_unit="µg/m³",
                                observed_at=observed_at,
                                ingested_at=self.now,
                                latitude=13.08,
                                longitude=80.27,
                                quality_status="valid",
                                metadata_json={},
                            )
                        )

        overview = self.client.get(
            "/api/v1/dashboard/overview?city_id=chennai"
        ).json()
        aqi = overview["cities"][0]["estimated_india_aqi"]
        self.assertEqual(aqi["value"], 40)
        self.assertEqual(aqi["basis"], "citywide_ground_24h")
        self.assertEqual(aqi["station_count"], 2)
        self.assertEqual(set(aqi["pollutants_used"]), {"PM2.5", "PM10"})

    def test_city_aqi_uses_fresh_modeled_city_data_when_ground_history_is_sparse(self):
        with self.session_factory.begin() as session:
            for metric, value in (("PM2.5", 57.3), ("PM10", 58.4)):
                session.add(
                    SourceObservation(
                        source_id="open_meteo_air_quality",
                        city_id="chennai",
                        provider_record_id=f"modeled-{metric}",
                        metric=metric,
                        value=value,
                        original_unit="µg/m³",
                        canonical_unit="µg/m³",
                        observed_at=self.now - timedelta(minutes=30),
                        ingested_at=self.now,
                        quality_status="valid",
                        metadata_json={"measurement_kind": "modelled_current"},
                    )
                )

        overview = self.client.get(
            "/api/v1/dashboard/overview?city_id=chennai"
        ).json()
        aqi = overview["cities"][0]["estimated_india_aqi"]
        self.assertEqual(aqi["value"], 96)
        self.assertEqual(aqi["basis"], "modeled_current")
        self.assertEqual(aqi["source_id"], "open_meteo_air_quality")
        self.assertEqual(set(aqi["pollutants_used"]), {"PM2.5", "PM10"})

    def test_city_aqi_does_not_use_stale_modeled_data(self):
        with self.session_factory.begin() as session:
            session.add(
                SourceObservation(
                    source_id="open_meteo_air_quality",
                    city_id="chennai",
                    provider_record_id="stale-modeled-pm25",
                    metric="PM2.5",
                    value=57.3,
                    original_unit="µg/m³",
                    canonical_unit="µg/m³",
                    observed_at=self.now - timedelta(hours=4),
                    ingested_at=self.now,
                    quality_status="valid",
                    metadata_json={"measurement_kind": "modelled_current"},
                )
            )

        overview = self.client.get(
            "/api/v1/dashboard/overview?city_id=chennai"
        ).json()
        aqi = overview["cities"][0]["estimated_india_aqi"]
        self.assertIsNone(aqi["value"])
        self.assertEqual(aqi["basis"], "unavailable")

    def test_provider_polling_intervals_are_throttled_per_city(self):
        with self.session_factory.begin() as session:
            session.add(
                IngestionRun(
                    source_id="copernicus_cams",
                    city_id="chennai",
                    status="failed",
                    started_at=self.now - timedelta(hours=1),
                )
            )
            session.add(
                IngestionRun(
                    source_id="open_meteo",
                    city_id="chennai",
                    status="succeeded",
                    started_at=self.now - timedelta(minutes=44),
                )
            )
            session.add(
                IngestionRun(
                    source_id="open_meteo_air_quality",
                    city_id="chennai",
                    status="succeeded",
                    started_at=self.now - timedelta(minutes=46),
                )
            )

        with patch.object(ingestion_module, "SessionLocal", self.session_factory):
            self.assertFalse(
                ingestion_module._source_poll_due("copernicus_cams", "chennai")
            )
            self.assertTrue(
                ingestion_module._source_poll_due("copernicus_cams", "bengaluru")
            )
            self.assertFalse(
                ingestion_module._source_poll_due("open_meteo", "chennai")
            )
            self.assertTrue(
                ingestion_module._source_poll_due("open_meteo", "bengaluru")
            )
            self.assertTrue(
                ingestion_module._source_poll_due(
                    "open_meteo_air_quality", "chennai"
                )
            )

    def test_dashboard_hides_legacy_nasa_power_solar_sentinel(self):
        with self.session_factory.begin() as session:
            session.add(
                SourceObservation(
                    source_id="nasa_power",
                    city_id="chennai",
                    provider_record_id="legacy-missing-solar",
                    metric="Solar radiation",
                    value=-999.0,
                    original_unit="kWh/m²/day",
                    canonical_unit="kWh/m²/day",
                    observed_at=self.now,
                    ingested_at=self.now,
                    quality_status="valid",
                    metadata_json={},
                )
            )

        overview = self.client.get(
            "/api/v1/dashboard/overview?city_id=chennai"
        ).json()
        self.assertNotIn(
            "Solar radiation",
            {row["metric"] for row in overview["cities"][0]["measurements"]},
        )

    def test_mutation_routes_persist_or_return_explicit_validation(self):
        forecast = self.client.post(
            "/api/v1/forecasts/generate?city_id=chennai&station_id="
            + str(self.station_id)
        )
        self.assertEqual(forecast.status_code, 200, forecast.text)
        self.assertEqual(forecast.json()["status"], "insufficient_data")

        search = self.client.post(
            "/api/v1/knowledge/search",
            json={"query": "PM2.5 station", "limit": 5},
        )
        self.assertEqual(search.status_code, 200, search.text)
        self.assertTrue(search.json()["results"])

        acknowledgment = self.client.post("/api/v1/alerts/1/acknowledge")
        self.assertEqual(acknowledgment.status_code, 200, acknowledgment.text)
        self.assertEqual(acknowledgment.json()["status"], "acknowledged")

        report = self.client.post(
            "/api/v1/reports/generate?city_id=chennai&report_type=weekly"
        )
        self.assertEqual(report.status_code, 201, report.text)
        self.assertEqual(report.json()["content"]["status"], "computed_from_persisted_records")

        disabled_upload = self.client.post(
            "/api/v1/knowledge/documents",
            data={"title": "Test upload"},
            files={"file": ("test.txt", b"stored text", "text/plain")},
        )
        self.assertEqual(disabled_upload.status_code, 403)

        invalid_city = self.client.get("/api/v1/cities/unknown")
        self.assertEqual(invalid_city.status_code, 404)
        self.assertIsInstance(invalid_city.json()["detail"], str)

        invalid_chat = self.client.post(
            "/api/v1/assistant/chat",
            json={"question": "Check this city", "city_id": "unknown"},
        )
        self.assertEqual(invalid_chat.status_code, 404)

        invalid_report = self.client.post(
            "/api/v1/reports/generate?report_type=monthly"
        )
        self.assertEqual(invalid_report.status_code, 422)

    def test_public_registration_creates_and_authenticates_viewer(self):
        client = TestClient(main_module.app)
        try:
            csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
            client.headers.update({"X-CSRF-Token": csrf})
            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": " New.User@Example.Test ",
                    "password": "test-registration-password",
                    "role": "admin",
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["email"], "new.user@example.test")
            self.assertEqual(response.json()["role"], "viewer")
            self.assertTrue(response.json()["csrf_token"])
            self.assertEqual(client.get("/api/v1/auth/me").json()["role"], "viewer")
            client.headers.update(
                {"X-CSRF-Token": client.cookies.get("aerosense_csrf")}
            )
            duplicate = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "new.user@example.test",
                    "password": "test-registration-password",
                },
            )
            self.assertEqual(duplicate.status_code, 409)

            with self.session_factory() as session:
                user = session.scalar(
                    select(UserAccount).where(
                        UserAccount.email == "new.user@example.test"
                    )
                )
                self.assertIsNotNone(user)
                self.assertEqual(user.role, "viewer")
                self.assertNotEqual(user.password_hash, "test-registration-password")
        finally:
            client.close()

    def test_public_registration_requires_csrf_and_rate_limits_ip(self):
        client = TestClient(main_module.app)
        try:
            payload = {
                "email": "first.signup@example.test",
                "password": "test-registration-password",
            }
            missing_csrf = client.post("/api/v1/auth/register", json=payload)
            self.assertEqual(missing_csrf.status_code, 403)

            csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
            client.headers.update({"X-CSRF-Token": csrf})
            with patch.object(auth_module, "MAX_SIGNUPS_PER_WINDOW", 1):
                first = client.post("/api/v1/auth/register", json=payload)
                self.assertEqual(first.status_code, 201, first.text)
                client.headers.update(
                    {"X-CSRF-Token": client.cookies.get("aerosense_csrf")}
                )
                second = client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": "second.signup@example.test",
                        "password": "test-registration-password",
                    },
                )
                self.assertEqual(second.status_code, 429)
        finally:
            client.close()

    def test_authentication_roles_and_resource_ownership(self):
        unauthenticated = TestClient(main_module.app)
        try:
            self.assertEqual(
                unauthenticated.get("/api/v1/cities").status_code, 401
            )
            csrf = unauthenticated.get("/api/v1/auth/csrf").json()["csrf_token"]
            missing_csrf = unauthenticated.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.test", "password": "test-admin-password"},
            )
            self.assertEqual(missing_csrf.status_code, 403)
            unauthenticated.headers.update({"X-CSRF-Token": csrf})
            login = unauthenticated.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.test", "password": "test-admin-password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertTrue(
                any(
                    "aerosense_access=" in cookie and "HttpOnly" in cookie
                    for cookie in login.headers.get_list("set-cookie")
                )
            )
        finally:
            unauthenticated.close()

        with self.session_factory.begin() as session:
            admin_id = session.scalar(
                select(UserAccount.id).where(UserAccount.email == "admin@example.test")
            )
            session.add(
                UserAccount(
                    email="viewer@example.test",
                    password_hash=hash_password("test-viewer-password"),
                    role="viewer",
                    is_active=True,
                )
            )
            investigation = Investigation(
                owner_user_id=admin_id,
                question="Private investigation",
                status="completed",
                created_at=self.now,
                evidence=[],
            )
            session.add(investigation)
            session.flush()
            investigation_id = investigation.id
        viewer = TestClient(main_module.app)
        try:
            csrf = viewer.get("/api/v1/auth/csrf").json()["csrf_token"]
            viewer.headers.update({"X-CSRF-Token": csrf})
            login = viewer.post(
                "/api/v1/auth/login",
                json={"email": "viewer@example.test", "password": "test-viewer-password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            viewer.headers.update(
                {"X-CSRF-Token": viewer.cookies.get("aerosense_csrf")}
            )
            self.assertEqual(viewer.get("/api/v1/cities").status_code, 200)
            forecast = viewer.post(
                "/api/v1/forecasts/generate?city_id=chennai&station_id="
                + str(self.station_id)
            )
            self.assertEqual(forecast.status_code, 200, forecast.text)
            self.assertEqual(
                viewer.get("/api/v1/models/performance?city_id=chennai").status_code,
                200,
            )
            search = viewer.post(
                "/api/v1/knowledge/search",
                json={"query": "PM2.5 station", "limit": 5},
            )
            self.assertEqual(search.status_code, 200, search.text)
            acknowledgment = viewer.post("/api/v1/alerts/1/acknowledge")
            self.assertEqual(acknowledgment.status_code, 200, acknowledgment.text)
            report = viewer.post("/api/v1/reports/generate")
            self.assertEqual(report.status_code, 201, report.text)
            viewer_reports = viewer.get("/api/v1/reports")
            self.assertEqual(viewer_reports.status_code, 200, viewer_reports.text)
            self.assertTrue(
                any(item["id"] == report.json()["id"] for item in viewer_reports.json())
            )
            with patch.object(
                api_module,
                "get_settings",
                return_value=SimpleNamespace(allow_document_uploads=True),
            ):
                upload = viewer.post(
                    "/api/v1/knowledge/documents",
                    data={"title": "Viewer uploaded document"},
                    files={"file": ("viewer.txt", b"stored text", "text/plain")},
                )
            self.assertEqual(upload.status_code, 201, upload.text)

            with patch.object(api_module, "_execute_investigation") as execute:
                for path in ("/api/v1/assistant/chat", "/api/v1/agents/investigate"):
                    response = viewer.post(
                        path,
                        json={
                            "question": "Why did PM2.5 change?",
                            "city_id": "chennai",
                        },
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    viewer_investigation_id = response.json()["investigation_id"]
                    self.assertEqual(
                        viewer.get(
                            f"/api/v1/agents/investigations/{viewer_investigation_id}"
                        ).status_code,
                        200,
                    )
                self.assertEqual(execute.call_count, 2)

            report = self.client.post("/api/v1/reports/generate")
            self.assertEqual(report.status_code, 201, report.text)
            report_id = report.json()["id"]
            self.assertEqual(
                viewer.get(f"/api/v1/reports/{report_id}/pdf").status_code, 404
            )
            self.assertEqual(
                viewer.get(
                    f"/api/v1/agents/investigations/{investigation_id}"
                ).status_code,
                404,
            )
        finally:
            viewer.close()

    def test_refresh_rotates_tokens_and_logout_revokes_session(self):
        old_access = self.client.cookies.get("aerosense_access")
        old_refresh = self.client.cookies.get("aerosense_refresh")
        refresh = self.client.post("/api/v1/auth/refresh")
        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.assertNotEqual(self.client.cookies.get("aerosense_access"), old_access)
        self.assertNotEqual(self.client.cookies.get("aerosense_refresh"), old_refresh)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 200)

        logout = self.client.post("/api/v1/auth/logout")
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertEqual(self.client.get("/api/v1/cities").status_code, 401)

    def test_investigation_executes_and_persists_agent_history(self):
        response = self.client.post(
            "/api/v1/agents/investigate",
            json={"question": "What does stored PM2.5 evidence show?", "city_id": "chennai"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        investigation_id = response.json()["investigation_id"]
        details = self.client.get(
            f"/api/v1/agents/investigations/{investigation_id}"
        )
        self.assertEqual(details.status_code, 200, details.text)
        body = details.json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("does not establish a cause", body["answer"])
        self.assertEqual(len(body["tasks"]), 9)
        self.assertTrue(
            all(task["status"] != "queued" and task["status"] != "running" for task in body["tasks"])
        )
        self.assertTrue(any(item.get("observation_id") == self.observation_id for item in body["evidence"]))

    def test_gemini_failure_still_persists_investigation_evidence(self):
        from google.genai.errors import ServerError

        with patch.object(
            agents_module,
            "get_settings",
            return_value=SimpleNamespace(gemini_api_key="test-gemini-key"),
        ):
            with patch("google.genai.Client") as gemini_client:
                gemini_client.return_value.models.generate_content.side_effect = (
                    ServerError(
                        503,
                        {
                            "error": {
                                "code": 503,
                                "message": "Service temporarily unavailable.",
                                "status": "UNAVAILABLE",
                            }
                        },
                    )
                )
                response = self.client.post(
                    "/api/v1/agents/investigate",
                    json={
                        "question": "Why did PM2.5 change?",
                        "city_id": "chennai",
                    },
                )

        self.assertEqual(response.status_code, 202, response.text)
        investigation_id = response.json()["investigation_id"]
        details = self.client.get(
            f"/api/v1/agents/investigations/{investigation_id}"
        )
        self.assertEqual(details.status_code, 200, details.text)
        body = details.json()
        self.assertEqual(body["status"], "completed")
        self.assertIn("Gemini was unavailable (HTTP 503)", body["answer"])
        self.assertIn("do not contain two observations from the same source and station", body["answer"])
        self.assertIn("station 1", body["answer"])
        self.assertIn("station 2", body["answer"])
        self.assertIn("does not establish a cause", body["answer"])
        self.assertTrue(body["evidence"])
        self.assertTrue(
            all(
                task["status"] not in {"queued", "running"}
                for task in body["tasks"]
            )
        )

    def test_pm25_evidence_fallback_compares_only_matching_series(self):
        evidence = [
            {
                "observation_id": 1,
                "city_id": "chennai",
                "source_id": "open_meteo_air_quality",
                "station_id": None,
                "metric": "PM2.5",
                "value": 12.0,
                "unit": "µg/m³",
                "observed_at": "2026-11-24T13:00:00+00:00",
            },
            {
                "observation_id": 2,
                "city_id": "chennai",
                "source_id": "open_meteo_air_quality",
                "station_id": None,
                "metric": "PM2.5",
                "value": 11.9,
                "unit": "µg/m³",
                "observed_at": "2026-11-24T14:00:00+00:00",
            },
            {
                "observation_id": 3,
                "city_id": "chennai",
                "source_id": "openaq",
                "station_id": 5,
                "metric": "PM2.5",
                "value": 19.7,
                "unit": "µg/m³",
                "observed_at": "2026-11-24T13:00:00+00:00",
            },
        ]
        answer = agents_module._evidence_only_answer(
            evidence, "Why did PM2.5 change?", gemini_status=503
        )
        self.assertIn("Open-Meteo city-level: 12 to 11.9 µg/m³", answer)
        self.assertIn("decreased 0.1 µg/m³", answer)
        self.assertIn("OpenAQ station 5: 19.7 µg/m³", answer)
        self.assertIn("records 1 and 2", answer)
        self.assertIn("does not establish a cause", answer)

    def test_gemini_model_failover_uses_available_model(self):
        from google.genai.errors import ServerError

        with patch.object(
            agents_module,
            "get_settings",
            return_value=SimpleNamespace(gemini_api_key="test-gemini-key"),
        ):
            with patch("google.genai.Client") as gemini_client:
                gemini_client.return_value.models.generate_content.side_effect = [
                    ServerError(
                        503,
                        {"error": {"code": 503, "status": "UNAVAILABLE"}},
                    ),
                    SimpleNamespace(text="Generated answer with stored citations."),
                ]
                answer = agents_module._generate_answer(
                    "Why did PM2.5 change?",
                    "chennai",
                    [
                        {
                            "agent": "air_quality_analysis",
                            "evidence": [
                                {
                                    "observation_id": 1,
                                    "metric": "PM2.5",
                                    "value": 12.0,
                                    "unit": "µg/m³",
                                    "source_id": "openaq",
                                    "station_id": 1,
                                    "city_id": "chennai",
                                    "observed_at": "2026-11-24T13:00:00+00:00",
                                }
                            ],
                        }
                    ],
                )

        self.assertEqual(answer, "Generated answer with stored citations.")
        calls = gemini_client.return_value.models.generate_content.call_args_list
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            ["gemini-3.6-flash", "gemini-3.8-flash"],
        )

    def test_vertex_ai_client_uses_project_and_location(self):
        settings = SimpleNamespace(
            gemini_provider="vertex_ai",
            google_cloud_project="test-project",
            google_cloud_location="us-central1",
        )
        with patch("google.genai.Client") as gemini_client:
            client = agents_module._create_gemini_client(settings)

        self.assertIs(client, gemini_client.return_value)
        self.assertEqual(
            gemini_client.call_args.kwargs["vertexai"],
            True,
        )
        self.assertEqual(
            gemini_client.call_args.kwargs["project"],
            "test-project",
        )
        self.assertEqual(
            gemini_client.call_args.kwargs["location"],
            "us-central1",
        )

    def test_vertex_ai_without_adc_returns_cited_evidence_fallback(self):
        from google.auth.exceptions import DefaultCredentialsError

        settings = SimpleNamespace(
            gemini_provider="vertex_ai",
            google_cloud_project="test-project",
            google_cloud_location="us-central1",
            gemini_api_key=None,
        )
        evidence = [
            {
                "observation_id": 1,
                "metric": "PM2.5",
                "value": 20.0,
                "unit": "µg/m³",
                "source_id": "openaq",
                "station_id": 7,
                "observed_at": "2026-11-24T13:00:00+00:00",
            }
        ]
        with patch.object(agents_module, "get_settings", return_value=settings):
            with patch(
                "google.genai.Client",
                side_effect=DefaultCredentialsError("No ADC credentials"),
            ):
                answer = agents_module._generate_answer(
                    "Why did PM2.5 change?",
                    "chennai",
                    [{"agent": "air_quality_analysis", "evidence": evidence}],
                )

        self.assertIn("Vertex AI credentials or project are not configured", answer)
        self.assertIn("record 1", answer)
        self.assertIn("does not establish a cause", answer)

    def test_ingestion_counts_inserted_rows_and_rejects_invalid_observations(self):
        observed_at = self.now - timedelta(minutes=1)
        valid = {
            "city_id": "chennai",
            "source": "openaq",
            "provider_record_id": "valid-record",
            "metric": "PM2.5",
            "value": 20.0,
            "original_unit": "µg/m³",
            "unit": "µg/m³",
            "observed_at": observed_at,
            "latitude": None,
            "longitude": None,
        }
        rows = [
            valid,
            {**valid},
            {**valid, "provider_record_id": ""},
            {**valid, "provider_record_id": "wrong-source", "source": "nasa_power"},
            {**valid, "provider_record_id": "wrong-city", "city_id": "delhi"},
            {
                **valid,
                "provider_record_id": "invalid-coordinate",
                "latitude": True,
                "longitude": 80.27,
            },
        ]
        with self.session_factory.begin() as session:
            run = IngestionRun(
                source_id="openaq",
                city_id="chennai",
                status="running",
                started_at=self.now,
            )
            session.add(run)
            session.flush()
            accepted, rejected = ingestion_module._store_observations(
                session,
                ProviderBatch("openaq", "chennai", self.now, observations=rows),
                run,
                {},
                {},
                None,
            )
        self.assertEqual((accepted, rejected), (1, 4))

    def test_spike_alert_deduplication_does_not_abort_ingestion(self):
        dedupe_key = (
            f"spike:{self.station_id}:PM2.5:{self.now.strftime('%Y%m%d')}"
        )
        with self.session_factory.begin() as session:
            session.add_all(
                [
                    SourceObservation(
                        source_id="openaq",
                        city_id="chennai",
                        station_id=self.station_id,
                        provider_record_id=f"baseline-{index}",
                        metric="PM2.5",
                        value=10.0,
                        original_unit="µg/m³",
                        canonical_unit="µg/m³",
                        observed_at=self.now - timedelta(hours=8 - index),
                        quality_status="valid",
                        metadata_json={},
                    )
                    for index in range(5)
                ]
            )
            observation = SourceObservation(
                source_id="openaq",
                city_id="chennai",
                station_id=self.station_id,
                provider_record_id="spike-replay",
                metric="PM2.5",
                value=100.0,
                original_unit="µg/m³",
                canonical_unit="µg/m³",
                observed_at=self.now,
                quality_status="valid",
                metadata_json={},
            )
            session.add(observation)
            session.add(
                Alert(
                    dedupe_key=dedupe_key,
                    city_id="chennai",
                    severity="high",
                    title="Existing spike alert",
                    description="Already recorded for this station and day.",
                    created_at=self.now,
                    status="active",
                )
            )
            session.flush()

            ingestion_module._evaluate_station_reading(
                session,
                observation.id,
                self.station_id,
                "chennai",
                "PM2.5",
                observation.value,
                "µg/m³",
                self.now,
            )

        with self.session_factory() as session:
            self.assertTrue(
                session.scalar(
                    select(Anomaly.id).where(
                        Anomaly.observation_id == observation.id
                    )
                )
                is not None
            )
            self.assertTrue(
                session.scalar(
                    select(Alert.id).where(Alert.dedupe_key == dedupe_key)
                )
                is not None
            )

    def test_aqi_alert_deduplication_in_a_single_ingestion_batch(self):
        dedupe_key = (
            f"aqi100:{self.station_id}:PM2.5:{self.now.strftime('%Y%m%d')}"
        )
        observed_at = self.now.replace(tzinfo=None)
        with self.session_factory.begin() as session:
            session.add_all(
                [
                    SourceObservation(
                        source_id="openaq",
                        city_id="chennai",
                        station_id=self.station_id,
                        provider_record_id=f"high-baseline-{index}",
                        metric="PM2.5",
                        value=100.0,
                        original_unit="µg/m³",
                        canonical_unit="µg/m³",
                        observed_at=observed_at - timedelta(hours=20 - index),
                        quality_status="valid",
                        metadata_json={},
                    )
                    for index in range(16)
                ]
            )
            observations = [
                SourceObservation(
                    source_id="openaq",
                    city_id="chennai",
                    station_id=self.station_id,
                    provider_record_id=f"high-current-{index}",
                    metric="PM2.5",
                    value=100.0,
                    original_unit="µg/m³",
                    canonical_unit="µg/m³",
                    observed_at=observed_at + timedelta(minutes=index),
                    quality_status="valid",
                    metadata_json={},
                )
                for index in range(2)
            ]
            session.add_all(observations)
            session.flush()
            for observation in observations:
                ingestion_module._evaluate_station_reading(
                    session,
                    observation.id,
                    self.station_id,
                    "chennai",
                    "PM2.5",
                    observation.value,
                    "µg/m³",
                    observation.observed_at,
                )

        with self.session_factory() as session:
            self.assertTrue(
                session.scalar(
                    select(Alert.id).where(Alert.dedupe_key == dedupe_key)
                )
                is not None
            )
            self.assertEqual(
                len(
                    session.scalars(
                        select(Alert.id).where(Alert.dedupe_key == dedupe_key)
                    ).all()
                ),
                1,
            )

    def test_database_sse_emits_new_observations(self):
        class ConnectedRequest:
            def __init__(self, session_id):
                self.state = SimpleNamespace(auth_session_id=session_id)

            async def is_disconnected(self):
                return False

        async def consume_one():
            session_id = self.client.cookies.get("aerosense_access").split(".", 1)[0]
            response = await api_module.dashboard_live_events(
                ConnectedRequest(session_id)
            )
            original_to_thread = asyncio.to_thread

            async def insert_after_cursor_read(function, *args, **kwargs):
                result = await original_to_thread(function, *args, **kwargs)
                if function.__name__ == "read_cursors":
                    with self.session_factory.begin() as session:
                        session.add(
                            SourceObservation(
                                source_id="openaq",
                                city_id="chennai",
                                station_id=self.station_id,
                                provider_record_id="pm25-sse",
                                metric="PM2.5",
                                value=22.0,
                                original_unit="µg/m³",
                                canonical_unit="µg/m³",
                                observed_at=self.now,
                                ingested_at=self.now,
                                quality_status="valid",
                                metadata_json={},
                            )
                        )
                return result

            with patch.object(api_module.asyncio, "to_thread", insert_after_cursor_read):
                chunk = await anext(response.body_iterator)
                await response.body_iterator.aclose()
            return chunk

        chunk = asyncio.run(consume_one())
        self.assertIn("event: observation", chunk)
        self.assertIn('"value": 22.0', chunk)


if __name__ == "__main__":
    unittest.main()
