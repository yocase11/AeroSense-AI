from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.database import (
    Forecast,
    ModelEvaluation,
    ModelRegistry,
    SourceObservation,
)


MODEL_NAME = "RandomForest lag baseline"
MODEL_VERSION = "rf-lags-v3"
FORECAST_HORIZONS_HOURS = (1, 6, 12, 24, 48)
FORECAST_TRAINING_WINDOW_DAYS = 90
SEASONAL_PERIOD_HOURS = 24
MINIMUM_TRAINING_SAMPLES = 48
MINIMUM_BACKTEST_ORIGINS = 10
MAXIMUM_BACKTEST_ORIGINS = 20


def detect_spike(values: list[float], latest: float) -> dict[str, Any]:
    if len(values) < 6:
        return {
            "detected": False,
            "status": "insufficient_history",
            "baseline_count": len(values),
        }
    center = statistics.median(values)
    mad = statistics.median(abs(value - center) for value in values)
    threshold = center + max(10.0, 3 * 1.4826 * mad)
    return {
        "detected": latest > threshold,
        "status": "evaluated",
        "median": center,
        "mad": mad,
        "threshold": threshold,
        "latest": latest,
        "baseline_count": len(values),
        "method": "median_absolute_deviation",
    }


def _features(
    lag_values: list[float], timestamp: datetime
) -> list[float]:
    hour = timestamp.hour + timestamp.minute / 60
    weekday = timestamp.weekday()
    return [
        *lag_values[-6:][::-1],
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
        math.sin(2 * math.pi * weekday / 7),
        math.cos(2 * math.pi * weekday / 7),
    ]


def _rmse(actual: list[float], predicted: list[float]) -> float:
    return math.sqrt(
        sum((truth - estimate) ** 2 for truth, estimate in zip(actual, predicted))
        / len(actual)
    )


def _regression_metrics(
    actual: list[float], predicted: list[float]
) -> dict[str, float | None]:
    mae = sum(
        abs(truth - estimate) for truth, estimate in zip(actual, predicted)
    ) / len(actual)
    rmse = _rmse(actual, predicted)
    mean_actual = statistics.fmean(actual)
    total_variance = sum((value - mean_actual) ** 2 for value in actual)
    residual_sum = sum(
        (truth - estimate) ** 2 for truth, estimate in zip(actual, predicted)
    )
    r_squared = 1 - residual_sum / total_variance if total_variance > 0 else None
    return {"mae": mae, "rmse": rmse, "r_squared": r_squared}


def _seasonal_naive_value(
    points: list[tuple[datetime, float]],
    origin_index: int,
    target_time: datetime,
    cadence_seconds: float,
) -> float | None:
    tolerance_seconds = max(15 * 60, cadence_seconds / 2)
    reference_time = target_time - timedelta(hours=SEASONAL_PERIOD_HOURS)
    origin_time = points[origin_index][0]
    while reference_time > origin_time + timedelta(seconds=tolerance_seconds):
        reference_time -= timedelta(hours=SEASONAL_PERIOD_HOURS)
    reference_index = min(
        range(origin_index + 1),
        key=lambda index: abs((points[index][0] - reference_time).total_seconds()),
    )
    if (
        abs((points[reference_index][0] - reference_time).total_seconds())
        > tolerance_seconds
    ):
        return None
    return points[reference_index][1]


