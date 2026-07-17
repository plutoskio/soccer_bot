from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Protocol

import duckdb

from soccer_bot.config import load_json
from soccer_bot.modeling.walk_forward import (
    comparison_seed,
)


class ScoreGridResearchError(RuntimeError):
    """Raised when coherent score-grid research is unsafe or inconsistent."""


class ScoreRatePrediction(Protocol):
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float


@dataclass(frozen=True)
class ScoreGridWindow:
    window_key: str
    source_key: str
    fit_start_inclusive: datetime
    fit_end_exclusive: datetime
    validation_start_inclusive: datetime
    validation_end_exclusive: datetime
    purpose: str


@dataclass(frozen=True)
class ScoreGridCandidate:
    model_key: str
    family: str
    minimum_fit_fixtures: int
    temperature_minimum: float | None = None
    temperature_maximum: float | None = None
    optimizer_tolerance: float = 1e-8
    feature_names: tuple[str, ...] = ()
    feature_scales: tuple[float, ...] = ()
    ridge_penalty: float | None = None
    maximum_newton_iterations: int | None = None


@dataclass(frozen=True)
class ScoreGridResearchConfig:
    model_version: str
    research_status: str
    baseline_model_key: str
    moneyline_control_model_key: str
    moneyline_control_temperature_minimum: float
    moneyline_control_temperature_maximum: float
    moneyline_control_optimizer_tolerance: float
    moneyline_control_minimum_fit_fixtures: int
    forbidden_kickoff_start: datetime
    poisson_tail_tolerance: float
    minimum_max_goals: int
    maximum_max_goals: int
    probability_floor: float
    windows: tuple[ScoreGridWindow, ...]
    candidates: tuple[ScoreGridCandidate, ...]
    selection_primary_metric: str
    selection_require_negative_every_horizon: bool
    selection_tie_break_metrics: tuple[str, ...]
    confirmation_exact_upper_below_zero: bool
    confirmation_nonpositive_metrics: tuple[str, ...]
    confirmation_moneyline_delta_maximum: float
    bootstrap_replicates: int
    bootstrap_seed: int


@dataclass(frozen=True)
class ScoreGridFit:
    model_key: str
    family: str
    window_key: str
    information_state: str
    fit_fixtures: int
    parameters: dict[str, float]
    converged: bool
    iterations: int
    objective: float


@dataclass(frozen=True)
class ScoreGridEvaluationRow:
    model_key: str
    baseline_model_key: str
    window_key: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float
    exact_score_probability: float
    home_goals_probability: float
    away_goals_probability: float
    total_goals_probability: float
    goal_difference_probability: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    both_teams_to_score_probability: float
    exact_score_log_loss: float
    home_goals_log_loss: float
    away_goals_log_loss: float
    total_goals_log_loss: float
    goal_difference_log_loss: float
    moneyline_log_loss: float
    moneyline_brier: float
    both_teams_to_score_log_loss: float
    both_teams_to_score_brier: float
    total_goals_rps: float
    goal_difference_rps: float


@dataclass(frozen=True)
class ScoreRateRow:
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float


@dataclass(frozen=True)
class _PreparedGrid:
    home_probabilities: tuple[float, ...]
    away_probabilities: tuple[float, ...]
    observed_home_goals: int
    observed_away_goals: int


METRIC_FIELDS = (
    "exact_score_log_loss",
    "home_goals_log_loss",
    "away_goals_log_loss",
    "total_goals_log_loss",
    "goal_difference_log_loss",
    "moneyline_log_loss",
    "moneyline_brier",
    "both_teams_to_score_log_loss",
    "both_teams_to_score_brier",
    "total_goals_rps",
    "goal_difference_rps",
)


