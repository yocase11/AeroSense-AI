"""Add an hourly city PM2.5 and weather view for Power BI."""

from alembic import op


revision = "0003_pm25_hourly_view"
down_revision = "0002_authentication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW vw_pm25_hourly AS
        WITH pm25_hourly AS (
            SELECT
                city_id,
                date_trunc('hour', observed_at) AS hour_utc,
                AVG(value) AS pm25_mean_ug_m3,
                COUNT(*) AS pm25_measurement_count,
                COUNT(DISTINCT station_id) AS pm25_station_count
            FROM source_observations
            WHERE source_id = 'openaq'
              AND metric = 'PM2.5'
              AND canonical_unit = 'µg/m³'
              AND quality_status = 'valid'
              AND station_id IS NOT NULL
            GROUP BY city_id, date_trunc('hour', observed_at)
        ),
        weather_hourly AS (
            SELECT
                city_id,
                date_trunc('hour', observed_at) AS hour_utc,
                AVG(value) FILTER (WHERE metric = 'Temperature') AS temperature_c,
                AVG(value) FILTER (WHERE metric = 'Wind speed') AS wind_speed_kmh,
                AVG(value) FILTER (WHERE metric = 'Humidity') AS humidity_pct,
                AVG(value) FILTER (WHERE metric = 'Precipitation') AS precipitation_mm
            FROM source_observations
            WHERE source_id = 'open_meteo'
              AND quality_status = 'valid'
              AND metric IN ('Temperature', 'Wind speed', 'Humidity', 'Precipitation')
            GROUP BY city_id, date_trunc('hour', observed_at)
        ),
        local_hours AS (
            SELECT
                pm25_hourly.*,
                pm25_hourly.hour_utc AT TIME ZONE 'Asia/Kolkata' AS hour_ist
            FROM pm25_hourly
        )
        SELECT
            local_hours.city_id,
            cities.name AS city_name,
            local_hours.hour_utc,
            local_hours.hour_ist,
            EXTRACT(HOUR FROM local_hours.hour_ist)::integer AS local_hour,
            EXTRACT(ISODOW FROM local_hours.hour_ist)::integer AS local_weekday_num,
            TRIM(TO_CHAR(local_hours.hour_ist, 'Day')) AS local_weekday,
            local_hours.pm25_mean_ug_m3,
            local_hours.pm25_measurement_count,
            local_hours.pm25_station_count,
            weather_hourly.temperature_c,
            weather_hourly.wind_speed_kmh,
            weather_hourly.humidity_pct,
            weather_hourly.precipitation_mm
        FROM local_hours
        JOIN cities ON cities.id = local_hours.city_id
        LEFT JOIN weather_hourly
          ON weather_hourly.city_id = local_hours.city_id
         AND weather_hourly.hour_utc = local_hours.hour_utc
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_pm25_hourly")
