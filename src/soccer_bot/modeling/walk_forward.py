from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import random

from soccer_bot.config import load_json
from soccer_bot.datasets.features import RegulationFeatureRow


class WalkForwardConfigurationError(ValueError):
    """Raised when a chronological evaluation definition is unsafe."""


@dataclass(frozen=True)
class EvaluationFold:
    fold_key: str
    kickoff_end_exclusive: datetime | None


@dataclass(frozen=True)
class WalkForwardConfig:
    evaluation_version: str
    result_availability_delay_minutes: int
    minimum_training_fixtures: int
    probability_floor: float
    poisson_tail_tolerance: float
    minimum_expected_goals: float
    maximum_expected_goals: float
    scale_prior_observed_goals: float
    scale_prior_expected_goals: float
    rho_prior_variance: float
    rho_hard_minimum: float
    rho_hard_maximum: float
    bootstrap_replicates: int
    bootstrap_seed: int
    calibration_fit_fold: str
    calibration_apply_fold: str
    calibration_minimum_fixtures: int
    temperature_minimum: float
    temperature_maximum: float
    temperature_optimizer_tolerance: float
    folds: tuple[EvaluationFold, ...]


@dataclass(frozen=True)
class WalkForwardPrediction:
    evaluation_version: str
    model_key: str
    feature_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    fold_key: str
    training_fixtures: int
    home_goals: int
    away_goals: int
    result: str
    expected_home_goals: float
    expected_away_goals: float
    home_rate_scale: float
    away_rate_scale: float
    dixon_coles_rho: float
    exact_score_probability: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    exact_score_log_loss: float
    moneyline_log_loss: float
    moneyline_brier: float


@dataclass(frozen=True)
class ScoreRateScaleFit:
    information_state: str
    training_fixtures: int
    home_observed_goals: float
    home_expected_goals: float
    away_observed_goals: float
    away_expected_goals: float
    home_rate_scale: float
    away_rate_scale: float


@dataclass(frozen=True)
class _Forecast:
    row: RegulationFeatureRow
    home_rate: float
    away_rate: float
    home_scale: float
    away_scale: float
    rho: float
    training_fixtures: int


def load_walk_forward_config(path: Path) -> WalkForwardConfig:
    raw = load_json(path)
    rate = raw.get("rate_model", {})
    dixon_coles = raw.get("dixon_coles", {})
    uncertainty = raw.get("uncertainty", {})
    calibration = raw.get("moneyline_calibration", {})
    if uncertainty.get("paired_block_unit") != "calendar_month":
        raise WalkForwardConfigurationError(
            "Only calendar_month paired blocks are supported"
        )
    if calibration.get("method") != "temperature_scaling":
        raise WalkForwardConfigurationError(
            "Only temperature_scaling moneyline calibration is supported"
        )
    folds = tuple(
        EvaluationFold(
            fold_key=str(item.get("fold_key", "")),
            kickoff_end_exclusive=(
                None
                if item.get("kickoff_end_exclusive") is None
                else datetime.fromisoformat(item["kickoff_end_exclusive"])
            ),
        )
        for item in raw.get("folds", [])
    )
    config = WalkForwardConfig(
        evaluation_version=str(raw.get("evaluation_version", "")),
        result_availability_delay_minutes=int(
            raw["result_availability_delay_minutes"]
        ),
        minimum_training_fixtures=int(raw["minimum_training_fixtures"]),
        probability_floor=float(raw["probability_floor"]),
        poisson_tail_tolerance=float(raw["poisson_tail_tolerance"]),
        minimum_expected_goals=float(rate["minimum_expected_goals"]),
        maximum_expected_goals=float(rate["maximum_expected_goals"]),
        scale_prior_observed_goals=float(rate["scale_prior_observed_goals"]),
        scale_prior_expected_goals=float(rate["scale_prior_expected_goals"]),
        rho_prior_variance=float(
            dixon_coles["rho_prior_standard_deviation"]
        )
        ** 2,
        rho_hard_minimum=float(dixon_coles["rho_hard_minimum"]),
        rho_hard_maximum=float(dixon_coles["rho_hard_maximum"]),
        bootstrap_replicates=int(uncertainty["bootstrap_replicates"]),
        bootstrap_seed=int(uncertainty["bootstrap_seed"]),
        calibration_fit_fold=str(calibration["fit_fold"]),
        calibration_apply_fold=str(calibration["apply_fold"]),
        calibration_minimum_fixtures=int(calibration["minimum_fit_fixtures"]),
        temperature_minimum=float(calibration["temperature_minimum"]),
        temperature_maximum=float(calibration["temperature_maximum"]),
        temperature_optimizer_tolerance=float(
            calibration["optimizer_tolerance"]
        ),
        folds=folds,
    )
    _validate_config(config)
    return config