def load_score_grid_research_config(path: Path) -> ScoreGridResearchConfig:
    raw = load_json(path)
    if raw.get("research_status") != "development_only_no_opened_final_test_access":
        raise ScoreGridResearchError("Score-grid research must exclude the opened test")
    if raw.get("parameter_status") != (
        "candidate_definitions_frozen_before_confirmation_window_scoring"
    ):
        raise ScoreGridResearchError("Score-grid candidate definitions are not frozen")
    support = raw.get("support", {})
    moneyline_control = raw.get("moneyline_control", {})
    uncertainty = raw.get("uncertainty", {})
    if uncertainty.get("paired_block_unit") != "calendar_month":
        raise ScoreGridResearchError("Only calendar-month uncertainty is supported")
    windows = tuple(
        ScoreGridWindow(
            window_key=str(item["window_key"]),
            source_key=str(item["source_key"]),
            fit_start_inclusive=datetime.fromisoformat(item["fit_start_inclusive"]),
            fit_end_exclusive=datetime.fromisoformat(item["fit_end_exclusive"]),
            validation_start_inclusive=datetime.fromisoformat(
                item["validation_start_inclusive"]
            ),
            validation_end_exclusive=datetime.fromisoformat(
                item["validation_end_exclusive"]
            ),
            purpose=str(item["purpose"]),
        )
        for item in raw.get("windows", [])
    )
    candidates = []
    for item in raw.get("candidates", []):
        feature_names = tuple(item.get("features", []))
        scales = item.get("feature_scales", {})
        candidates.append(
            ScoreGridCandidate(
                model_key=str(item["model_key"]),
                family=str(item["family"]),
                minimum_fit_fixtures=int(
                    item["minimum_fit_fixtures_per_horizon"]
                ),
                temperature_minimum=(
                    None
                    if item.get("temperature_minimum") is None
                    else float(item["temperature_minimum"])
                ),
                temperature_maximum=(
                    None
                    if item.get("temperature_maximum") is None
                    else float(item["temperature_maximum"])
                ),
                optimizer_tolerance=float(item["optimizer_tolerance"]),
                feature_names=feature_names,
                feature_scales=tuple(float(scales[name]) for name in feature_names),
                ridge_penalty=(
                    None
                    if item.get("ridge_penalty") is None
                    else float(item["ridge_penalty"])
                ),
                maximum_newton_iterations=(
                    None
                    if item.get("maximum_newton_iterations") is None
                    else int(item["maximum_newton_iterations"])
                ),
            )
        )
    selection = raw.get("selection_policy", {})
    confirmation = raw.get("confirmation_gate", {})
    config = ScoreGridResearchConfig(
        model_version=str(raw.get("model_version", "")),
        research_status=str(raw.get("research_status", "")),
        baseline_model_key=str(raw.get("baseline_model_key", "")),
        moneyline_control_model_key=str(moneyline_control.get("model_key", "")),
        moneyline_control_temperature_minimum=float(
            moneyline_control["temperature_minimum"]
        ),
        moneyline_control_temperature_maximum=float(
            moneyline_control["temperature_maximum"]
        ),
        moneyline_control_optimizer_tolerance=float(
            moneyline_control["optimizer_tolerance"]
        ),
        moneyline_control_minimum_fit_fixtures=int(
            moneyline_control["minimum_fit_fixtures_per_horizon"]
        ),
        forbidden_kickoff_start=datetime.fromisoformat(
            raw["forbidden_kickoff_start"]
        ),
        poisson_tail_tolerance=float(support["poisson_tail_tolerance"]),
        minimum_max_goals=int(support["minimum_max_goals_per_team"]),
        maximum_max_goals=int(support["maximum_max_goals_per_team"]),
        probability_floor=float(support["probability_floor"]),
        windows=windows,
        candidates=tuple(candidates),
        selection_primary_metric=str(selection["primary_metric"]),
        selection_require_negative_every_horizon=bool(
            selection["require_negative_mean_delta_in_every_horizon"]
        ),
        selection_tie_break_metrics=tuple(selection["tie_break_metrics"]),
        confirmation_exact_upper_below_zero=bool(
            confirmation["require_exact_score_bootstrap_upper_below_zero"]
        ),
        confirmation_nonpositive_metrics=tuple(
            confirmation["require_nonpositive_mean_delta_metrics"]
        ),
        confirmation_moneyline_delta_maximum=float(
            confirmation["moneyline_mean_delta_maximum"]
        ),
        bootstrap_replicates=int(uncertainty["bootstrap_replicates"]),
        bootstrap_seed=int(uncertainty["bootstrap_seed"]),
    )
    _validate_config(config)
    return config


def read_rich_rate_predictions(
    path: Path,
    *,
    feature_path: Path,
    kickoff_start: datetime,
    kickoff_end: datetime,
) -> list[ScoreRateRow]:
    """Read only an explicitly bounded chronological slice from a Parquet file."""

    connection = duckdb.connect(":memory:")
    try:
        relation = connection.execute(
            f"""
            SELECT
                cast(p.fixture_id AS VARCHAR) AS fixture_id,
                p.information_state,
                p.prediction_at,
                p.kickoff,
                cast(f.home_goals AS INTEGER) AS home_goals,
                cast(f.away_goals AS INTEGER) AS away_goals,
                p.expected_home_goals,
                p.expected_away_goals
            FROM read_parquet({_sql_literal(path)}) p
            JOIN read_parquet({_sql_literal(feature_path)}) f
              ON p.fixture_id=f.fixture_id
             AND p.information_state=f.information_state
            WHERE p.kickoff >= ? AND p.kickoff < ?
            ORDER BY p.kickoff, p.fixture_id, p.information_state
            """,
            [kickoff_start, kickoff_end],
        )
        values = [ScoreRateRow(*row) for row in relation.fetchall()]
        keys = [(row.fixture_id, row.information_state) for row in values]
        if len(keys) != len(set(keys)):
            raise ScoreGridResearchError("Rich/target join produced duplicate rows")
        return values
    finally:
        connection.close()


def poisson_score_grid(
    home_rate: float,
    away_rate: float,
    config: ScoreGridResearchConfig,
) -> dict[tuple[int, int], float]:
    home = _poisson_marginal(home_rate, config)
    away = _poisson_marginal(away_rate, config)
    return {
        (home_goals, away_goals): home_probability * away_probability
        for home_goals, home_probability in enumerate(home)
        for away_goals, away_probability in enumerate(away)
    }


def transform_score_grid(
    base_grid: dict[tuple[int, int], float],
    candidate: ScoreGridCandidate,
    parameters: dict[str, float],
) -> dict[tuple[int, int], float]:
    if candidate.family == "grid_temperature":
        temperature = parameters["temperature"]
        if temperature <= 0:
            raise ScoreGridResearchError("Grid temperature must be positive")
        log_weights = {
            score: math.log(probability) / temperature
            for score, probability in base_grid.items()
        }
    elif candidate.family == "exponential_tilt":
        theta = tuple(parameters[name] for name in candidate.feature_names)
        log_weights = {
            score: math.log(probability)
            + math.fsum(
                coefficient * value
                for coefficient, value in zip(
                    theta, _score_features(score, candidate), strict=True
                )
            )
            for score, probability in base_grid.items()
        }
    else:
        raise ScoreGridResearchError(
            f"Unsupported score-grid family: {candidate.family}"
        )
    maximum = max(log_weights.values())
    weights = {
        score: math.exp(value - maximum) for score, value in log_weights.items()
    }
    total = math.fsum(weights.values())
    return {score: value / total for score, value in weights.items()}


