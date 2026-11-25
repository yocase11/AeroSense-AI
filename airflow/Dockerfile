FROM apache/airflow:2.11.1-python3.12@sha256:18130e49360be9e5cbe8bad00f5cda56995f347f4c7c0b5de143c6b746ede16d

USER airflow
COPY requirements-airflow.lock /tmp/requirements-airflow.lock
RUN pip install --no-cache-dir --require-hashes --timeout 180 --retries 10 \
    -r /tmp/requirements-airflow.lock

COPY --chown=airflow:root requirements-prod.lock /opt/airflow/project/requirements-prod.lock
COPY --chown=airflow:root backend /opt/airflow/project/backend
COPY --chown=airflow:root data_engineering /opt/airflow/project/data_engineering
COPY --chown=airflow:root air_quality.py /opt/airflow/project/air_quality.py
RUN python -m venv /opt/airflow/aerosense-venv \
    && mkdir -p /opt/airflow/aerosense-venv/lib/python3.12/site-packages
COPY --from=aerosense-api /usr/local/lib/python3.12/site-packages/ /opt/airflow/aerosense-venv/lib/python3.12/site-packages/
ENV PYTHONPATH=/opt/airflow/project