class _OnlineScoreEstimator:
    def __init__(self, config: WalkForwardConfig) -> None:
        self.config = config
        self.training_fixtures = 0
        self.home_observed = config.scale_prior_observed_goals
        self.home_expected = config.scale_prior_expected_goals
        self.away_observed = config.scale_prior_observed_goals
        self.away_expected = config.scale_prior_expected_goals
        self.rho = 0.0
        self.rho_variance = config.rho_prior_variance

    def forecast(self, row: RegulationFeatureRow) -> _Forecast:
        home_scale = self.home_observed / self.home_expected
        away_scale = self.away_observed / self.away_expected
        home_rate = _clamp(
            row.expected_home_goals * home_scale,
            self.config.minimum_expected_goals,
            self.config.maximum_expected_goals,
        )
        away_rate = _clamp(
            row.expected_away_goals * away_scale,
            self.config.minimum_expected_goals,
            self.config.maximum_expected_goals,
        )
        rho = _valid_rho(self.rho, home_rate, away_rate, self.config)
        return _Forecast(
            row=row,
            home_rate=home_rate,
            away_rate=away_rate,
            home_scale=home_scale,
            away_scale=away_scale,
            rho=rho,
            training_fixtures=self.training_fixtures,
        )

    def observe_batch(self, forecasts: list[_Forecast]) -> None:
        if not forecasts:
            return
        gradient = 0.0
        information = 0.0
        for forecast in forecasts:
            row = forecast.row
            self.home_observed += row.home_goals
            self.home_expected += row.expected_home_goals
            self.away_observed += row.away_goals
            self.away_expected += row.expected_away_goals
            coefficient = _rho_coefficient(
                forecast.home_rate,
                forecast.away_rate,
                row.home_goals,
                row.away_goals,
            )
            if coefficient is not None:
                rho_at_update = _valid_rho(
                    self.rho,
                    forecast.home_rate,
                    forecast.away_rate,
                    self.config,
                )
                denominator = 1.0 + coefficient * rho_at_update
                gradient += coefficient / denominator
                information += (coefficient / denominator) ** 2
        if information:
            posterior_variance = 1.0 / (
                1.0 / self.rho_variance + information
            )
            self.rho = _clamp(
                self.rho + posterior_variance * gradient,
                self.config.rho_hard_minimum,
                self.config.rho_hard_maximum,
            )
            self.rho_variance = posterior_variance
        self.training_fixtures += len(forecasts)


def evaluate_walk_forward(
    rows: list[RegulationFeatureRow],
    config: WalkForwardConfig,
) -> list[WalkForwardPrediction]:
    """Run expanding-window prequential evaluation independently per horizon."""

    grouped: dict[str, list[RegulationFeatureRow]] = defaultdict(list)
    for row in rows:
        grouped[row.information_state].append(row)
    predictions = []
    for information_state in sorted(grouped):
        predictions.extend(_evaluate_horizon(grouped[information_state], config))
    return sorted(
        predictions,
        key=lambda row: (
            row.prediction_at,
            row.fixture_id,
            row.information_state,
            row.model_key,
        ),
    )