def _backtest_horizon(
    model: Any,
    points: list[tuple[datetime, float]],
    first_origin: int,
    cadence_seconds: float,
    horizon_hours: int,
) -> dict[str, Any]:
    horizon_seconds = horizon_hours * 3600
    tolerance_seconds = max(15 * 60, cadence_seconds / 2)
    candidates: list[tuple[int, int]] = []
    for origin_index in range(first_origin, len(points) - 1):
        target_time = points[origin_index][0] + timedelta(seconds=horizon_seconds)
        if target_time > points[-1][0] + timedelta(seconds=tolerance_seconds):
            break
        target_index = min(
            range(origin_index + 1, len(points)),
            key=lambda index: abs((points[index][0] - target_time).total_seconds()),
        )
        elapsed = (points[target_index][0] - points[origin_index][0]).total_seconds()
        if abs(elapsed - horizon_seconds) <= tolerance_seconds:
            candidates.append((origin_index, target_index))

    available_windows = len(candidates)
    if available_windows > MAXIMUM_BACKTEST_ORIGINS:
        selected_indices = [
            round(index * (available_windows - 1) / (MAXIMUM_BACKTEST_ORIGINS - 1))
            for index in range(MAXIMUM_BACKTEST_ORIGINS)
        ]
        candidates = [candidates[index] for index in selected_indices]

    steps = max(1, round(horizon_seconds / cadence_seconds))
    actual: list[float] = []
    predictions: list[float] = []
    persistence: list[float] = []
    seasonal_naive: list[float] = []
    evaluated_targets: list[datetime] = []
    for origin_index, target_index in candidates:
        target_time = points[origin_index][0] + timedelta(seconds=horizon_seconds)
        seasonal_value = _seasonal_naive_value(
            points, origin_index, target_time, cadence_seconds
        )
        if seasonal_value is None:
            continue
        lag_values = [value for _, value in points[origin_index - 5 : origin_index + 1]]
        prediction = 0.0
        for step_index in range(1, steps + 1):
            target_time = points[origin_index][0] + timedelta(
                seconds=cadence_seconds * step_index
            )
            prediction = max(
                0.0,
                float(model.predict([_features(lag_values, target_time)])[0]),
            )
            lag_values.append(prediction)
        actual.append(points[target_index][1])
        predictions.append(prediction)
        persistence.append(points[origin_index][1])
        seasonal_naive.append(seasonal_value)
        evaluated_targets.append(points[target_index][0])

    result: dict[str, Any] = {
        "horizon_hours": horizon_hours,
        "status": (
            "evaluated"
            if len(actual) >= MINIMUM_BACKTEST_ORIGINS
            else "insufficient_backtest_windows"
        ),
        "evaluation_windows": len(actual),
        "available_windows": available_windows,
        "seasonal_available_windows": len(actual),
        "minimum_windows": MINIMUM_BACKTEST_ORIGINS,
        "target_tolerance_seconds": tolerance_seconds,
    }
    if len(actual) < MINIMUM_BACKTEST_ORIGINS:
        return result

    model_metrics = _regression_metrics(actual, predictions)
    persistence_metrics = _regression_metrics(actual, persistence)
    seasonal_metrics = _regression_metrics(actual, seasonal_naive)
    baseline_mae = persistence_metrics["mae"]
    seasonal_mae = seasonal_metrics["mae"]
    model_mae = model_metrics["mae"]
    result.update(model_metrics)
    result["persistence_baseline"] = persistence_metrics
    result["seasonal_naive_baseline"] = {
        "name": "previous_day_same_time",
        **seasonal_metrics,
    }
    result["mae_improvement_percent"] = (
        (baseline_mae - model_mae) / baseline_mae * 100
        if baseline_mae is not None and baseline_mae > 0 and model_mae is not None
        else None
    )
    result["mae_improvement_vs_seasonal_naive_percent"] = (
        (seasonal_mae - model_mae) / seasonal_mae * 100
        if seasonal_mae is not None and seasonal_mae > 0 and model_mae is not None
        else None
    )
    result["evaluation_start"] = evaluated_targets[0].isoformat()
    result["evaluation_end"] = evaluated_targets[-1].isoformat()
    return result