def fit_score_grid_candidate(
    rows: list[ScoreRatePrediction],
    candidate: ScoreGridCandidate,
    *,
    config: ScoreGridResearchConfig,
    window_key: str,
    information_state: str,
) -> ScoreGridFit:
    if len(rows) < candidate.minimum_fit_fixtures:
        raise ScoreGridResearchError(
            f"Insufficient {window_key}/{information_state} fit rows: {len(rows)}"
        )
    _validate_rows_before_forbidden(rows, config)
    prepared = [_prepare(row, config) for row in rows]
    if candidate.family == "grid_temperature":
        parameters, converged, iterations, objective = _fit_grid_temperature(
            prepared, candidate
        )
    elif candidate.family == "exponential_tilt":
        parameters, converged, iterations, objective = _fit_exponential_tilt(
            prepared, candidate
        )
    else:
        raise ScoreGridResearchError(f"Unknown candidate family {candidate.family}")
    return ScoreGridFit(
        model_key=candidate.model_key,
        family=candidate.family,
        window_key=window_key,
        information_state=information_state,
        fit_fixtures=len(rows),
        parameters=parameters,
        converged=converged,
        iterations=iterations,
        objective=objective,
    )


def evaluate_score_grid_window(
    rows: list[ScoreRatePrediction],
    *,
    window: ScoreGridWindow,
    candidates: tuple[ScoreGridCandidate, ...],
    config: ScoreGridResearchConfig,
) -> tuple[list[ScoreGridFit], list[ScoreGridEvaluationRow], dict]:
    _validate_rows_before_forbidden(rows, config)
    grouped: dict[str, list[ScoreRatePrediction]] = defaultdict(list)
    for row in rows:
        grouped[row.information_state].append(row)
    fits: list[ScoreGridFit] = []
    evaluations: list[ScoreGridEvaluationRow] = []
    for information_state, values in sorted(grouped.items()):
        fit_rows = [
            row
            for row in values
            if window.fit_start_inclusive <= row.kickoff < window.fit_end_exclusive
        ]
        validation_rows = [
            row
            for row in values
            if window.validation_start_inclusive
            <= row.kickoff
            < window.validation_end_exclusive
        ]
        if not validation_rows:
            raise ScoreGridResearchError(
                f"No validation rows for {window.window_key}/{information_state}"
            )
        control_temperature, control_iterations, control_objective = (
            _fit_moneyline_control_temperature(fit_rows, config)
        )
        fits.append(
            ScoreGridFit(
                model_key=config.moneyline_control_model_key,
                family="moneyline_temperature_control",
                window_key=window.window_key,
                information_state=information_state,
                fit_fixtures=len(fit_rows),
                parameters={"temperature": control_temperature},
                converged=True,
                iterations=control_iterations,
                objective=control_objective,
            )
        )
        for row in validation_rows:
            base = poisson_score_grid(
                row.expected_home_goals, row.expected_away_goals, config
            )
            evaluations.append(
                _score_grid(
                    row,
                    base,
                    model_key=config.baseline_model_key,
                    baseline_model_key=config.baseline_model_key,
                    window_key=window.window_key,
                    probability_floor=config.probability_floor,
                )
            )
            evaluations.append(
                _score_grid(
                    row,
                    base,
                    model_key=config.moneyline_control_model_key,
                    baseline_model_key=config.baseline_model_key,
                    window_key=window.window_key,
                    probability_floor=config.probability_floor,
                    moneyline_temperature=control_temperature,
                )
            )
        for candidate in candidates:
            fit = fit_score_grid_candidate(
                fit_rows,
                candidate,
                config=config,
                window_key=window.window_key,
                information_state=information_state,
            )
            if not fit.converged:
                raise ScoreGridResearchError(
                    f"Fit did not converge: {candidate.model_key}/{information_state}"
                )
            fits.append(fit)
            for row in validation_rows:
                base = poisson_score_grid(
                    row.expected_home_goals, row.expected_away_goals, config
                )
                transformed = transform_score_grid(base, candidate, fit.parameters)
                evaluations.append(
                    _score_grid(
                        row,
                        transformed,
                        model_key=candidate.model_key,
                        baseline_model_key=config.baseline_model_key,
                        window_key=window.window_key,
                        probability_floor=config.probability_floor,
                    )
                )
    ordered = sorted(
        evaluations,
        key=lambda row: (
            row.kickoff,
            row.fixture_id,
            row.information_state,
            row.model_key,
        ),
    )
    return fits, ordered, summarize_score_grid_evaluation(ordered, config)


