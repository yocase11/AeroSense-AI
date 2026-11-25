import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from air_quality import (
    aqi_category,
    connect_db,
    indian_aqi,
    is_anomaly,
    store_measurements,
)


class AirQualityTests(unittest.TestCase):
    def test_indian_aqi_uses_worst_available_pm_sub_index(self) -> None:
        self.assertEqual(indian_aqi({"PM2.5": 30, "PM10": 50}), 50)
        self.assertEqual(indian_aqi({"PM2.5": 90, "PM10": 40}), 200)
        self.assertIsNone(indian_aqi({"PM2.5": None, "PM10": None}))

    def test_aqi_category_and_threshold_label(self) -> None:
        self.assertEqual(aqi_category(100), "Satisfactory")
        self.assertEqual(aqi_category(101), "Moderate")

    def test_anomaly_detection_needs_enough_history_and_finds_spikes(self) -> None:
        self.assertFalse(is_anomaly([10.0] * 5, 100.0))
        self.assertTrue(is_anomaly([10.0] * 6, 40.0))
        self.assertFalse(is_anomaly([10.0] * 6, 15.0))

    def test_database_deduplicates_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "monitor.sqlite3"
            row = {
                "city": "Chennai",
                "source": "OpenAQ",
                "metric": "PM2.5",
                "value": 20.0,
                "unit": "µg/m³",
                "observed_at": "2026-11-22T10:00:00Z",
                "location": "Station",
            }
            store_measurements([row, row], db_path)
            with closing(connect_db(db_path)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM measurements"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_database_caps_rows_per_city_and_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "monitor.sqlite3"
            now = datetime.now(timezone.utc)
            rows = [
                {
                    "city": "Chennai",
                    "source": "OpenAQ",
                    "metric": "PM2.5",
                    "value": float(index),
                    "unit": "µg/m³",
                    "observed_at": (now + timedelta(minutes=index)).isoformat(),
                    "location": "Station",
                }
                for index in range(3)
            ]
            with patch("air_quality.MAX_ROWS_PER_SERIES", 2):
                store_measurements(rows, db_path)
            with closing(connect_db(db_path)) as connection:
                values = [
                    row[0]
                    for row in connection.execute(
                        "SELECT value FROM measurements ORDER BY observed_at"
                    )
                ]
            self.assertEqual(values, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
