# Power BI analyst dashboard

## Open the interactive dashboard

The source-controlled Power BI Desktop project is
[`AeroSensePowerBI.pbip`](./powerbi/AeroSensePowerBI/AeroSensePowerBI.pbip).
Open it in Power BI Desktop, then refresh the semantic model. The report has:

- **Overview:** city and time slicers, mean PM2.5 / city-hour / wind KPIs,
  a city trend line, and a PM2.5-versus-wind scatter plot.
- **Hourly patterns:** city and time slicers plus a weekday-by-hour PM2.5
  matrix. Selecting cities or time periods filters the page visuals.

The project imports `public.vw_pm25_hourly` from PostgreSQL at
`localhost:5432`, database `aerosense`. If the database port was changed,
update the `PostgreSQL.Database` server value in
`AeroSensePowerBI.SemanticModel/definition/tables/vw_pm25_hourly.tmdl`.
Power BI prompts for database credentials on the first refresh; use the
read-only `powerbi_reader` role described below. No credentials or sample
measurements are included in the project.

Regenerate the project after editing its layout or field definitions with:

```powershell
python .\powerbi\build_dashboard.py
```

The generated project is plain text and remains small; it does not include
Power BI Desktop caches or a local copy of the database.

The database migration creates `public.vw_pm25_hourly`, a city-by-hour view
that combines valid OpenAQ station PM2.5 observations with same-hour
Open-Meteo weather context. PM2.5 is averaged across the reporting stations
available in each city-hour. `hour_utc` and `hour_ist` are both included;
hour and weekday fields use India Standard Time.

The view is observational, not causal. A missing weather value means no
matching weather observation was stored for that hour. It is not zero.

## Connect Power BI Desktop

1. Start the core Compose services with `docker compose up -d`.
2. The database is published on `127.0.0.1:5432` by default. Override
   `POSTGRES_PORT` in `.env` if that port is already in use.
3. Create a dedicated read-only login from a trusted local SQL session. Choose
   a unique password locally; do not save it in this repository:

   ```sql
   CREATE ROLE powerbi_reader LOGIN PASSWORD 'replace-with-a-local-secret';
   GRANT CONNECT ON DATABASE aerosense TO powerbi_reader;
   GRANT USAGE ON SCHEMA public TO powerbi_reader;
   GRANT SELECT ON public.vw_pm25_hourly TO powerbi_reader;
   ```

4. In Power BI Desktop, choose **Get data → PostgreSQL database**:
   - Server: `localhost:5432` (or the configured `POSTGRES_PORT`)
   - Database: `aerosense`
   - Data connectivity mode: **Import**
   - Credentials: the `powerbi_reader` login created above
5. Select `public.vw_pm25_hourly`. Refresh after the scheduled ingestion has
   added new observations. The Airflow ingestion DAG currently runs every 15
   minutes; this is more frequent than a daily schedule and keeps the existing
   live-monitor behavior.

The port is bound to loopback only. Do not change it to a public interface to
make Power BI work remotely; use a private network and an appropriately
secured gateway for remote access.

## Measures and model caveats

The project defines these measures:

```DAX
Mean PM2.5 (µg/m³) =
    AVERAGE('vw_pm25_hourly'[pm25_mean_ug_m3])

Observed City-Hours =
    COUNTROWS('vw_pm25_hourly')

Mean Wind Speed (km/h) =
    AVERAGE('vw_pm25_hourly'[wind_speed_kmh])

Mean Stations per City-Hour =
    AVERAGE('vw_pm25_hourly'[pm25_station_count])
```

These are descriptive measures, not regulatory-compliance or causal measures.
Do not treat an individual hourly mean as a 24-hour air-quality standard
exceedance. Write a findings note only after checking date coverage, station
counts, missing weather matches, and the actual filtered values in the report.