def fit_score_rate_scales(
    rows: list[RegulationFeatureRow], config: WalkForwardConfig
) -> list[ScoreRateScaleFit]:
    """Refit the evaluated global rate-scale recipe on all supplied history."""

    grouped: dict[str, list[RegulationFeatureRow]] = defaultdict(list)
    for row in rows:
        grouped[row.information_state].append(row)
    fits = []
    for information_state, values in sorted(grouped.items()):
        home_observed = config.scale_prior_observed_goals + math.fsum(
            row.home_goals for row in values
        )
        home_expected = config.scale_prior_expected_goals + math.fsum(
            row.expected_home_goals for row in values
        )
        away_observed = config.scale_prior_observed_goals + math.fsum(
            row.away_goals for row in values
        )
        away_expected = config.scale_prior_expected_goals + math.fsum(
            row.expected_away_goals for row in values
        )
        fits.append(
            ScoreRateScaleFit(
                information_state=information_state,
                training_fixtures=len(values),
                home_observed_goals=home_observed,
                home_expected_goals=home_expected,
                away_observed_goals=away_observed,
                away_expected_goals=away_expected,
                home_rate_scale=home_observed / home_expected,
                away_rate_scale=away_observed / away_expected,
            )
        )
    return fits


def _evaluate_horizon(
    rows: list[RegulationFeatureRow],
    config: WalkForwardConfig,
) -> list[WalkForwardPrediction]:
    ordered = sorted(rows, key=lambda row: (row.kickoff, row.fixture_id))
    fixture_ids = [row.fixture_id for row in ordered]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise WalkForwardConfigurationError(
            "Each horizon must contain at most one row per fixture"
        )

    prediction_events: dict[datetime, list[RegulationFeatureRow]] = defaultdict(list)
    result_events: dict[datetime, list[str]] = defaultdict(list)
    for row in ordered:
        if row.prediction_at >= row.kickoff:
            raise WalkForwardConfigurationError(
                f"Prediction is not pre-match for fixture {row.fixture_id}"
            )
        prediction_events[row.prediction_at].append(row)
        result_events[
            row.kickoff
            + timedelta(minutes=config.result_availability_delay_minutes)
        ].append(row.fixture_id)

    estimator = _OnlineScoreEstimator(config)
    pending: dict[str, _Forecast] = {}
    predictions = []
    for timestamp in sorted(set(prediction_events) | set(result_events)):
        for row in sorted(
            prediction_events.get(timestamp, []), key=lambda item: item.fixture_id
        ):
            forecast = estimator.forecast(row)
            pending[row.fixture_id] = forecast
            if forecast.training_fixtures >= config.minimum_training_fixtures:
                predictions.extend(_score_forecast(forecast, config))
        available = []
        for fixture_id in sorted(result_events.get(timestamp, [])):
            if fixture_id not in pending:
                raise WalkForwardConfigurationError(
                    f"Result became available before prediction: {fixture_id}"
                )
            available.append(pending.pop(fixture_id))
        estimator.observe_batch(available)
    if pending:
        raise WalkForwardConfigurationError(
            f"Unconsumed pending forecasts: {len(pending)}"
        )
    return predictions