def forecast_metric(
    session: Session,
    city_id: str,
    metric: str,
    station_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    station_query = (
        select(
            SourceObservation.station_id,
            func.count(SourceObservation.id).label("sample_count"),
        )
        .where(
            SourceObservation.city_id == city_id,
            SourceObservation.metric == metric,
            SourceObservation.source_id == "openaq",
            SourceObservation.station_id.is_not(None),
            SourceObservation.quality_status == "valid",
            SourceObservation.observed_at
            >= now - timedelta(days=FORECAST_TRAINING_WINDOW_DAYS),
            SourceObservation.observed_at <= now,
        )
        .group_by(SourceObservation.station_id)
        .order_by(desc("sample_count"), SourceObservation.station_id)
    )
    if station_id is None:
        station_id = session.execute(station_query.limit(1)).scalar()
    if station_id is None:
        return {
            "available": False,
            "status": "no_station_level_ground_observations",
            "required_samples": MINIMUM_TRAINING_SAMPLES,
            "available_samples": 0,
            "city_id": city_id,
            "metric": metric,
            "station_id": None,
        }
    rows = session.scalars(
        select(SourceObservation)
        .where(
            SourceObservation.city_id == city_id,
            SourceObservation.metric == metric,
            SourceObservation.source_id == "openaq",
            SourceObservation.station_id == station_id,
            SourceObservation.quality_status == "valid",
            SourceObservation.observed_at
            >= now - timedelta(days=FORECAST_TRAINING_WINDOW_DAYS),
            SourceObservation.observed_at <= now,
        )
        .order_by(SourceObservation.observed_at)
    ).all()
    points = [(row.observed_at, float(row.value)) for row in rows]
    if len(points) < MINIMUM_TRAINING_SAMPLES:
        return {
            "available": False,
            "status": "insufficient_data",
            "required_samples": MINIMUM_TRAINING_SAMPLES,
            "available_samples": len(points),
            "city_id": city_id,
            "metric": metric,
            "station_id": station_id,
        }
    intervals = [
        (right[0] - left[0]).total_seconds()
        for left, right in zip(points, points[1:])
    ]
    cadence = statistics.median(intervals)
    if cadence <= 0 or cadence > 6 * 3600:
        return {
            "available": False,
            "status": "irregular_or_stale_history",
            "available_samples": len(points),
            "city_id": city_id,
            "metric": metric,
            "station_id": station_id,
        }

    try:
        from sklearn.ensemble import RandomForestRegressor
    except ImportError as error:
        raise RuntimeError(
            "Forecasting requires scikit-learn; install the project requirements."
        ) from error

    features = [
        _features([value for _, value in points[index - 6 : index]], points[index][0])
        for index in range(6, len(points))
    ]
    targets = [value for _, value in points[6:]]
    split = max(1, int(len(features) * 0.8))
    if split < 24 or len(features) - split < 6:
        return {
            "available": False,
            "status": "insufficient_evaluation_window",
            "available_samples": len(points),
            "city_id": city_id,
            "metric": metric,
            "station_id": station_id,
        }
    model = RandomForestRegressor(
        n_estimators=100,
        min_samples_leaf=2,
        random_state=42,
        n_jobs=1,
    )
    model.fit(features[:split], targets[:split])
    evaluation_predictions = [
        float(value) for value in model.predict(features[split:])
    ]
    actual = targets[split:]
    one_step_metrics = _regression_metrics(actual, evaluation_predictions)
    mae = one_step_metrics["mae"]
    rmse = one_step_metrics["rmse"]
    r_squared = one_step_metrics["r_squared"]
    persistence_predictions = [row[0] for row in features[split:]]
    persistence_baseline = {
        "name": "last_observation_persistence",
        **_regression_metrics(actual, persistence_predictions),
    }
    seasonal_actual = []
    seasonal_predictions = []
    for target_index in range(split + 6, len(points)):
        seasonal_value = _seasonal_naive_value(
            points,
            target_index - 1,
            points[target_index][0],
            cadence,
        )
        if seasonal_value is not None:
            seasonal_actual.append(points[target_index][1])
            seasonal_predictions.append(seasonal_value)
    seasonal_naive_baseline = {
        "name": "previous_day_same_time",
        "status": (
            "evaluated"
            if len(seasonal_actual) >= MINIMUM_BACKTEST_ORIGINS
            else "insufficient_backtest_windows"
        ),
        "sample_count": len(seasonal_actual),
    }
    if len(seasonal_actual) >= MINIMUM_BACKTEST_ORIGINS:
        seasonal_naive_baseline.update(
            _regression_metrics(seasonal_actual, seasonal_predictions)
        )

    training_points = points[: split + 6]
    horizon_evaluations = [
        _backtest_horizon(
            model,
            points,
            split + 5,
            cadence,
            horizon,
        )
        for horizon in FORECAST_HORIZONS_HOURS
    ]
    model.fit(features, targets)
    try:
        import numpy as np
        import shap
    except ImportError as error:
        raise RuntimeError(
            "Forecast explanations require SHAP; install the project requirements."
        ) from error
    explanation_features = np.asarray(features[split : split + 100], dtype=float)
    shap_values = np.asarray(
        shap.TreeExplainer(model).shap_values(
            explanation_features, check_additivity=False
        )
    )
    if shap_values.ndim == 3 and shap_values.shape[-1] == 1:
        shap_values = shap_values[..., 0]
    if shap_values.ndim != 2 or shap_values.shape[1] != len(explanation_features[0]):
        raise RuntimeError("SHAP returned an unexpected feature-importance shape.")
    feature_names = [
        "lag_1_latest",
        "lag_2",
        "lag_3",
        "lag_4",
        "lag_5",
        "lag_6_oldest",
        "hour_sin",
        "hour_cos",
        "weekday_sin",
        "weekday_cos",
    ]
    feature_importance = [
        {
            "feature": feature_name,
            "mean_absolute_shap": float(value),
        }
        for feature_name, value in zip(
            feature_names, np.mean(np.abs(shap_values), axis=0)
        )
    ]
    feature_importance.sort(
        key=lambda item: item["mean_absolute_shap"], reverse=True
    )
    generated_at = datetime.now(timezone.utc)
    if session.scalar(
        select(ModelRegistry.id).where(
            ModelRegistry.model_name == MODEL_NAME,
            ModelRegistry.model_version == MODEL_VERSION,
        )
    ) is None:
        session.add(
            ModelRegistry(
                model_name=MODEL_NAME,
                model_version=MODEL_VERSION,
                status="active",
                created_at=generated_at,
                metadata_json={
                    "features": "six lags plus cyclical hour/weekday",
                    "minimum_samples": MINIMUM_TRAINING_SAMPLES,
                    "training_window_days": FORECAST_TRAINING_WINDOW_DAYS,
                    "explainability": "mean absolute TreeSHAP on up to 100 holdout samples",
                },
            )
        )
    lag_values = [value for _, value in points[-6:]]
    latest_time = points[-1][0]
    step = timedelta(seconds=cadence)
    forecasts = []
    target_values: dict[int, tuple[datetime, float]] = {}
    max_steps = max(round(hours * 3600 / cadence) for hours in FORECAST_HORIZONS_HOURS)
    for step_index in range(1, max_steps + 1):
        target_time = latest_time + step * step_index
        prediction = max(0.0, float(model.predict([_features(lag_values, target_time)])[0]))
        lag_values.append(prediction)
        for horizon in FORECAST_HORIZONS_HOURS:
            if step_index >= round(horizon * 3600 / cadence) and horizon not in target_values:
                target_values[horizon] = (target_time, prediction)
    for horizon, (target_at, value) in target_values.items():
        session.add(
            Forecast(
                city_id=city_id,
                station_id=station_id,
                metric=metric,
                model_name=MODEL_NAME,
                model_version=MODEL_VERSION,
                generated_at=generated_at,
                target_at=target_at,
                value=value,
                training_period_start=points[0][0],
                training_period_end=points[-1][0],
                metadata_json={
                    "horizon_hours": horizon,
                    "samples": len(points),
                    "horizon_evaluation": next(
                        item
                        for item in horizon_evaluations
                        if item["horizon_hours"] == horizon
                    ),
                    "feature_importance": feature_importance,
                },
            )
        )
        forecasts.append(
            {
                "horizon_hours": horizon,
                "target_at": target_at.isoformat(),
                "value": value,
            }
        )
    session.add(
        ModelEvaluation(
            city_id=city_id,
            station_id=station_id,
            metric=metric,
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            horizon_hours=1,
            mae=mae,
            rmse=rmse,
            r_squared=r_squared,
            training_period={
                "start": training_points[0][0].isoformat(),
                "end": training_points[-1][0].isoformat(),
            },
            evaluation_period={
                "evaluation_type": "one_step_holdout",
                "sample_count": len(actual),
                "start": points[split + 6][0].isoformat(),
                "end": points[-1][0].isoformat(),
                "persistence_baseline": persistence_baseline,
                "seasonal_naive_baseline": seasonal_naive_baseline,
            },
            evaluated_at=generated_at,
        )
    )
    for evaluation in horizon_evaluations:
        if evaluation["status"] != "evaluated":
            continue
        session.add(
            ModelEvaluation(
                city_id=city_id,
                station_id=station_id,
                metric=metric,
                model_name=MODEL_NAME,
                model_version=MODEL_VERSION,
                horizon_hours=evaluation["horizon_hours"],
                mae=evaluation["mae"],
                rmse=evaluation["rmse"],
                r_squared=evaluation["r_squared"],
                training_period={
                    "start": training_points[0][0].isoformat(),
                    "end": training_points[-1][0].isoformat(),
                },
                evaluation_period={
                    "evaluation_type": "rolling_origin_recursive",
                    "sample_count": evaluation["evaluation_windows"],
                    "start": evaluation["evaluation_start"],
                    "end": evaluation["evaluation_end"],
                    "target_tolerance_seconds": evaluation[
                        "target_tolerance_seconds"
                    ],
                    "persistence_baseline": evaluation[
                        "persistence_baseline"
                    ],
                    "seasonal_naive_baseline": evaluation[
                        "seasonal_naive_baseline"
                    ],
                    "mae_improvement_percent": evaluation[
                        "mae_improvement_percent"
                    ],
                    "mae_improvement_vs_seasonal_naive_percent": evaluation[
                        "mae_improvement_vs_seasonal_naive_percent"
                    ],
                },
                evaluated_at=generated_at,
            )
        )
    return {
        "available": True,
        "status": "trained",
        "city_id": city_id,
        "metric": metric,
        "station_id": station_id,
        "model": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "training_samples": len(points),
        "cadence_seconds": cadence,
        "feature_importance": feature_importance,
        "evaluation": {
            **one_step_metrics,
            "persistence_baseline": persistence_baseline,
            "evaluation_type": "one_step_holdout",
            "sample_count": len(actual),
        },
        "horizon_evaluations": horizon_evaluations,
        "forecasts": forecasts,
    }
