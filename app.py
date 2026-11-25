from __future__ import annotations

import os
from datetime import datetime, timezone

import streamlit as st

from air_quality import (
    AQI_THRESHOLD,
    CITIES,
    DataSourceError,
    aqi_category,
    fetch_open_meteo,
    fetch_openaq,
    indian_aqi,
    is_anomaly,
    read_measurements,
    rolling_average,
    store_measurements,
)


POLL_SECONDS = 300

st.set_page_config(
    page_title="Air quality monitor",
    page_icon="🌫️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; padding-bottom: 2.5rem;}
    [data-testid="stMetric"] {
        background: #f7fafc;
        border: 1px solid #e2e8f0;
        padding: 1rem 1.1rem;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Air quality monitor")
st.caption("Live city readings · OpenAQ + Open-Meteo · refreshes every 5 minutes")

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input(
        "OpenAQ API key",
        value=os.environ.get("OPENAQ_API_KEY", ""),
        type="password",
        help="Get a free key at explore.openaq.org. It is not saved by this app.",
    )
    st.caption("Weather is available without a key. Air readings need an OpenAQ key.")
    st.divider()
    st.caption("AQI uses India's PM2.5/PM10 breakpoints; alert threshold: AQI > 100.")
    st.caption("Local history is limited to 7 days and 1,500 readings per series.")


@st.cache_data(ttl=POLL_SECONDS, show_spinner=False)
def load_snapshot(key: str) -> tuple[list[dict], list[str], str]:
    records: list[dict] = []
    errors: list[str] = []
    for city in CITIES:
        if key.strip():
            try:
                records.extend(fetch_openaq(city, key))
            except DataSourceError as error:
                errors.append(f"{city} · OpenAQ: {error}")
        elif not errors:
            errors.append(f"{city} · OpenAQ: add an API key to load air readings.")
        try:
            records.extend(fetch_open_meteo(city))
        except DataSourceError as error:
            errors.append(f"{city} · Open-Meteo: {error}")
    return records, errors, datetime.now(timezone.utc).isoformat(timespec="seconds")


@st.fragment(run_every=POLL_SECONDS)
def live_dashboard() -> None:
    if st.button("Refresh now", type="secondary"):
        load_snapshot.clear()

    records, errors, fetched_at = load_snapshot(api_key)
    store_measurements(records)
    history = read_measurements(hours=24)

    if errors:
        for error in errors:
            if "add an API key" in error:
                st.info(error)
            else:
                st.warning(error)

    latest_by_city_metric: dict[tuple[str, str], dict] = {}
    history_by_city_metric: dict[tuple[str, str], list[dict]] = {}
    for row in history:
        key = (row["city"], row["metric"])
        history_by_city_metric.setdefault(key, []).append(row)
        current = latest_by_city_metric.get(key)
        if current is None or row["observed_at"] > current["observed_at"]:
            latest_by_city_metric[key] = row

    summaries: dict[str, dict] = {}
    for city in CITIES:
        pm_values: dict[str, float | None] = {}
        for metric in ("PM2.5", "PM10"):
            readings = history_by_city_metric.get((city, metric), [])
            values = [row["value"] for row in readings]
            pm_values[metric] = rolling_average(values)
        summaries[city] = {
            "aqi": indian_aqi(pm_values),
            "pm_values": pm_values,
        }

    breach_count = sum(
        summary["aqi"] is not None and summary["aqi"] > AQI_THRESHOLD
        for summary in summaries.values()
    )
    top = st.columns(3)
    top[0].metric("Cities monitored", len(CITIES))
    top[1].metric("AQI threshold breaches", breach_count)
    checked_at = datetime.fromisoformat(fetched_at).strftime("%Y-%m-%d %H:%M UTC")
    top[2].metric("Last API check", checked_at)

    st.subheader("City snapshot")
    city_columns = st.columns(2)
    for index, city in enumerate(CITIES):
        summary = summaries[city]
        aqi = summary["aqi"]
        with city_columns[index % 2]:
            with st.container(border=True):
                st.markdown(f"### {city}")
                st.metric(
                    "Estimated India AQI",
                    "—" if aqi is None else aqi,
                    aqi_category(aqi),
                )
                pm_cols = st.columns(2)
                for col, metric in zip(pm_cols, ("PM2.5", "PM10")):
                    value = summary["pm_values"][metric]
                    col.metric(
                        f"{metric} · 24h mean",
                        "—" if value is None else f"{value:.1f} µg/m³",
                    )
                if aqi is not None and aqi > AQI_THRESHOLD:
                    st.error(f"AQI is above the {AQI_THRESHOLD} threshold.")

                weather = {
                    metric: row
                    for (row_city, metric), row in latest_by_city_metric.items()
                    if row_city == city
                    and row["source"] == "Open-Meteo"
                }
                weather_cols = st.columns(3)
                for col, metric in zip(
                    weather_cols, ("Temperature", "Humidity", "Wind speed")
                ):
                    row = weather.get(metric)
                    value = "—" if row is None else f"{row['value']:g} {row['unit']}"
                    col.caption(f"**{metric}**  \n{value}")

    st.subheader("24-hour PM2.5 trend")
    trend_rows = []
    for row in history:
        if row["metric"] == "PM2.5":
            timestamp = datetime.fromisoformat(
                row["observed_at"].replace("Z", "+00:00")
            )
            trend_rows.append(
                {"time": timestamp, "city": row["city"], "PM2.5": row["value"]}
            )
    if trend_rows:
        st.line_chart(trend_rows, x="time", y="PM2.5", color="city")
    else:
        st.info("Trend data will appear after the first OpenAQ readings are stored.")

    st.subheader("Anomaly checks")
    alerts = []
    for city in CITIES:
        for metric in ("PM2.5", "PM10"):
            readings = history_by_city_metric.get((city, metric), [])
            if len(readings) >= 7 and is_anomaly(
                [row["value"] for row in readings[:-1]], readings[-1]["value"]
            ):
                alerts.append(
                    {
                        "City": city,
                        "Pollutant": metric,
                        "Latest": f"{readings[-1]['value']:.1f} µg/m³",
                        "Observed": readings[-1]["observed_at"],
                    }
                )
    if alerts:
        st.dataframe(alerts, hide_index=True, use_container_width=True)
    else:
        st.caption("No PM2.5/PM10 spikes detected against the recent 24-hour baseline.")

    st.caption(
        "AQI is an estimate based on available PM rolling means, not an official "
        "government reading. OpenAQ measurements may be delayed or unavailable."
    )
    st.caption("Local history: data/air_quality.sqlite3 (ignored by Git)")


live_dashboard()
