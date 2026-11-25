import unittest
from contextlib import redirect_stdout
from http.client import HTTPMessage
from io import StringIO
from urllib.error import HTTPError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from data_engineering import ingest_once, ingest_schedule
from backend.integrations import (
    CityPoint,
    CopernicusCamsAdapter,
    HttpClient,
    NasaPowerAdapter,
    OpenAQAdapter,
    OpenMeteoAdapter,
    ProviderError,
    adapter_catalog,
    _finite_number,
    _normalize_value,
    _utc_datetime,
)
from backend.ingestion import _raw_payload_row


class FakeClient:
    def __init__(self, responses, status_code=200):
        self.responses = iter(responses)
        self.calls = []
        self.status_code = status_code

    def get_json(self, url, *, source, headers=None):
        self.calls.append((url, source, headers))
        return next(self.responses)

    def get_json_with_status(self, url, *, source, headers=None):
        return self.get_json(url, source=source, headers=headers), self.status_code


class IntegrationNormalizationTests(unittest.TestCase):
    def test_normalizes_common_units_and_rejects_unknown_units(self):
        self.assertEqual(
            _normalize_value("Wind speed", 2, "m/s", "km/h"), (7.2, "km/h")
        )
        self.assertEqual(
            _normalize_value("CO", 1, "ppm", "µg/m³"),
            (28.01 * 1000 / 24.45, "µg/m³"),
        )
        self.assertEqual(
            _normalize_value("PM2.5", 0.00000002, "kg m-3", "kg m-3"),
            (20.0, "µg/m³"),
        )
        self.assertIsNone(_normalize_value("PM2.5", 12, "ppm", "µg/m³"))

    def test_numeric_and_timestamp_validation(self):
        self.assertIsNone(_finite_number(True))
        self.assertIsNone(_finite_number(float("inf")))
        parsed = _utc_datetime("2026-11-22T17:00:00Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_openaq_parser_retains_station_sensor_and_original_unit(self):
        client = FakeClient(
            [
                {
                    "results": [
                        {
                            "id": 5,
                            "name": "Central station",
                            "coordinates": {"latitude": 13.08, "longitude": 80.27},
                            "sensors": [
                                {
                                    "id": 42,
                                    "parameter": {"name": "pm25", "units": "µg/m³"},
                                }
                            ],
                        }
                    ]
                },
                {
                    "results": [
                        {
                            "sensorsId": 42,
                            "value": 20.5,
                            "datetime": {"utc": "2026-11-22T17:00:00Z"},
                        }
                    ]
                },
            ],
        )
        batch = OpenAQAdapter("test-key", client).fetch(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707)
        )
        self.assertEqual(len(batch.observations), 1)
        observation = batch.observations[0]
        self.assertEqual(observation["station_provider_id"], "5")
        self.assertEqual(observation["sensor_provider_id"], "42")
        self.assertEqual(observation["original_unit"], "µg/m³")
        self.assertEqual(observation["unit"], "µg/m³")
        self.assertEqual(observation["metric"], "PM2.5")
        self.assertEqual(len(batch.raw_payloads), 2)

    def test_openaq_history_uses_period_start_timestamp(self):
        client = FakeClient(
            [
                {
                    "results": [
                        {
                            "id": 5,
                            "name": "Central station",
                            "coordinates": {"latitude": 13.08, "longitude": 80.27},
                            "sensors": [
                                {
                                    "id": 42,
                                    "parameter": {"name": "pm25", "units": "µg/m³"},
                                }
                            ],
                        }
                    ]
                },
                {
                    "results": [
                        {
                            "value": 20.5,
                            "period": {
                                "datetimeFrom": {
                                    "utc": "2026-11-22T17:00:00Z",
                                },
                                "datetimeTo": {
                                    "utc": "2026-11-22T17:15:00Z",
                                },
                            },
                        }
                    ]
                },
            ],
        )

        batch = OpenAQAdapter("test-key", client).fetch_history(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707),
            datetime(2026, 11, 22, tzinfo=timezone.utc),
            datetime(2026, 11, 23, tzinfo=timezone.utc),
        )

        self.assertEqual(len(batch.observations), 1)
        observation = batch.observations[0]
        self.assertEqual(
            observation["observed_at"],
            datetime(2026, 11, 22, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(observation["station_provider_id"], "5")
        self.assertEqual(observation["sensor_provider_id"], "42")

    def test_openaq_history_paginates_and_tracks_payload_page(self):
        client = FakeClient(
            [
                {
                    "results": [
                        {
                            "id": 5,
                            "name": "Central station",
                            "coordinates": {"latitude": 13.08, "longitude": 80.27},
                            "sensors": [
                                {
                                    "id": 42,
                                    "parameter": {"name": "pm25", "units": "µg/m³"},
                                }
                            ],
                        }
                    ]
                },
                {
                    "results": [
                        {
                            "value": 20.5,
                            "period": {"datetimeFrom": {"utc": "2026-11-22T17:00:00Z"}},
                        },
                        {
                            "value": 21.5,
                            "period": {"datetimeFrom": {"utc": "2026-11-22T18:00:00Z"}},
                        },
                    ]
                },
                {
                    "results": [
                        {
                            "value": 22.5,
                            "period": {"datetimeFrom": {"utc": "2026-11-22T19:00:00Z"}},
                        }
                    ]
                },
            ],
        )

        batch = OpenAQAdapter("test-key", client).fetch_history(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707),
            datetime(2026, 11, 22, tzinfo=timezone.utc),
            datetime(2026, 11, 23, tzinfo=timezone.utc),
            limit=2,
        )

        self.assertEqual(len(batch.observations), 3)
        self.assertEqual(
            [row["raw_payload_page"] for row in batch.observations],
            [1, 1, 2],
        )
        measurement_urls = [call[0] for call in client.calls[1:]]
        self.assertIn("page=1", measurement_urls[0])
        self.assertIn("page=2", measurement_urls[1])
        self.assertEqual(batch.metadata["history_pages"], 2)
        self.assertEqual(batch.metadata["truncated_sensors"], [])

    def test_openaq_current_fetch_includes_the_eighth_nearest_station(self):
        locations = [
            {
                "id": station_id,
                "name": f"Station {station_id}",
                "coordinates": {
                    "latitude": 13.0827 + station_id / 10000,
                    "longitude": 80.2707,
                },
                "sensors": [
                    {
                        "id": 100 + station_id,
                        "parameter": {"name": "pm25", "units": "µg/m³"},
                    }
                ],
            }
            for station_id in range(1, 9)
        ]
        client = FakeClient(
            [
                {"results": locations},
                *[
                    {
                        "results": [
                            {
                                "sensorsId": 100 + station_id,
                                "value": 20.0,
                                "datetime": {"utc": "2026-11-22T17:00:00Z"},
                            }
                        ]
                    }
                    for station_id in range(1, 9)
                ],
            ]
        )

        batch = OpenAQAdapter("test-key", client).fetch(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707)
        )

        self.assertEqual(
            {row["station_provider_id"] for row in batch.observations},
            {str(station_id) for station_id in range(1, 9)},
        )

    def test_openaq_history_includes_the_eighth_nearest_station(self):
        locations = [
            {
                "id": station_id,
                "name": f"Station {station_id}",
                "coordinates": {
                    "latitude": 13.0827 + station_id / 10000,
                    "longitude": 80.2707,
                },
                "sensors": [
                    {
                        "id": 100 + station_id,
                        "parameter": {"name": "pm25", "units": "µg/m³"},
                    }
                ],
            }
            for station_id in range(1, 9)
        ]
        client = FakeClient(
            [
                {"results": locations},
                *[
                    {
                        "results": [
                            {
                                "value": 20.0,
                                "period": {
                                    "datetimeFrom": {
                                        "utc": "2026-11-22T17:00:00Z",
                                    },
                                },
                            }
                        ]
                    }
                    for _ in range(8)
                ],
            ]
        )

        batch = OpenAQAdapter("test-key", client).fetch_history(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707),
            datetime(2026, 11, 22, tzinfo=timezone.utc),
            datetime(2026, 11, 23, tzinfo=timezone.utc),
        )

        self.assertIn(
            "8",
            {row["station_provider_id"] for row in batch.observations},
        )

    def test_openaq_without_key_fails_explicitly(self):
        with self.assertRaisesRegex(ProviderError, "not configured"):
            OpenAQAdapter(None).fetch(
                CityPoint("chennai", "Chennai", 13.0827, 80.2707)
            )

    def test_open_meteo_parser_marks_weather_as_modelled_and_keeps_units(self):
        client = FakeClient(
            [
                {
                    "current": {
                        "time": "2026-11-22T17:00",
                        "temperature_2m": 27.1,
                        "relative_humidity_2m": 72,
                        "wind_speed_10m": 2.0,
                    },
                    "current_units": {
                        "temperature_2m": "°C",
                        "relative_humidity_2m": "%",
                        "wind_speed_10m": "m/s",
                    },
                }
            ],
            status_code=206,
        )
        batch = OpenMeteoAdapter(client).fetch_weather(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707)
        )
        self.assertEqual(len(batch.observations), 3)
        by_metric = {row["metric"]: row for row in batch.observations}
        self.assertEqual(by_metric["Temperature"]["unit"], "°C")
        self.assertEqual(by_metric["Wind speed"]["value"], 7.2)
        self.assertEqual(by_metric["Wind speed"]["original_unit"], "m/s")
        self.assertEqual(by_metric["Wind speed"]["unit"], "km/h")
        self.assertEqual(
            by_metric["Temperature"]["metadata"]["measurement_kind"],
            "modelled_current",
        )
        self.assertEqual(batch.raw_payloads[0]["response_status"], 206)

    def test_cams_forecast_download_is_parsed_and_attributed_without_saving_token(self):
        from netCDF4 import Dataset

        with tempfile.TemporaryDirectory() as directory:
            netcdf_path = Path(directory) / "cams.nc"
            archive_path = Path(directory) / "cams.zip"
            with Dataset(netcdf_path, "w", format="NETCDF4") as dataset:
                dataset.createDimension("latitude", 2)
                dataset.createDimension("longitude", 2)
                latitudes = dataset.createVariable("latitude", "f4", ("latitude",))
                latitudes.standard_name = "latitude"
                latitudes[:] = [13.0, 13.4]
                longitudes = dataset.createVariable("longitude", "f4", ("longitude",))
                longitudes.standard_name = "longitude"
                longitudes[:] = [80.0, 80.4]
                pm25 = dataset.createVariable(
                    "particulate_matter_2.5um",
                    "f4",
                    ("latitude", "longitude"),
                )
                pm25.units = "kg m-3"
                pm25[:] = [[1e-8, 2e-8], [3e-8, 4e-8]]
                pm10 = dataset.createVariable(
                    "particulate_matter_10um",
                    "f4",
                    ("latitude", "longitude"),
                )
                pm10.units = "kg m-3"
                pm10[:] = [[4e-8, 5e-8], [6e-8, 7e-8]]
            with ZipFile(archive_path, "w") as archive:
                archive.write(netcdf_path, "cams.nc")
            archive_bytes = archive_path.read_bytes()

        calls = []

        class FakeCamsClient:
            def retrieve(self, dataset_name, request, target):
                calls.append((dataset_name, request))
                Path(target).write_bytes(archive_bytes)

        def fake_client_factory(**kwargs):
            self.assertEqual(kwargs["url"], CopernicusCamsAdapter.api_url)
            self.assertTrue(kwargs["quiet"])
            return FakeCamsClient()

        now = datetime(2026, 11, 24, 6, 40, tzinfo=timezone.utc)
        batch = CopernicusCamsAdapter(
            "test-cams-token",
            client_factory=fake_client_factory,
            clock=lambda: now,
        ).fetch(CityPoint("chennai", "Chennai", 13.08, 80.27))

        self.assertEqual(batch.source, "copernicus_cams")
        self.assertEqual(calls[0][0], CopernicusCamsAdapter.dataset)
        self.assertEqual(calls[0][1]["date"], ["2026-11-23"])
        self.assertEqual(calls[0][1]["leadtime_hour"], ["30"])
        self.assertEqual(calls[0][1]["data_format"], "netcdf_zip")
        by_metric = {row["metric"]: row for row in batch.observations}
        self.assertAlmostEqual(by_metric["PM2.5"]["value"], 2e-8, places=14)
        self.assertAlmostEqual(by_metric["PM10"]["value"], 5e-8, places=14)
        self.assertEqual(by_metric["PM2.5"]["original_unit"], "kg m-3")
        self.assertEqual(
            by_metric["PM2.5"]["metadata"]["measurement_kind"],
            "modelled_forecast",
        )
        self.assertNotIn("test-cams-token", str(batch.raw_payloads))

    def test_cams_is_enabled_only_when_an_api_token_is_configured(self):
        catalog = adapter_catalog(
            openaq_api_key=None,
            firms_map_key=None,
            cams_api_key="test-cams-token",
        )
        self.assertTrue(catalog["copernicus_cams"]["configured"])
        self.assertIsInstance(
            catalog["copernicus_cams"]["adapter"], CopernicusCamsAdapter
        )

    def test_nasa_power_wind_speed_is_normalized_and_original_unit_retained(self):
        client = FakeClient(
            [
                {
                    "properties": {
                        "parameter": {
                            "WS10M": {"2026112308": 2.0},
                            "T2M": {"2026112308": 25.0},
                            "ALLSKY_SFC_SW_DWN": {"2026112308": -999.0},
                        }
                    }
                }
            ]
        )
        batch = NasaPowerAdapter(client).fetch(
            CityPoint("chennai", "Chennai", 13.0827, 80.2707),
            datetime(2026, 11, 23, tzinfo=timezone.utc),
            datetime(2026, 11, 24, tzinfo=timezone.utc),
        )

        by_metric = {row["metric"]: row for row in batch.observations}
        self.assertEqual(by_metric["Wind speed"]["value"], 7.2)
        self.assertEqual(by_metric["Wind speed"]["original_unit"], "m/s")
        self.assertEqual(by_metric["Wind speed"]["unit"], "km/h")
        self.assertNotIn("Solar radiation", by_metric)

    def test_scheduled_ingestion_visits_each_active_city(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.scalars.return_value.all.return_value = [
            "bengaluru",
            "chennai",
        ]
        expected = [{"status": "succeeded"}]

        with (
            patch.object(ingest_schedule, "SessionLocal", return_value=session),
            patch.object(ingest_schedule, "ingest_city", return_value=expected) as ingest,
        ):
            results = ingest_schedule.ingest_active_cities_once()

        self.assertEqual(results, expected * 2)
        self.assertEqual(
            [call.args[0] for call in ingest.call_args_list],
            ["bengaluru", "chennai"],
        )

    def test_scheduler_restart_waits_only_for_remaining_live_interval(self):
        now = datetime.now(timezone.utc)
        session = MagicMock()
        session.__enter__.return_value = session
        session.scalars.return_value.all.return_value = ["chennai"]
        session.execute.return_value.all.return_value = [
            ("chennai", "open_meteo", now - timedelta(minutes=44)),
            (
                "chennai",
                "open_meteo_air_quality",
                now - timedelta(minutes=44),
            ),
        ]

        with patch.object(ingest_schedule, "SessionLocal", return_value=session):
            wait_seconds = ingest_schedule.seconds_until_next_poll(now)

        self.assertAlmostEqual(wait_seconds, 60, delta=0.1)

    def test_http_client_retries_transient_status_only(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = []
        response.getcode.return_value = 202
        transient = HTTPError(
            "https://example.test", 503, "Service unavailable", HTTPMessage(), None
        )
        with patch("backend.integrations.urlopen", side_effect=[transient, response]), patch(
            "backend.integrations.time.sleep"
        ) as sleep:
            payload, status_code = HttpClient(attempts=2).get_json_with_status(
                "https://example.test", source="test"
            )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(status_code, 202)
        sleep.assert_called_once()

    def test_raw_payload_row_preserves_provider_response_status(self):
        row = _raw_payload_row(
            "open_meteo",
            "chennai",
            1,
            {"resource": "forecast", "response_status": 206, "payload": {"ok": True}},
            datetime.now(timezone.utc),
        )
        self.assertEqual(row.response_status, 206)


class IngestionCommandTests(unittest.TestCase):
    def test_90_day_backfill_is_split_into_contiguous_30_day_chunks(self):
        ending_at = datetime(2026, 11, 24, tzinfo=timezone.utc)

        windows = ingest_once._historical_windows(90, ending_at)

        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], ending_at - timedelta(days=90))
        self.assertEqual(windows[-1][1], ending_at)
        self.assertTrue(
            all(end - start == timedelta(days=30) for start, end in windows)
        )
        self.assertTrue(
            all(windows[index][1] == windows[index + 1][0] for index in range(2))
        )

    def test_cli_runs_each_backfill_chunk_for_a_city(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.scalars.return_value.all.return_value = [
            SimpleNamespace(id="chennai")
        ]
        session.get.return_value = SimpleNamespace(active=True)
        expected = {
            "source": "openaq",
            "city_id": "chennai",
            "status": "succeeded",
            "accepted": 1,
        }

        with (
            patch.object(ingest_once, "SessionLocal", return_value=session),
            patch.object(ingest_once, "ingest_source", return_value=expected) as ingest,
            patch(
                "sys.argv",
                [
                    "ingest_once",
                    "--city",
                    "chennai",
                    "--source",
                    "openaq",
                    "--historical-days",
                    "90",
                ],
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(ingest_once.main(), 0)

        self.assertEqual(ingest.call_count, 3)
        self.assertTrue(
            all(
                call.kwargs["history_end"] - call.kwargs["history_start"]
                == timedelta(days=30)
                for call in ingest.call_args_list
            )
        )

    def test_recorded_provider_errors_can_be_nonfatal_for_airflow(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.scalars.return_value.all.return_value = [
            SimpleNamespace(id="chennai")
        ]
        provider_result = [
            {
                "source": "gdelt",
                "city_id": "chennai",
                "status": "failed",
                "error": "Provider unavailable.",
            }
        ]

        with (
            patch.object(ingest_once, "SessionLocal", return_value=session),
            patch.object(ingest_once, "ingest_city", return_value=provider_result),
            patch(
                "sys.argv",
                [
                    "ingest_once",
                    "--city",
                    "chennai",
                    "--allow-provider-errors",
                ],
            ),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(ingest_once.main(), 0)

        with (
            patch.object(ingest_once, "SessionLocal", return_value=session),
            patch.object(ingest_once, "ingest_city", return_value=provider_result),
            patch("sys.argv", ["ingest_once", "--city", "chennai"]),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(ingest_once.main(), 1)


if __name__ == "__main__":
    unittest.main()