def summarize_score_grid_evaluation(
    rows: list[ScoreGridEvaluationRow], config: ScoreGridResearchConfig
) -> dict:
    grouped: dict[tuple[str, str, str], list[ScoreGridEvaluationRow]] = defaultdict(list)
    by_fixture: dict[
        tuple[str, str, str], dict[str, ScoreGridEvaluationRow]
    ] = defaultdict(dict)
    for row in rows:
        grouped[(row.window_key, row.information_state, row.model_key)].append(row)
        by_fixture[(row.window_key, row.information_state, row.fixture_id)][
            row.model_key
        ] = row
    metrics = []
    for (window_key, information_state, model_key), values in sorted(grouped.items()):
        metrics.append(
            {
                "window_key": window_key,
                "information_state": information_state,
                "model_key": model_key,
                "fixtures": len(values),
                **{
                    f"mean_{metric}": math.fsum(getattr(row, metric) for row in values)
                    / len(values)
                    for metric in METRIC_FIELDS
                },
            }
        )
    comparisons = []
    comparison_groups: dict[
        tuple[str, str, str],
        list[
            tuple[
                ScoreGridEvaluationRow,
                ScoreGridEvaluationRow,
                ScoreGridEvaluationRow,
            ]
        ],
    ] = defaultdict(list)
    for (window_key, information_state, _), models in by_fixture.items():
        baseline = models.get(config.baseline_model_key)
        if baseline is None:
            raise ScoreGridResearchError("Every fixture requires the baseline grid")
        moneyline_control = models.get(config.moneyline_control_model_key)
        if moneyline_control is None:
            raise ScoreGridResearchError(
                "Every fixture requires the calibrated moneyline control"
            )
        for model_key, challenger in models.items():
            if model_key not in {
                config.baseline_model_key,
                config.moneyline_control_model_key,
            }:
                comparison_groups[(window_key, information_state, model_key)].append(
                    (challenger, baseline, moneyline_control)
                )
    for (window_key, information_state, model_key), pairs in sorted(
        comparison_groups.items()
    ):
        for metric in METRIC_FIELDS:
            blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
            comparison_baseline_key = (
                config.moneyline_control_model_key
                if metric in {"moneyline_log_loss", "moneyline_brier"}
                else config.baseline_model_key
            )
            for challenger, baseline, moneyline_control in pairs:
                comparison_baseline = (
                    moneyline_control
                    if comparison_baseline_key == config.moneyline_control_model_key
                    else baseline
                )
                blocks[(challenger.kickoff.year, challenger.kickoff.month)].append(
                    getattr(challenger, metric)
                    - getattr(comparison_baseline, metric)
                )
            differences = [value for block in blocks.values() for value in block]
            lower, upper, probability = _block_bootstrap_interval(
                blocks,
                replicates=config.bootstrap_replicates,
                seed=comparison_seed(
                    config.bootstrap_seed,
                    "score_grid",
                    window_key,
                    information_state,
                    model_key,
                    metric,
                ),
            )
            comparisons.append(
                {
                    "window_key": window_key,
                    "information_state": information_state,
                    "challenger_model": model_key,
                    "baseline_model": comparison_baseline_key,
                    "metric": metric,
                    "fixtures": len(pairs),
                    "calendar_month_blocks": len(blocks),
                    "mean_delta_challenger_minus_baseline": math.fsum(differences)
                    / len(differences),
                    "paired_month_block_bootstrap_95_lower": lower,
                    "paired_month_block_bootstrap_95_upper": upper,
                    "bootstrap_probability_challenger_is_better": probability,
                    "lower_is_better": True,
                }
            )
    return {"metrics": metrics, "paired_model_comparisons": comparisons}


def select_candidate(selection_summary: dict, config: ScoreGridResearchConfig) -> dict:
    primary = config.selection_primary_metric
    all_comparisons = selection_summary["paired_model_comparisons"]
    comparisons = [
        item
        for item in all_comparisons
        if item["metric"] == primary
    ]
    horizons = sorted({item["information_state"] for item in comparisons})
    candidates = sorted({item["challenger_model"] for item in comparisons})
    eligible = []
    for model_key in candidates:
        values = [item for item in comparisons if item["challenger_model"] == model_key]
        if {item["information_state"] for item in values} != set(horizons):
            raise ScoreGridResearchError("Candidate comparison horizons differ")
        if config.selection_require_negative_every_horizon and any(
            item["mean_delta_challenger_minus_baseline"] >= 0 for item in values
        ):
            continue
        weighted_delta = math.fsum(
            item["fixtures"] * item["mean_delta_challenger_minus_baseline"]
            for item in values
        ) / sum(item["fixtures"] for item in values)
        tie_break_deltas = []
        for metric in config.selection_tie_break_metrics:
            metric_values = [
                item
                for item in all_comparisons
                if item["challenger_model"] == model_key
                and item["metric"] == metric
            ]
            if len(metric_values) != len(horizons):
                raise ScoreGridResearchError(
                    f"Missing tie-break comparison for {model_key}/{metric}"
                )
            tie_break_deltas.append(
                math.fsum(
                    item["fixtures"] * item["mean_delta_challenger_minus_baseline"]
                    for item in metric_values
                )
                / sum(item["fixtures"] for item in metric_values)
            )
        eligible.append(
            ((weighted_delta, *tie_break_deltas, model_key), model_key, tie_break_deltas)
        )
    if not eligible:
        return {
            "selected_model": None,
            "selection_gate_passed": False,
            "reason": "no_candidate_improved_primary_metric_in_every_horizon",
            "primary_metric": primary,
        }
    key, model_key, tie_break_deltas = min(eligible, key=lambda item: item[0])
    return {
        "selected_model": model_key,
        "selection_gate_passed": True,
        "reason": "best_weighted_primary_metric_among_every_horizon_improvers",
        "primary_metric": primary,
        "weighted_mean_delta": key[0],
        "tie_break_metrics": list(config.selection_tie_break_metrics),
        "tie_break_weighted_mean_deltas": dict(
            zip(config.selection_tie_break_metrics, tie_break_deltas, strict=True)
        ),
    }


