FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
ARG AEROSENSE_REQUIREMENTS_LOCK=requirements-prod.lock
COPY requirements-prod.lock requirements-streaming.lock /tmp/locks/
RUN pip install --no-cache-dir --require-hashes --timeout 180 --retries 10 \
    -r "/tmp/locks/${AEROSENSE_REQUIREMENTS_LOCK}"

COPY pyproject.toml ./
COPY backend ./backend
COPY data_engineering ./data_engineering
COPY air_quality.py ./
COPY alembic.ini ./
COPY database ./database
COPY app.py README.md ./

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && python -m backend.seed && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
