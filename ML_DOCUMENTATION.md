# Forecasting and anomaly analysis

## Forecasting

`backend/ml.py` trains a Random Forest regression baseline from persisted,
quality-valid OpenAQ observations for one selected monitoring station and
metric, using a trailing 90-day training window. If no station is specified, it
chooses the station with the most valid records in that window. It never
combines readings across stations or uses modeled Open-Meteo air-quality
products as ground truth. Inputs are the six latest observations plus cyclical
hour-of-day and day-of-week features. Weather is not an input feature.

The chronological 80/20 split reports one-step MAE, RMSE, and R² against
last-observation persistence. The model also recursively emits 1-, 6-, 12-,
24-, and 48-hour forecast records. Each horizon has a rolling-origin
evaluation in the held-out period, comparing the model on the same target
windows against:

- last-observation persistence; and
- a 24-hour seasonal-naive value (previous-day same time, repeated from
  already observed history for horizons beyond one day).

The nearest actual value is accepted only within `max(15 minutes, half the
median observation cadence)`. At most 20 evenly spaced origins are scored.
A horizon result is persisted only when at least 10 common origin/target pairs
are available for the model and both baselines; otherwise the status is
`insufficient_backtest_windows`. Missing scores are not represented as zero.

Mean absolute TreeSHAP values are calculated for the model's lag and cyclical
time features using up to 100 held-out feature rows, then exposed with forecast
results. These values describe feature contribution to this fitted model; they
are not causal explanations and cannot show weather influence because weather
is not currently in the model.

The API reports `insufficient_data` unless there are at least 48 observations
and a usable median cadence; it does not create placeholder predictions or
fake scores. Horizon scores describe this station and historical window only.
They are not a guarantee of future performance and should not be used for
alerts or public-health recommendations without broader backtests, uncertainty
calibration, and ongoing monitoring.

## Anomalies

The ingestion path applies a transparent median/MAD spike rule to the prior
station readings for PM2.5/PM10. The rule requires at least six historical
baseline values, uses a 3× scaled MAD threshold with a minimum 10-unit margin,
and stores a station/metric/date-deduplicated alert. It is an alert heuristic,
not a causal diagnosis.

The dashboard distinguishes no stored anomaly records from the absence of
risk. The detector requires a historical baseline, so sparse or irregular
station history can leave it unable to evaluate a signal. Isolation Forest and
multi-variable model-based anomaly classification are not enabled in this
phase; they require a sufficiently large, quality-controlled feature set and
a separately evaluated threshold.

## Model lifecycle gaps

Model versions and evaluation records are persisted. Production deployment still needs scheduled retraining policy, model artifact
registry, drift monitoring, confidence calibration, and more extensive
station/time-period backtests. Do not present prototype evaluation metrics as
general model performance.