def confirmation_gate(
    confirmation_summary: dict,
    selected_model: str,
    config: ScoreGridResearchConfig,
) -> dict:
    comparisons = [
        item
        for item in confirmation_summary["paired_model_comparisons"]
        if item["challenger_model"] == selected_model
    ]
    failures = []
    exact = [item for item in comparisons if item["metric"] == "exact_score_log_loss"]
    horizons = {item["information_state"] for item in exact}
    if not horizons or len(exact) != len(horizons):
        failures.append("missing_or_duplicate_exact_score_confirmation_comparison")
    if config.confirmation_exact_upper_below_zero:
        for item in exact:
            if item["paired_month_block_bootstrap_95_upper"] >= 0:
                failures.append(
                    f"exact_score_interval_crosses_zero:{item['information_state']}"
                )
    for metric in config.confirmation_nonpositive_metrics:
        metric_values = [item for item in comparisons if item["metric"] == metric]
        if (
            len(metric_values) != len(horizons)
            or {item["information_state"] for item in metric_values} != horizons
        ):
            failures.append(f"missing_or_duplicate_confirmation_metric:{metric}")
        for item in metric_values:
            if item["mean_delta_challenger_minus_baseline"] > 0:
                failures.append(f"positive_mean_delta:{metric}:{item['information_state']}")
    moneyline = [
        item for item in comparisons if item["metric"] == "moneyline_log_loss"
    ]
    if (
        len(moneyline) != len(horizons)
        or {item["information_state"] for item in moneyline} != horizons
    ):
        failures.append("missing_or_duplicate_confirmation_metric:moneyline_log_loss")
    for item in moneyline:
        if (
            item["mean_delta_challenger_minus_baseline"]
            > config.confirmation_moneyline_delta_maximum
        ):
            failures.append(
                f"moneyline_delta_above_limit:{item['information_state']}"
            )
    return {
        "selected_model": selected_model,
        "confirmation_gate_passed": not failures,
        "failures": failures,
        "production_status": (
            "await_new_forward_holdout_even_if_internal_gate_passes"
            if not failures
            else "research_candidate_failed_confirmation_gate"
        ),
    }


def evaluation_rows_sha256(rows: list[ScoreGridEvaluationRow]) -> str:
    values = []
    for row in rows:
        value = asdict(row)
        value["prediction_at"] = row.prediction_at.isoformat()
        value["kickoff"] = row.kickoff.isoformat()
        values.append(value)
    body = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _prepare(row: ScoreRatePrediction, config: ScoreGridResearchConfig) -> _PreparedGrid:
    if row.home_goals < 0 or row.away_goals < 0:
        raise ScoreGridResearchError("Observed scores must be nonnegative")
    home = tuple(_poisson_marginal(row.expected_home_goals, config))
    away = tuple(_poisson_marginal(row.expected_away_goals, config))
    if row.home_goals >= len(home) or row.away_goals >= len(away):
        raise ScoreGridResearchError(
            f"Observed score outside configured support: {row.fixture_id}"
        )
    return _PreparedGrid(home, away, row.home_goals, row.away_goals)


def _fit_moneyline_control_temperature(
    rows: list[ScoreRatePrediction], config: ScoreGridResearchConfig
) -> tuple[float, int, float]:
    if len(rows) < config.moneyline_control_minimum_fit_fixtures:
        raise ScoreGridResearchError(
            "Insufficient rows for the calibrated moneyline control: "
            f"{len(rows)}"
        )
    prepared = []
    for row in rows:
        grid = poisson_score_grid(
            row.expected_home_goals, row.expected_away_goals, config
        )
        probabilities = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        for (home_goals, away_goals), probability in grid.items():
            key = (
                "home_win"
                if home_goals > away_goals
                else "draw" if home_goals == away_goals else "away_win"
            )
            probabilities[key] += probability
        result = (
            "home_win"
            if row.home_goals > row.away_goals
            else "draw" if row.home_goals == row.away_goals else "away_win"
        )
        prepared.append((probabilities, result))

    def objective(log_temperature: float) -> float:
        temperature = math.exp(log_temperature)
        return math.fsum(
            -math.log(
                _temperature_scale_moneyline(probabilities, temperature)[result]
            )
            for probabilities, result in prepared
        )

    lower = math.log(config.moneyline_control_temperature_minimum)
    upper = math.log(config.moneyline_control_temperature_maximum)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = objective(left)
    right_value = objective(right)
    iterations = 0
    while upper - lower > config.moneyline_control_optimizer_tolerance:
        iterations += 1
        if left_value <= right_value:
            upper = right
            right = left
            right_value = left_value
            left = upper - ratio * (upper - lower)
            left_value = objective(left)
        else:
            lower = left
            left = right
            left_value = right_value
            right = lower + ratio * (upper - lower)
            right_value = objective(right)
    log_temperature = (lower + upper) / 2.0
    return math.exp(log_temperature), iterations, objective(log_temperature)


def _temperature_scale_moneyline(
    probabilities: dict[str, float], temperature: float
) -> dict[str, float]:
    if not math.isfinite(temperature) or temperature <= 0:
        raise ScoreGridResearchError(
            "Moneyline control temperature must be finite and positive"
        )
    if set(probabilities) != {"home_win", "draw", "away_win"}:
        raise ScoreGridResearchError("Moneyline control requires three outcomes")
    if any(not math.isfinite(value) or value <= 0 for value in probabilities.values()):
        raise ScoreGridResearchError(
            "Moneyline control probabilities must be finite and positive"
        )
    log_weights = {
        key: math.log(value) / temperature
        for key, value in probabilities.items()
    }
    maximum = max(log_weights.values())
    weights = {
        key: math.exp(value - maximum) for key, value in log_weights.items()
    }
    total = math.fsum(weights.values())
    return {key: value / total for key, value in weights.items()}


def _fit_grid_temperature(
    rows: list[_PreparedGrid], candidate: ScoreGridCandidate
) -> tuple[dict[str, float], bool, int, float]:
    if candidate.temperature_minimum is None or candidate.temperature_maximum is None:
        raise ScoreGridResearchError("Temperature bounds are required")
    lower = math.log(candidate.temperature_minimum)
    upper = math.log(candidate.temperature_maximum)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = _temperature_objective(rows, math.exp(left))
    right_value = _temperature_objective(rows, math.exp(right))
    iterations = 0
    while upper - lower > candidate.optimizer_tolerance:
        iterations += 1
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
    temperature = math.exp((lower + upper) / 2.0)
    return (
        {"temperature": temperature},
        True,
        iterations,
        _temperature_objective(rows, temperature),
    )


