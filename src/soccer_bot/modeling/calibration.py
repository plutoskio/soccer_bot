from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Protocol

import duckdb

from soccer_bot.modeling.walk_forward import (
    WalkForwardConfig,
    block_bootstrap_interval,
    comparison_seed,
)


class CalibrationError(RuntimeError):
    """Raised when a probability calibrator would violate fold isolation."""


class MoneylinePrediction(Protocol):
    model_key: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    fold_key: str
    result: str
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    moneyline_log_loss: float
    moneyline_brier: float


@dataclass(frozen=True)
class MoneylineTemperatureFit:
    model_key: str
    information_state: str
    fit_fold: str
    fixtures: int
    temperature: float
    mean_log_loss_before: float
    mean_log_loss_after: float
    optimum_at_configured_boundary: bool


@dataclass(frozen=True)
class CalibratedMoneylinePrediction:
    model_key: str
    base_model_key: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    fold_key: str
    temperature: float
    result: str
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    moneyline_log_loss: float
    moneyline_brier: float


def read_calibrated_predictions_parquet(
    path: Path,
) -> list[CalibratedMoneylinePrediction]:
    connection = duckdb.connect(":memory:")
    try:
        relation = connection.execute(
            f"""
            SELECT * FROM read_parquet({_sql_literal(path)})
            ORDER BY prediction_at, fixture_id, information_state, model_key
            """
        )
        names = [item[0] for item in relation.description]
        expected = {field.name for field in fields(CalibratedMoneylinePrediction)}
        if set(names) != expected:
            raise CalibrationError("Unexpected calibrated prediction schema")
        string_columns = {
            "model_key",
            "base_model_key",
            "fixture_id",
            "information_state",
            "fold_key",
            "result",
        }
        values = []
        for row in relation.fetchall():
            value = dict(zip(names, row, strict=True))
            for column in string_columns:
                value[column] = str(value[column])
            values.append(CalibratedMoneylinePrediction(**value))
        return values
    finally:
        connection.close()


def fit_and_apply_temperature_calibration(
    predictions: list[MoneylinePrediction],
    config: WalkForwardConfig,
) -> tuple[list[MoneylineTemperatureFit], list[CalibratedMoneylinePrediction]]:
    grouped: dict[tuple[str, str], list[MoneylinePrediction]] = defaultdict(list)
    for row in predictions:
        grouped[(row.model_key, row.information_state)].append(row)
    fits = []
    calibrated = []
    for (model_key, information_state), values in sorted(grouped.items()):
        fit_rows = [row for row in values if row.fold_key == config.calibration_fit_fold]
        apply_rows = [
            row for row in values if row.fold_key == config.calibration_apply_fold
        ]
        if len(fit_rows) < config.calibration_minimum_fixtures:
            raise CalibrationError(
                f"Insufficient calibration rows for {model_key}/{information_state}: "
                f"{len(fit_rows)}"
            )
        if not apply_rows:
            raise CalibrationError(
                f"No apply-fold rows for {model_key}/{information_state}"
            )
        temperature = _fit_temperature(fit_rows, config)
        before = math.fsum(row.moneyline_log_loss for row in fit_rows) / len(fit_rows)
        after = math.fsum(
            _log_loss(_calibrated_probabilities(row, temperature), row.result)
            for row in fit_rows
        ) / len(fit_rows)
        fits.append(
            MoneylineTemperatureFit(
                model_key=model_key,
                information_state=information_state,
                fit_fold=config.calibration_fit_fold,
                fixtures=len(fit_rows),
                temperature=temperature,
                mean_log_loss_before=before,
                mean_log_loss_after=after,
                optimum_at_configured_boundary=(
                    math.isclose(
                        temperature,
                        config.temperature_minimum,
                        abs_tol=10 * config.temperature_optimizer_tolerance,
                    )
                    or math.isclose(
                        temperature,
                        config.temperature_maximum,
                        abs_tol=10 * config.temperature_optimizer_tolerance,
                    )
                ),
            )
        )
        for row in apply_rows:
            probabilities = _calibrated_probabilities(row, temperature)
            calibrated.append(
                CalibratedMoneylinePrediction(
                    model_key=f"{model_key}_temperature_calibrated",
                    base_model_key=model_key,
                    fixture_id=row.fixture_id,
                    information_state=row.information_state,
                    prediction_at=row.prediction_at,
                    kickoff=row.kickoff,
                    fold_key=row.fold_key,
                    temperature=temperature,
                    result=row.result,
                    home_win_probability=probabilities["home_win"],
                    draw_probability=probabilities["draw"],
                    away_win_probability=probabilities["away_win"],
                    moneyline_log_loss=_log_loss(probabilities, row.result),
                    moneyline_brier=math.fsum(
                        (probabilities[key] - float(key == row.result)) ** 2
                        for key in ("home_win", "draw", "away_win")
                    ),
                )
            )
    return fits, sorted(
        calibrated,
        key=lambda row: (
            row.prediction_at,
            row.fixture_id,
            row.information_state,
            row.model_key,
        ),
    )