def _score_forecast(
    forecast: _Forecast,
    config: WalkForwardConfig,
) -> list[WalkForwardPrediction]:
    row = forecast.row
    poisson_moneyline, dixon_coles_moneyline = moneyline_probabilities(
        forecast.home_rate,
        forecast.away_rate,
        forecast.rho,
        config.poisson_tail_tolerance,
    )
    independent_exact = _poisson_probability(
        forecast.home_rate, row.home_goals
    ) * _poisson_probability(forecast.away_rate, row.away_goals)
    dixon_coles_exact = independent_exact * _dixon_coles_tau(
        forecast.home_rate,
        forecast.away_rate,
        row.home_goals,
        row.away_goals,
        forecast.rho,
    )
    actual_result = _result(row.home_goals, row.away_goals)
    values = []
    for model_key, exact_probability, moneyline, rho in (
        ("independent_poisson", independent_exact, poisson_moneyline, 0.0),
        ("dixon_coles", dixon_coles_exact, dixon_coles_moneyline, forecast.rho),
    ):
        exact_probability = max(exact_probability, config.probability_floor)
        result_probability = max(
            moneyline[actual_result], config.probability_floor
        )
        brier = sum(
            (moneyline[key] - float(key == actual_result)) ** 2
            for key in ("home_win", "draw", "away_win")
        )
        values.append(
            WalkForwardPrediction(
                evaluation_version=config.evaluation_version,
                model_key=model_key,
                feature_version=row.feature_version,
                fixture_id=row.fixture_id,
                information_state=row.information_state,
                prediction_at=row.prediction_at,
                kickoff=row.kickoff,
                fold_key=assign_fold(row.kickoff, config.folds),
                training_fixtures=forecast.training_fixtures,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                result=actual_result,
                expected_home_goals=forecast.home_rate,
                expected_away_goals=forecast.away_rate,
                home_rate_scale=forecast.home_scale,
                away_rate_scale=forecast.away_scale,
                dixon_coles_rho=rho,
                exact_score_probability=exact_probability,
                home_win_probability=moneyline["home_win"],
                draw_probability=moneyline["draw"],
                away_win_probability=moneyline["away_win"],
                exact_score_log_loss=-math.log(exact_probability),
                moneyline_log_loss=-math.log(result_probability),
                moneyline_brier=brier,
            )
        )
    return values


def summarize_predictions(
    predictions: list[WalkForwardPrediction],
    config: WalkForwardConfig,
) -> dict:
    grouped: dict[tuple[str, str, str], list[WalkForwardPrediction]] = defaultdict(list)
    for prediction in predictions:
        grouped[
            (
                prediction.model_key,
                prediction.information_state,
                prediction.fold_key,
            )
        ].append(prediction)
    metrics = []
    for (model_key, information_state, fold_key), values in sorted(grouped.items()):
        count = len(values)
        metrics.append(
            {
                "model_key": model_key,
                "information_state": information_state,
                "fold_key": fold_key,
                "fixtures": count,
                "kickoff_start": min(row.kickoff for row in values).isoformat(),
                "kickoff_end": max(row.kickoff for row in values).isoformat(),
                "mean_exact_score_log_loss": math.fsum(
                    row.exact_score_log_loss for row in values
                )
                / count,
                "mean_moneyline_log_loss": math.fsum(
                    row.moneyline_log_loss for row in values
                )
                / count,
                "mean_moneyline_brier": math.fsum(
                    row.moneyline_brier for row in values
                )
                / count,
                "moneyline_calibration_error": _calibration_error(values),
            }
        )
    return {
        "prediction_rows": len(predictions),
        "logical_predictions_sha256": prediction_rows_sha256(predictions),
        "metrics": metrics,
        "paired_model_comparisons": _paired_model_comparisons(
            predictions, config
        ),
    }


def prediction_rows_sha256(predictions: list[WalkForwardPrediction]) -> str:
    body = []
    for prediction in predictions:
        value = asdict(prediction)
        value["prediction_at"] = prediction.prediction_at.astimezone(
            timezone.utc
        ).isoformat()
        value["kickoff"] = prediction.kickoff.astimezone(timezone.utc).isoformat()
        body.append(value)
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def moneyline_probabilities(
    home_rate: float,
    away_rate: float,
    rho: float,
    tail_tolerance: float,
) -> tuple[dict[str, float], dict[str, float]]:
    home = _poisson_marginal(home_rate, tail_tolerance)
    away = _poisson_marginal(away_rate, tail_tolerance)
    independent = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    dixon_coles = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    for home_goals, home_probability in enumerate(home):
        for away_goals, away_probability in enumerate(away):
            probability = home_probability * away_probability
            result = _result(home_goals, away_goals)
            independent[result] += probability
            dixon_coles[result] += probability * _dixon_coles_tau(
                home_rate,
                away_rate,
                home_goals,
                away_goals,
                rho,
            )
    dc_total = math.fsum(dixon_coles.values())
    dixon_coles = {
        key: value / dc_total for key, value in dixon_coles.items()
    }
    return independent, dixon_coles