def _temperature_objective(rows: list[_PreparedGrid], temperature: float) -> float:
    return math.fsum(_temperature_nll(row, temperature) for row in rows)


def _temperature_nll(row: _PreparedGrid, temperature: float) -> float:
    exponent = 1.0 / temperature
    home_weights = [value**exponent for value in row.home_probabilities]
    away_weights = [value**exponent for value in row.away_probabilities]
    home_probability = home_weights[row.observed_home_goals] / math.fsum(home_weights)
    away_probability = away_weights[row.observed_away_goals] / math.fsum(away_weights)
    return -math.log(home_probability * away_probability)


def _fit_exponential_tilt(
    rows: list[_PreparedGrid], candidate: ScoreGridCandidate
) -> tuple[dict[str, float], bool, int, float]:
    if candidate.ridge_penalty is None or candidate.maximum_newton_iterations is None:
        raise ScoreGridResearchError("Tilt optimizer configuration is incomplete")
    size = len(candidate.feature_names)
    theta = [0.0] * size
    feature_cache = _feature_cache(rows, candidate)
    for iteration in range(1, candidate.maximum_newton_iterations + 1):
        score = [-candidate.ridge_penalty * value for value in theta]
        information = [
            [candidate.ridge_penalty if i == j else 0.0 for j in range(size)]
            for i in range(size)
        ]
        for row in rows:
            log_z, means, covariance = _tilt_moments(
                row, theta, feature_cache, need_covariance=True
            )
            del log_z
            observed = feature_cache[(row.observed_home_goals, row.observed_away_goals)]
            for i in range(size):
                score[i] += observed[i] - means[i]
                for j in range(size):
                    information[i][j] += covariance[i][j]
        step = _solve(information, score)
        if max(abs(value) for value in step) < candidate.optimizer_tolerance:
            objective = _tilt_objective(rows, theta, candidate, feature_cache)
            return (
                dict(zip(candidate.feature_names, theta, strict=True)),
                True,
                iteration,
                objective,
            )
        current = _tilt_objective(rows, theta, candidate, feature_cache)
        scale = 1.0
        while scale >= 1e-6:
            proposed = [
                value + scale * change for value, change in zip(theta, step, strict=True)
            ]
            proposed_objective = _tilt_objective(
                rows, proposed, candidate, feature_cache
            )
            if proposed_objective < current:
                theta = proposed
                break
            scale *= 0.5
        else:
            return (
                dict(zip(candidate.feature_names, theta, strict=True)),
                False,
                iteration,
                current,
            )
    objective = _tilt_objective(rows, theta, candidate, feature_cache)
    return (
        dict(zip(candidate.feature_names, theta, strict=True)),
        False,
        candidate.maximum_newton_iterations,
        objective,
    )


def _tilt_objective(
    rows: list[_PreparedGrid],
    theta: list[float],
    candidate: ScoreGridCandidate,
    feature_cache: dict[tuple[int, int], tuple[float, ...]],
) -> float:
    value = 0.5 * candidate.ridge_penalty * math.fsum(item * item for item in theta)
    for row in rows:
        log_z, _, _ = _tilt_moments(row, theta, feature_cache, need_covariance=False)
        observed = feature_cache[(row.observed_home_goals, row.observed_away_goals)]
        log_base = math.log(row.home_probabilities[row.observed_home_goals]) + math.log(
            row.away_probabilities[row.observed_away_goals]
        )
        value += log_z - log_base - math.fsum(
            coefficient * feature
            for coefficient, feature in zip(theta, observed, strict=True)
        )
    return value


def _tilt_moments(
    row: _PreparedGrid,
    theta: list[float],
    feature_cache: dict[tuple[int, int], tuple[float, ...]],
    *,
    need_covariance: bool,
) -> tuple[float, list[float], list[list[float]]]:
    weighted = []
    maximum = -math.inf
    for home_goals, home_probability in enumerate(row.home_probabilities):
        for away_goals, away_probability in enumerate(row.away_probabilities):
            features = feature_cache[(home_goals, away_goals)]
            log_weight = (
                math.log(home_probability)
                + math.log(away_probability)
                + math.fsum(
                    coefficient * feature
                    for coefficient, feature in zip(theta, features, strict=True)
                )
            )
            weighted.append((log_weight, features))
            maximum = max(maximum, log_weight)
    weights = [math.exp(log_weight - maximum) for log_weight, _ in weighted]
    total = math.fsum(weights)
    size = len(theta)
    means = [0.0] * size
    for weight, (_, features) in zip(weights, weighted, strict=True):
        for i in range(size):
            means[i] += weight * features[i]
    means = [value / total for value in means]
    covariance = [[0.0] * size for _ in range(size)]
    if need_covariance:
        for weight, (_, features) in zip(weights, weighted, strict=True):
            probability = weight / total
            for i in range(size):
                left = features[i] - means[i]
                for j in range(size):
                    covariance[i][j] += probability * left * (
                        features[j] - means[j]
                    )
    return maximum + math.log(total), means, covariance


def _feature_cache(
    rows: list[_PreparedGrid], candidate: ScoreGridCandidate
) -> dict[tuple[int, int], tuple[float, ...]]:
    maximum_home = max(len(row.home_probabilities) for row in rows) - 1
    maximum_away = max(len(row.away_probabilities) for row in rows) - 1
    return {
        (home_goals, away_goals): _score_features(
            (home_goals, away_goals), candidate
        )
        for home_goals in range(maximum_home + 1)
        for away_goals in range(maximum_away + 1)
    }