def summarize_calibration(
    fits: list[MoneylineTemperatureFit],
    calibrated: list[CalibratedMoneylinePrediction],
    baseline_predictions: list[MoneylinePrediction],
    config: WalkForwardConfig,
) -> dict:
    baseline = {
        (row.fixture_id, row.information_state, row.model_key): row
        for row in baseline_predictions
        if row.fold_key == config.calibration_apply_fold
    }
    grouped: dict[
        tuple[str, str], list[CalibratedMoneylinePrediction]
    ] = defaultdict(list)
    for row in calibrated:
        grouped[(row.model_key, row.information_state)].append(row)
    metrics = []
    comparisons = []
    for (model_key, information_state), values in sorted(grouped.items()):
        metrics.append(
            {
                "model_key": model_key,
                "information_state": information_state,
                "fold_key": config.calibration_apply_fold,
                "fixtures": len(values),
                "mean_moneyline_log_loss": math.fsum(
                    row.moneyline_log_loss for row in values
                )
                / len(values),
                "mean_moneyline_brier": math.fsum(
                    row.moneyline_brier for row in values
                )
                / len(values),
                "moneyline_calibration_error": _calibration_error(values),
            }
        )
        base_model_key = values[0].base_model_key
        for metric in ("moneyline_log_loss", "moneyline_brier"):
            blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
            for row in values:
                base = baseline[
                    (row.fixture_id, row.information_state, base_model_key)
                ]
                blocks[(row.kickoff.year, row.kickoff.month)].append(
                    getattr(row, metric) - getattr(base, metric)
                )
            differences = [value for block in blocks.values() for value in block]
            lower, upper, probability = block_bootstrap_interval(
                blocks,
                replicates=config.bootstrap_replicates,
                seed=comparison_seed(
                    config.bootstrap_seed,
                    model_key,
                    information_state,
                    config.calibration_apply_fold,
                    metric,
                ),
            )
            comparisons.append(
                {
                    "challenger_model": model_key,
                    "baseline_model": base_model_key,
                    "information_state": information_state,
                    "fold_key": config.calibration_apply_fold,
                    "metric": metric,
                    "fixtures": len(differences),
                    "calendar_month_blocks": len(blocks),
                    "mean_delta_calibrated_minus_base": math.fsum(differences)
                    / len(differences),
                    "paired_month_block_bootstrap_95_lower": lower,
                    "paired_month_block_bootstrap_95_upper": upper,
                    "bootstrap_probability_calibrated_is_better": probability,
                    "lower_is_better": True,
                }
            )
    return {
        "method": "temperature_scaling",
        "fit_fold": config.calibration_fit_fold,
        "apply_fold": config.calibration_apply_fold,
        "fits": [asdict(fit) for fit in fits],
        "metrics": metrics,
        "paired_model_comparisons": comparisons,
    }


def write_calibrated_predictions_parquet(
    rows: list[CalibratedMoneylinePrediction], path: Path
) -> None:
    if not rows:
        raise CalibrationError("Cannot write empty calibrated predictions")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="calibrated-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(handle.name)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with handle:
            for row in rows:
                value = asdict(row)
                value["prediction_at"] = row.prediction_at.timestamp()
                value["kickoff"] = row.kickoff.timestamp()
                handle.write(
                    json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
                )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (
                        to_timestamp(prediction_at) AS prediction_at,
                        to_timestamp(kickoff) AS kickoff
                    )
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY prediction_at, fixture_id, information_state, model_key
                ) TO {_sql_literal(temporary)} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary, path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _fit_temperature(
    rows: list[MoneylinePrediction], config: WalkForwardConfig
) -> float:
    lower = math.log(config.temperature_minimum)
    upper = math.log(config.temperature_maximum)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = _temperature_objective(rows, math.exp(left))
    right_value = _temperature_objective(rows, math.exp(right))
    while upper - lower > config.temperature_optimizer_tolerance:
        if left_value <= right_value:
            upper = right
            right = left
            right_value = left_value
            left = upper - ratio * (upper - lower)
            left_value = _temperature_objective(rows, math.exp(left))
        else:
            lower = left
            left = right
            left_value = right_value
            right = lower + ratio * (upper - lower)
            right_value = _temperature_objective(rows, math.exp(right))
    return math.exp((lower + upper) / 2.0)


def _temperature_objective(
    rows: list[MoneylinePrediction], temperature: float
) -> float:
    return math.fsum(
        _log_loss(_calibrated_probabilities(row, temperature), row.result)
        for row in rows
    ) / len(rows)


def _calibrated_probabilities(
    row: MoneylinePrediction, temperature: float
) -> dict[str, float]:
    return temperature_scale_probabilities(
        {
            "home_win": row.home_win_probability,
            "draw": row.draw_probability,
            "away_win": row.away_win_probability,
        },
        temperature,
    )


def temperature_scale_probabilities(
    probabilities: dict[str, float], temperature: float
) -> dict[str, float]:
    if temperature <= 0:
        raise CalibrationError("Temperature must be positive")
    if set(probabilities) != {"home_win", "draw", "away_win"}:
        raise CalibrationError("Temperature scaling requires three-way moneyline")
    if any(value <= 0 for value in probabilities.values()):
        raise CalibrationError("Temperature scaling requires positive probabilities")
    logits = {
        key: math.log(value) / temperature
        for key, value in probabilities.items()
    }
    maximum = max(logits.values())
    weights = {key: math.exp(value - maximum) for key, value in logits.items()}
    total = math.fsum(weights.values())
    return {key: value / total for key, value in weights.items()}


def _log_loss(probabilities: dict[str, float], result: str) -> float:
    return -math.log(probabilities[result])


def _calibration_error(
    rows: list[CalibratedMoneylinePrediction], bins: int = 10
) -> float:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for row in rows:
        probabilities = {
            "home_win": row.home_win_probability,
            "draw": row.draw_probability,
            "away_win": row.away_win_probability,
        }
        for result, probability in probabilities.items():
            index = min(int(probability * bins), bins - 1)
            buckets[index].append((probability, float(row.result == result)))
    total = sum(len(bucket) for bucket in buckets)
    return math.fsum(
        len(bucket)
        / total
        * abs(
            math.fsum(item[0] for item in bucket) / len(bucket)
            - math.fsum(item[1] for item in bucket) / len(bucket)
        )
        for bucket in buckets
        if bucket
    )


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"
