import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from backend.database import (
    ApiSource,
    Base,
    City,
    Forecast,
    GeographyPoint,
    ModelEvaluation,
    ModelRegistry,
    MonitoringStation,
    SourceObservation,
)
from backend.ml import forecast_metric
from backend.agents import _facts_from_db


@compiles(GeographyPoint, "sqlite")
def _compile_geography_for_sqlite(type_, compiler, **kwargs):
    del type_, compiler, kwargs
    return "TEXT"


class StationIsolatedForecastTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add_all(
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
                ),
            ]
        )
        self.session.flush()
        self.stations = [
            MonitoringStation(
                city_id="chennai",
                source_id="openaq",
                provider_id=f"station-{index}",
                name=f"Station {index}",
                metadata_json={},
                active=True,
            )
            for index in (1, 2)
        ]
        self.session.add_all(self.stations)
        self.session.flush()
        self.now = datetime.now(timezone.utc)
        for station_index, station in enumerate(self.stations, start=1):
            for sample_index in range(station_index + 1):
                observed_at = self.now - timedelta(hours=sample_index + 1)
                self.session.add(
                    SourceObservation(
                        source_id="openaq",
                        city_id="chennai",
                        station_id=station.id,
                        provider_record_id=f"{station_index}-{sample_index}",
                        metric="PM2.5",
                        value=10.0 + sample_index,
                        original_unit="µg/m³",
                        canonical_unit="µg/m³",
                        observed_at=observed_at,
                        quality_status="valid",
                        metadata_json={},
                    )
                )
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_explicit_station_forecast_history_never_mixes_stations(self):
        result = forecast_metric(
            self.session,
            "chennai",
            "PM2.5",
            station_id=self.stations[0].id,
            now=self.now,
        )

        self.assertEqual(result["station_id"], self.stations[0].id)
        self.assertEqual(result["available_samples"], 2)
        self.assertEqual(result["status"], "insufficient_data")

    def test_automatic_station_choice_uses_one_station_with_most_samples(self):
        result = forecast_metric(
            self.session, "chennai", "PM2.5", now=self.now
        )

        self.assertEqual(result["station_id"], self.stations[1].id)
        self.assertEqual(result["available_samples"], 3)

    def test_training_persists_station_scoped_forecasts_and_evaluation(self):
        self.session.execute(delete(SourceObservation))
        for sample_index in range(60):
            self.session.add(
                SourceObservation(
                    source_id="openaq",
                    city_id="chennai",
                    station_id=self.stations[0].id,
                    provider_record_id=f"training-{sample_index}",
                    metric="PM2.5",
                    value=12.0 + sample_index % 8,
                    original_unit="µg/m³",
                    canonical_unit="µg/m³",
                    observed_at=self.now - timedelta(hours=sample_index + 1),
                    quality_status="valid",
                    metadata_json={},
                )
            )
        self.session.commit()

        result = forecast_metric(
            self.session,
            "chennai",
            "PM2.5",
            station_id=self.stations[0].id,
            now=self.now,
        )
        self.session.commit()

        self.assertTrue(result["available"])
        self.assertEqual(result["training_samples"], 60)
        self.assertEqual(len(result["forecasts"]), 5)
        self.assertIn("persistence_baseline", result["evaluation"])
        self.assertIn("seasonal_naive_baseline", result["evaluation"])
        self.assertGreaterEqual(
            result["evaluation"]["persistence_baseline"]["mae"], 0
        )
        self.assertEqual(
            [item["horizon_hours"] for item in result["horizon_evaluations"]],
            [1, 6, 12, 24, 48],
        )
        self.assertEqual(result["horizon_evaluations"][0]["status"], "evaluated")
        self.assertTrue(all(
            item["status"] == "insufficient_backtest_windows"
            for item in result["horizon_evaluations"][1:]
        ))
        self.assertEqual(len(result["feature_importance"]), 10)
        self.assertTrue(all(item["mean_absolute_shap"] >= 0
                            for item in result["feature_importance"]))
        self.assertEqual(
            {
                row.station_id
                for row in self.session.scalars(select(Forecast)).all()
            },
            {self.stations[0].id},
        )
        evaluation = self.session.scalar(select(ModelEvaluation))
        self.assertEqual(evaluation.station_id, self.stations[0].id)
        self.assertIn(
            "persistence_baseline", evaluation.evaluation_period
        )
        self.assertIsNotNone(self.session.scalar(select(ModelRegistry)))

    def test_long_history_persists_horizon_backtests_when_enough_windows_exist(self):
        self.session.execute(delete(SourceObservation))
        for sample_index in range(300):
            self.session.add(
                SourceObservation(
                    source_id="openaq",
                    city_id="chennai",
                    station_id=self.stations[0].id,
                    provider_record_id=f"long-training-{sample_index}",
                    metric="PM2.5",
                    value=12.0 + sample_index % 8,
                    original_unit="µg/m³",
                    canonical_unit="µg/m³",
                    observed_at=self.now - timedelta(hours=sample_index + 1),
                    quality_status="valid",
                    metadata_json={},
                )
            )
        self.session.commit()

        result = forecast_metric(
            self.session,
            "chennai",
            "PM2.5",
            station_id=self.stations[0].id,
            now=self.now,
        )
        self.session.commit()

        backtest = next(
            item
            for item in result["horizon_evaluations"]
            if item["horizon_hours"] == 48
        )
        self.assertEqual(backtest["status"], "evaluated")
        self.assertGreaterEqual(backtest["evaluation_windows"], 10)
        self.assertGreaterEqual(
            backtest["seasonal_naive_baseline"]["mae"], 0
        )
        self.assertGreaterEqual(
            backtest["persistence_baseline"]["mae"], 0
        )
        persisted = self.session.scalar(
            select(ModelEvaluation).where(ModelEvaluation.horizon_hours == 48)
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(
            persisted.evaluation_period["evaluation_type"],
            "rolling_origin_recursive",
        )
        self.assertEqual(
            persisted.evaluation_period["sample_count"],
            backtest["evaluation_windows"],
        )

    def test_prediction_agent_reads_one_evaluated_station_without_training(self):
        generated_at = self.now - timedelta(minutes=5)
        for station_index, station in enumerate(self.stations, start=1):
            self.session.add(
                Forecast(
                    city_id="chennai",
                    station_id=station.id,
                    metric="PM2.5",
                    model_name="station-model",
                    model_version="1",
                    generated_at=generated_at + timedelta(minutes=station_index),
                    target_at=self.now + timedelta(hours=station_index),
                    value=20.0 + station_index,
                    metadata_json={},
                )
            )
            self.session.add(
                ModelEvaluation(
                    city_id="chennai",
                    station_id=station.id,
                    metric="PM2.5",
                    model_name="station-model",
                    model_version="1",
                    horizon_hours=6,
                    mae=1.0,
                    rmse=1.2,
                    r_squared=0.8,
                    training_period={},
                    evaluation_period={},
                    evaluated_at=self.now,
                )
            )
        self.session.commit()

        result = _facts_from_db(
            self.session,
            "prediction",
            "chennai",
            "What is the PM2.5 forecast?",
        )

        self.assertEqual(result["status"], "forecast_available")
        self.assertEqual(
            {fact["station_id"] for fact in result["facts"]},
            {self.stations[1].id},
        )
        self.assertTrue(all(fact["forecast_id"] for fact in result["facts"]))
        self.assertEqual(len(self.session.new), 0)


if __name__ == "__main__":
    unittest.main()