def _score_features(
    score: tuple[int, int], candidate: ScoreGridCandidate
) -> tuple[float, ...]:
    home_goals, away_goals = score
    raw = {
        "home_goals": float(home_goals),
        "away_goals": float(away_goals),
        "log_factorial_sum": math.lgamma(home_goals + 1)
        + math.lgamma(away_goals + 1),
        "draw": float(home_goals == away_goals),
        "zero_zero": float(home_goals == away_goals == 0),
        "both_teams_to_score": float(home_goals > 0 and away_goals > 0),
    }
    try:
        return tuple(
            raw[name] / scale
            for name, scale in zip(
                candidate.feature_names, candidate.feature_scales, strict=True
            )
        )
    except KeyError as error:
        raise ScoreGridResearchError(f"Unsupported tilt feature: {error.args[0]}") from None


def _score_grid(
    row: ScoreRatePrediction,
    grid: dict[tuple[int, int], float],
    *,
    model_key: str,
    baseline_model_key: str,
    window_key: str,
    probability_floor: float,
    moneyline_temperature: float | None = None,
) -> ScoreGridEvaluationRow:
    total_probability = math.fsum(grid.values())
    if not math.isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ScoreGridResearchError(f"Score grid is not normalized: {total_probability}")
    if any(not math.isfinite(value) or value <= 0 for value in grid.values()):
        raise ScoreGridResearchError("Score grid cells must be finite and positive")
    home: dict[int, float] = defaultdict(float)
    away: dict[int, float] = defaultdict(float)
    totals: dict[int, float] = defaultdict(float)
    differences: dict[int, float] = defaultdict(float)
    moneyline = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    btts_yes = 0.0
    for (home_goals, away_goals), probability in grid.items():
        home[home_goals] += probability
        away[away_goals] += probability
        totals[home_goals + away_goals] += probability
        differences[home_goals - away_goals] += probability
        if home_goals > away_goals:
            moneyline["home_win"] += probability
        elif home_goals < away_goals:
            moneyline["away_win"] += probability
        else:
            moneyline["draw"] += probability
        if home_goals > 0 and away_goals > 0:
            btts_yes += probability
    if moneyline_temperature is not None:
        moneyline = _temperature_scale_moneyline(
            moneyline, moneyline_temperature
        )
    result = (
        "home_win"
        if row.home_goals > row.away_goals
        else "draw" if row.home_goals == row.away_goals else "away_win"
    )
    actual_btts = row.home_goals > 0 and row.away_goals > 0
    exact = max(grid.get((row.home_goals, row.away_goals), 0.0), probability_floor)
    home_probability = max(home.get(row.home_goals, 0.0), probability_floor)
    away_probability = max(away.get(row.away_goals, 0.0), probability_floor)
    total_goals_probability = max(
        totals.get(row.home_goals + row.away_goals, 0.0), probability_floor
    )
    difference_probability = max(
        differences.get(row.home_goals - row.away_goals, 0.0), probability_floor
    )
    moneyline_probability = max(moneyline[result], probability_floor)
    btts_probability = btts_yes if actual_btts else 1.0 - btts_yes
    btts_probability = max(btts_probability, probability_floor)
    return ScoreGridEvaluationRow(
        model_key=model_key,
        baseline_model_key=baseline_model_key,
        window_key=window_key,
        fixture_id=row.fixture_id,
        information_state=row.information_state,
        prediction_at=row.prediction_at,
        kickoff=row.kickoff,
        home_goals=row.home_goals,
        away_goals=row.away_goals,
        expected_home_goals=row.expected_home_goals,
        expected_away_goals=row.expected_away_goals,
        exact_score_probability=exact,
        home_goals_probability=home_probability,
        away_goals_probability=away_probability,
        total_goals_probability=total_goals_probability,
        goal_difference_probability=difference_probability,
        home_win_probability=moneyline["home_win"],
        draw_probability=moneyline["draw"],
        away_win_probability=moneyline["away_win"],
        both_teams_to_score_probability=btts_yes,
        exact_score_log_loss=-math.log(exact),
        home_goals_log_loss=-math.log(home_probability),
        away_goals_log_loss=-math.log(away_probability),
        total_goals_log_loss=-math.log(total_goals_probability),
        goal_difference_log_loss=-math.log(difference_probability),
        moneyline_log_loss=-math.log(moneyline_probability),
        moneyline_brier=math.fsum(
            (moneyline[key] - float(key == result)) ** 2 for key in moneyline
        ),
        both_teams_to_score_log_loss=-math.log(btts_probability),
        both_teams_to_score_brier=(btts_yes - float(actual_btts)) ** 2,
        total_goals_rps=_rps(totals, row.home_goals + row.away_goals),
        goal_difference_rps=_rps(differences, row.home_goals - row.away_goals),
    )


def _rps(distribution: dict[int, float], observed: int) -> float:
    support = range(min(distribution), max(distribution))
    cumulative = 0.0
    value = 0.0
    for threshold in support:
        cumulative += distribution.get(threshold, 0.0)
        value += (cumulative - float(observed <= threshold)) ** 2
    return value