def _poisson_marginal(rate: float, tail_tolerance: float) -> list[float]:
    values = [math.exp(-rate)]
    cumulative = values[0]
    goals = 0
    while cumulative < 1.0 - tail_tolerance:
        goals += 1
        values.append(values[-1] * rate / goals)
        cumulative += values[-1]
        if goals >= 100:
            raise WalkForwardConfigurationError("Poisson tail failed to converge")
    return [value / cumulative for value in values]


def _poisson_probability(rate: float, goals: int) -> float:
    return math.exp(-rate + goals * math.log(rate) - math.lgamma(goals + 1))


def _dixon_coles_tau(
    home_rate: float,
    away_rate: float,
    home_goals: int,
    away_goals: int,
    rho: float,
) -> float:
    coefficient = _rho_coefficient(
        home_rate, away_rate, home_goals, away_goals
    )
    return 1.0 if coefficient is None else 1.0 + coefficient * rho


def _rho_coefficient(
    home_rate: float,
    away_rate: float,
    home_goals: int,
    away_goals: int,
) -> float | None:
    return {
        (0, 0): -home_rate * away_rate,
        (0, 1): home_rate,
        (1, 0): away_rate,
        (1, 1): -1.0,
    }.get((home_goals, away_goals))


def _valid_rho(
    rho: float,
    home_rate: float,
    away_rate: float,
    config: WalkForwardConfig,
) -> float:
    epsilon = 1e-9
    lower = max(
        config.rho_hard_minimum,
        -(1.0 - epsilon) / max(home_rate, away_rate),
    )
    upper = min(
        config.rho_hard_maximum,
        (1.0 - epsilon) / max(1.0, home_rate * away_rate),
    )
    return _clamp(rho, lower, upper)


def assign_fold(kickoff: datetime, folds: tuple[EvaluationFold, ...]) -> str:
    for fold in folds:
        if fold.kickoff_end_exclusive is None or kickoff < fold.kickoff_end_exclusive:
            return fold.fold_key
    raise WalkForwardConfigurationError("No fold covers prediction kickoff")


def _calibration_error(values: list[WalkForwardPrediction], bins: int = 10) -> float:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for row in values:
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


def _paired_model_comparisons(
    predictions: list[WalkForwardPrediction],
    config: WalkForwardConfig,
) -> list[dict]:
    paired: dict[
        tuple[str, str], dict[str, dict[str, WalkForwardPrediction]]
    ] = defaultdict(lambda: defaultdict(dict))
    for prediction in predictions:
        paired[(prediction.information_state, prediction.fold_key)][
            prediction.fixture_id
        ][prediction.model_key] = prediction

    comparisons = []
    metric_fields = (
        "exact_score_log_loss",
        "moneyline_log_loss",
        "moneyline_brier",
    )
    for (information_state, fold_key), fixture_models in sorted(paired.items()):
        complete = [
            models
            for models in fixture_models.values()
            if set(models) == {"independent_poisson", "dixon_coles"}
        ]
        if len(complete) != len(fixture_models):
            raise WalkForwardConfigurationError(
                "Paired comparison requires both models for every fixture"
            )
        for metric in metric_fields:
            blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
            for models in complete:
                dixon_coles = models["dixon_coles"]
                independent = models["independent_poisson"]
                blocks[(dixon_coles.kickoff.year, dixon_coles.kickoff.month)].append(
                    getattr(dixon_coles, metric) - getattr(independent, metric)
                )
            differences = [value for block in blocks.values() for value in block]
            lower, upper, improvement_probability = block_bootstrap_interval(
                blocks,
                replicates=config.bootstrap_replicates,
                seed=comparison_seed(
                    config.bootstrap_seed, information_state, fold_key, metric
                ),
            )
            comparisons.append(
                {
                    "challenger_model": "dixon_coles",
                    "baseline_model": "independent_poisson",
                    "information_state": information_state,
                    "fold_key": fold_key,
                    "metric": metric,
                    "fixtures": len(differences),
                    "calendar_month_blocks": len(blocks),
                    "mean_delta_challenger_minus_baseline": math.fsum(
                        differences
                    )
                    / len(differences),
                    "paired_month_block_bootstrap_95_lower": lower,
                    "paired_month_block_bootstrap_95_upper": upper,
                    "bootstrap_probability_challenger_is_better": (
                        improvement_probability
                    ),
                    "lower_is_better": True,
                }
            )
    return comparisons


