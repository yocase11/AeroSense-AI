from __future__ import annotations

from datetime import datetime, timedelta, timezone

from airflow.decorators import dag, task
from airflow.operators.bash import BashOperator


@dag(
    dag_id="aerosense_environmental_ingestion",
    description="Ingest configured real environmental provider data into PostgreSQL.",
    schedule=timedelta(minutes=45),
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=2)},
    tags=["aerosense", "environmental-data"],
)
def environmental_ingestion():
    seed_references = BashOperator(
        task_id="seed_reference_data",
        bash_command=(
            "cd /opt/airflow/project && "
            "/opt/airflow/aerosense-venv/bin/python -m backend.seed"
        ),
    )
    ingest_configured_providers = BashOperator(
        task_id="ingest_configured_providers",
        bash_command=(
            "cd /opt/airflow/project && "
            "/opt/airflow/aerosense-venv/bin/python -m "
            "data_engineering.ingest_once --allow-provider-errors"
        ),
    )
    seed_references >> ingest_configured_providers


environmental_ingestion()