def _block_bootstrap_interval(
    blocks: dict[tuple[int, int], list[float]],
    *,
    replicates: int,
    seed: int,
) -> tuple[float, float, float]:
    """Month bootstrap using block sufficient statistics.

    This is algebraically identical to flattening every sampled fixture delta,
    but avoids repeatedly traversing thousands of fixture values for each
    replicate and metric.
    """

    summaries = [
        (math.fsum(values), len(values)) for values in blocks.values() if values
    ]
    if not summaries:
        raise ScoreGridResearchError("Bootstrap requires non-empty month blocks")
    generator = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        total = 0.0
        count = 0
        for _ in summaries:
            block_sum, block_count = summaries[generator.randrange(len(summaries))]
            total += block_sum
            count += block_count
        estimates.append(total / count)
    estimates.sort()
    lower = estimates[int(0.025 * (replicates - 1))]
    upper = estimates[int(0.975 * (replicates - 1))]
    better = sum(estimate < 0 for estimate in estimates) / replicates
    return lower, upper, better


def _poisson_marginal(
    rate: float, config: ScoreGridResearchConfig
) -> list[float]:
    if not math.isfinite(rate) or rate <= 0:
        raise ScoreGridResearchError("Poisson rate must be finite and positive")
    values = [math.exp(-rate)]
    cumulative = values[0]
    goals = 0
    while cumulative < 1.0 - config.poisson_tail_tolerance or goals < config.minimum_max_goals:
        goals += 1
        if goals > config.maximum_max_goals:
            raise ScoreGridResearchError("Poisson support exceeded configured maximum")
        values.append(values[-1] * rate / goals)
        cumulative += values[-1]
    return [value / cumulative for value in values]


def _validate_rows_before_forbidden(
    rows: list[ScoreRatePrediction], config: ScoreGridResearchConfig
) -> None:
    if any(row.kickoff >= config.forbidden_kickoff_start for row in rows):
        raise ScoreGridResearchError("Opened final-test row entered score-grid research")


def _validate_config(config: ScoreGridResearchConfig) -> None:
    if (
        not config.model_version
        or not config.baseline_model_key
        or not config.moneyline_control_model_key
    ):
        raise ScoreGridResearchError(
            "Model, score baseline, and moneyline control versions are required"
        )
    if config.moneyline_control_model_key == config.baseline_model_key:
        raise ScoreGridResearchError(
            "Moneyline control and score baseline keys must differ"
        )
    if not (
        0
        < config.moneyline_control_temperature_minimum
        < 1
        < config.moneyline_control_temperature_maximum
        and config.moneyline_control_optimizer_tolerance > 0
        and config.moneyline_control_minimum_fit_fixtures > 0
    ):
        raise ScoreGridResearchError("Moneyline control configuration is invalid")
    if not 0 < config.poisson_tail_tolerance < 1:
        raise ScoreGridResearchError("Poisson tail tolerance must be in (0, 1)")
    if not 0 < config.probability_floor < 1:
        raise ScoreGridResearchError("Probability floor must be in (0, 1)")
    if not 0 <= config.minimum_max_goals < config.maximum_max_goals:
        raise ScoreGridResearchError("Score-grid support bounds are invalid")
    if config.bootstrap_replicates < 100:
        raise ScoreGridResearchError("Bootstrap requires at least 100 replicates")
    if not config.windows or not config.candidates:
        raise ScoreGridResearchError("Windows and candidates are required")
    window_keys = [window.window_key for window in config.windows]
    if len(window_keys) != len(set(window_keys)):
        raise ScoreGridResearchError("Window keys must be unique")
    for window in config.windows:
        if not (
            window.fit_start_inclusive
            < window.fit_end_exclusive
            <= window.validation_start_inclusive
            < window.validation_end_exclusive
            <= config.forbidden_kickoff_start
        ):
            raise ScoreGridResearchError(f"Unsafe research window: {window.window_key}")
    model_keys = [candidate.model_key for candidate in config.candidates]
    if len(model_keys) != len(set(model_keys)):
        raise ScoreGridResearchError("Candidate model keys must be unique")
    if {config.baseline_model_key, config.moneyline_control_model_key} & set(
        model_keys
    ):
        raise ScoreGridResearchError(
            "Candidate keys must differ from all control model keys"
        )
    for candidate in config.candidates:
        if candidate.minimum_fit_fixtures <= 0 or candidate.optimizer_tolerance <= 0:
            raise ScoreGridResearchError("Candidate fit controls must be positive")
        if candidate.family == "grid_temperature":
            if not (
                candidate.temperature_minimum is not None
                and candidate.temperature_maximum is not None
                and 0 < candidate.temperature_minimum < 1 < candidate.temperature_maximum
            ):
                raise ScoreGridResearchError("Temperature bounds must contain one")
        elif candidate.family == "exponential_tilt":
            if (
                not candidate.feature_names
                or len(candidate.feature_names) != len(candidate.feature_scales)
                or any(scale <= 0 for scale in candidate.feature_scales)
                or candidate.ridge_penalty is None
                or candidate.ridge_penalty <= 0
                or candidate.maximum_newton_iterations is None
                or candidate.maximum_newton_iterations <= 0
            ):
                raise ScoreGridResearchError("Tilt candidate configuration is invalid")
        else:
            raise ScoreGridResearchError(f"Unsupported family: {candidate.family}")
    supported_metrics = set(METRIC_FIELDS)
    required_metrics = {
        config.selection_primary_metric,
        *config.selection_tie_break_metrics,
        *config.confirmation_nonpositive_metrics,
        "exact_score_log_loss",
        "moneyline_log_loss",
    }
    if not required_metrics <= supported_metrics:
        raise ScoreGridResearchError("Research policy names an unsupported metric")


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[i][:] + [vector[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ScoreGridResearchError("Singular score-grid information matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(
                    augmented[row], augmented[column], strict=True
                )
            ]
    return [augmented[index][-1] for index in range(size)]


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"