def block_bootstrap_interval(
    blocks: dict[tuple[int, int], list[float]],
    *,
    replicates: int,
    seed: int,
) -> tuple[float, float, float]:
    values = list(blocks.values())
    if not values:
        raise WalkForwardConfigurationError("Bootstrap requires paired blocks")
    generator = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sampled = [values[generator.randrange(len(values))] for _ in values]
        total_count = sum(len(block) for block in sampled)
        estimates.append(
            math.fsum(value for block in sampled for value in block) / total_count
        )
    estimates.sort()
    lower = estimates[int(0.025 * (replicates - 1))]
    upper = estimates[int(0.975 * (replicates - 1))]
    better = sum(estimate < 0 for estimate in estimates) / replicates
    return lower, upper, better


def comparison_seed(base: int, *values: str) -> int:
    digest = hashlib.sha256("|".join(values).encode("utf-8")).digest()
    return base + int.from_bytes(digest[:8], "big")


def _result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def _validate_config(config: WalkForwardConfig) -> None:
    if not config.evaluation_version:
        raise WalkForwardConfigurationError("evaluation_version is required")
    if config.result_availability_delay_minutes < 0:
        raise WalkForwardConfigurationError("Result delay cannot be negative")
    if config.minimum_training_fixtures <= 0:
        raise WalkForwardConfigurationError("Training warmup must be positive")
    if not 0 < config.probability_floor < 1:
        raise WalkForwardConfigurationError("Probability floor must be in (0, 1)")
    if not 0 < config.poisson_tail_tolerance < 1:
        raise WalkForwardConfigurationError("Tail tolerance must be in (0, 1)")
    if min(
        config.minimum_expected_goals,
        config.scale_prior_observed_goals,
        config.scale_prior_expected_goals,
        config.rho_prior_variance,
    ) <= 0:
        raise WalkForwardConfigurationError("Priors and goal bounds must be positive")
    if config.maximum_expected_goals <= config.minimum_expected_goals:
        raise WalkForwardConfigurationError("Expected-goal bounds are invalid")
    if not config.rho_hard_minimum < 0 < config.rho_hard_maximum:
        raise WalkForwardConfigurationError("Rho bounds must contain zero")
    if config.bootstrap_replicates < 100:
        raise WalkForwardConfigurationError(
            "Bootstrap requires at least 100 replicates"
        )
    if config.calibration_minimum_fixtures <= 0:
        raise WalkForwardConfigurationError(
            "Calibration minimum fixtures must be positive"
        )
    if not 0 < config.temperature_minimum < 1 < config.temperature_maximum:
        raise WalkForwardConfigurationError(
            "Temperature bounds must be positive and contain one"
        )
    if config.temperature_optimizer_tolerance <= 0:
        raise WalkForwardConfigurationError(
            "Temperature optimizer tolerance must be positive"
        )
    if not config.folds or config.folds[-1].kickoff_end_exclusive is not None:
        raise WalkForwardConfigurationError("Final fold must have an open end")
    keys = [fold.fold_key for fold in config.folds]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise WalkForwardConfigurationError("Fold keys must be non-empty and unique")
    if (
        config.calibration_fit_fold not in keys
        or config.calibration_apply_fold not in keys
    ):
        raise WalkForwardConfigurationError("Calibration folds must exist")
    if keys.index(config.calibration_fit_fold) >= keys.index(
        config.calibration_apply_fold
    ):
        raise WalkForwardConfigurationError(
            "Calibration fit fold must precede apply fold"
        )
    finite_ends = [
        fold.kickoff_end_exclusive
        for fold in config.folds
        if fold.kickoff_end_exclusive is not None
    ]
    if finite_ends != sorted(finite_ends):
        raise WalkForwardConfigurationError("Fold boundaries must be chronological")


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
