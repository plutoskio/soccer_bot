from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Protocol

from soccer_bot.config import load_json
from soccer_bot.contracts import ScoreGrid


RESULTS = ("home_win", "draw", "away_win")


class ScoreGridShadowError(RuntimeError):
    """Raised when a prospective coherent score-grid operation is unsafe."""


class HistoricalRateRow(Protocol):
    fixture_id: str
    information_state: str
    kickoff: datetime
    home_goals: int
    away_goals: int
    expected_home_goals: float
    expected_away_goals: float


@dataclass(frozen=True)
class ScoreGridShadowConfig:
    model_version: str
    status: str
    parent_moneyline_model_version: str
    model_family: str
    recipe_frozen_at: datetime
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    poisson_tail_tolerance: float
    minimum_max_goals: int
    maximum_max_goals: int
    normalization_tolerance: float
    feature_names: tuple[str, ...]
    feature_scales: tuple[float, ...]
    ridge_penalty: float
    maximum_newton_iterations: int
    optimizer_tolerance: float
    minimum_fit_fixtures: int
    minimum_complete_calendar_month_blocks: int


@dataclass(frozen=True)
class ShadowHorizonParameters:
    information_state: str
    training_fixtures: int
    training_kickoff_start: datetime
    training_kickoff_end_exclusive: datetime
    feature_names: tuple[str, ...]
    feature_scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    ridge_penalty: float
    converged: bool
    iterations: int
    penalized_objective: float


@dataclass(frozen=True)
class RegulationScoreGridShadowModel:
    model_version: str
    status: str
    parent_moneyline_model_version: str
    model_family: str
    recipe_frozen_at: datetime
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    poisson_tail_tolerance: float
    minimum_max_goals: int
    maximum_max_goals: int
    normalization_tolerance: float
    horizons: tuple[ShadowHorizonParameters, ...]


@dataclass(frozen=True)
class _PreparedConditionalRow:
    fixture_id: str
    observed_score: tuple[int, int]
    observed_result: str
    home_probabilities: tuple[float, ...]
    away_probabilities: tuple[float, ...]


def load_score_grid_shadow_config(path: Path) -> ScoreGridShadowConfig:
    raw = load_json(path)
    expected_status = "frozen_prospective_shadow_no_retrospective_promotion_claim"
    if raw.get("status") != expected_status:
        raise ScoreGridShadowError("Score-grid v3 must be prospective shadow only")
    if raw.get("model_family") != (
        "result_marginal_preserving_exponential_tilt"
    ):
        raise ScoreGridShadowError("Unsupported score-grid shadow family")
    invariants = raw.get("invariants", {})
    for key in (
        "preserve_parent_moneyline_exactly",
        "all_score_cells_strictly_positive",
        "all_contracts_derived_from_one_score_grid",
        "extra_time_and_penalty_shootout_excluded",
    ):
        if invariants.get(key) is not True:
            raise ScoreGridShadowError(f"Required invariant is not enabled: {key}")
    evaluation = raw.get("evaluation", {})
    if evaluation.get("mode") != "prospective_only":
        raise ScoreGridShadowError("Score-grid v3 evaluation must be prospective")
    if evaluation.get("promotion_requires_new_written_gate_before_outcomes_are_scored") is not True:
        raise ScoreGridShadowError("Prospective promotion gate is not frozen")
    source_policy = raw.get("source_policy", {})
    if source_policy.get("market_data_allowed") is not False:
        raise ScoreGridShadowError("Market data cannot enter score-grid fitting")
    support = raw.get("support", {})
    tilt = raw.get("conditional_tilt", {})
    feature_names = tuple(tilt.get("features", ()))
    scales = tilt.get("feature_scales", {})
    config = ScoreGridShadowConfig(
        model_version=str(raw.get("model_version", "")),
        status=str(raw.get("status", "")),
        parent_moneyline_model_version=str(
            raw.get("parent_moneyline_model_version", "")
        ),
        model_family=str(raw.get("model_family", "")),
        recipe_frozen_at=_aware_datetime(raw["recipe_frozen_at"]),
        training_kickoff_end_exclusive=_aware_datetime(
            raw["training_kickoff_end_exclusive"]
        ),
        prospective_holdout_start=_aware_datetime(
            raw["prospective_holdout_kickoff_start_inclusive"]
        ),
        poisson_tail_tolerance=float(support["poisson_tail_tolerance"]),
        minimum_max_goals=int(support["minimum_max_goals_per_team"]),
        maximum_max_goals=int(support["maximum_max_goals_per_team"]),
        normalization_tolerance=float(support["normalization_tolerance"]),
        feature_names=feature_names,
        feature_scales=tuple(float(scales[name]) for name in feature_names),
        ridge_penalty=float(tilt["ridge_penalty"]),
        maximum_newton_iterations=int(tilt["maximum_newton_iterations"]),
        optimizer_tolerance=float(tilt["optimizer_tolerance"]),
        minimum_fit_fixtures=int(tilt["minimum_fit_fixtures_per_horizon"]),
        minimum_complete_calendar_month_blocks=int(
            evaluation["minimum_complete_calendar_month_blocks"]
        ),
    )
    _validate_config(config)
    return config


def load_score_grid_prospective_gate(
    path: Path, *, model: RegulationScoreGridShadowModel | None = None
) -> dict:
    raw = load_json(path)
    if raw.get("status") != "frozen_before_first_eligible_shadow_prediction":
        raise ScoreGridShadowError("Prospective score-grid gate is not frozen")
    if raw.get("baseline") != (
        "parent_moneyline_preserving_independent_poisson_conditionals"
    ):
        raise ScoreGridShadowError("Prospective score-grid baseline is invalid")
    if raw.get("pairing_key") != ["fixture_id", "information_state"]:
        raise ScoreGridShadowError("Prospective score-grid pairing key is invalid")
    evidence = raw.get("minimum_evidence", {})
    if (
        int(evidence.get("complete_calendar_month_blocks", 0)) < 2
        or int(evidence.get("fixtures_per_horizon", 0)) <= 0
        or int(evidence.get("minimum_competitions_per_horizon", 0)) <= 0
    ):
        raise ScoreGridShadowError("Prospective minimum evidence is invalid")
    primary = raw.get("primary_gate", {})
    if (
        primary.get("metric") != "exact_score_log_loss"
        or primary.get("require_negative_mean_delta_each_horizon") is not True
        or primary.get(
            "require_paired_month_block_bootstrap_95_upper_below_zero_each_horizon"
        )
        is not True
    ):
        raise ScoreGridShadowError("Prospective primary gate is invalid")
    integrity = raw.get("evidence_integrity", {})
    for key in (
        "snapshot_as_of_must_be_at_or_after_recipe_freeze",
        "snapshot_creation_must_be_strictly_before_kickoff",
        "prediction_and_model_hashes_required",
        "outcomes_joined_only_after_prediction_artifacts_are_immutable",
        "no_parameter_or_gate_changes_before_decision",
        "retrospective_rows_before_holdout_are_ineligible",
    ):
        if integrity.get(key) is not True:
            raise ScoreGridShadowError(f"Prospective integrity rule missing: {key}")
    uncertainty = raw.get("uncertainty", {})
    if (
        uncertainty.get("paired_block_unit") != "calendar_month"
        or int(uncertainty.get("bootstrap_replicates", 0)) < 100
    ):
        raise ScoreGridShadowError("Prospective uncertainty policy is invalid")
    holdout_start = _aware_datetime(
        raw["prospective_holdout_kickoff_start_inclusive"]
    )
    if model is not None and (
        raw.get("model_version") != model.model_version
        or holdout_start != model.prospective_holdout_start
    ):
        raise ScoreGridShadowError("Prospective gate and shadow model differ")
    return raw


def fit_score_grid_shadow(
    rows: list[HistoricalRateRow], config: ScoreGridShadowConfig
) -> RegulationScoreGridShadowModel:
    if not rows:
        raise ScoreGridShadowError("Score-grid shadow fit requires rows")
    if any(row.kickoff >= config.training_kickoff_end_exclusive for row in rows):
        raise ScoreGridShadowError("Post-training-cutoff row entered shadow fit")
    grouped: dict[str, list[HistoricalRateRow]] = defaultdict(list)
    keys = set()
    for row in rows:
        key = (row.fixture_id, row.information_state)
        if key in keys:
            raise ScoreGridShadowError(f"Duplicate fit row: {key}")
        keys.add(key)
        grouped[row.information_state].append(row)
    horizons = []
    for information_state, values in sorted(grouped.items()):
        if len(values) < config.minimum_fit_fixtures:
            raise ScoreGridShadowError(
                f"Insufficient {information_state} rows: {len(values)}"
            )
        prepared = [_prepare(row, config) for row in values]
        coefficients, converged, iterations, objective = _fit_conditional_tilt(
            prepared, config
        )
        if not converged:
            raise ScoreGridShadowError(
                "Conditional tilt did not converge: "
                f"{information_state}; iterations={iterations}; "
                f"objective={objective:.12g}; coefficients={coefficients}"
            )
        horizons.append(
            ShadowHorizonParameters(
                information_state=information_state,
                training_fixtures=len(values),
                training_kickoff_start=min(row.kickoff for row in values),
                training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
                feature_names=config.feature_names,
                feature_scales=config.feature_scales,
                coefficients=coefficients,
                ridge_penalty=config.ridge_penalty,
                converged=True,
                iterations=iterations,
                penalized_objective=objective,
            )
        )
    return RegulationScoreGridShadowModel(
        model_version=config.model_version,
        status=config.status,
        parent_moneyline_model_version=config.parent_moneyline_model_version,
        model_family=config.model_family,
        recipe_frozen_at=config.recipe_frozen_at,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        prospective_holdout_start=config.prospective_holdout_start,
        poisson_tail_tolerance=config.poisson_tail_tolerance,
        minimum_max_goals=config.minimum_max_goals,
        maximum_max_goals=config.maximum_max_goals,
        normalization_tolerance=config.normalization_tolerance,
        horizons=tuple(horizons),
    )


def predict_coherent_score_grid(
    *,
    expected_home_goals: float,
    expected_away_goals: float,
    parent_moneyline: Mapping[str, float],
    information_state: str,
    model: RegulationScoreGridShadowModel,
) -> ScoreGrid:
    return _predict_parent_preserving_grid(
        expected_home_goals=expected_home_goals,
        expected_away_goals=expected_away_goals,
        parent_moneyline=parent_moneyline,
        information_state=information_state,
        model=model,
        apply_shadow_tilt=True,
    )


def predict_parent_preserving_poisson_grid(
    *,
    expected_home_goals: float,
    expected_away_goals: float,
    parent_moneyline: Mapping[str, float],
    information_state: str,
    model: RegulationScoreGridShadowModel,
) -> ScoreGrid:
    """Return the frozen gate's parent-preserving Poisson conditional baseline."""

    return _predict_parent_preserving_grid(
        expected_home_goals=expected_home_goals,
        expected_away_goals=expected_away_goals,
        parent_moneyline=parent_moneyline,
        information_state=information_state,
        model=model,
        apply_shadow_tilt=False,
    )


def _predict_parent_preserving_grid(
    *,
    expected_home_goals: float,
    expected_away_goals: float,
    parent_moneyline: Mapping[str, float],
    information_state: str,
    model: RegulationScoreGridShadowModel,
    apply_shadow_tilt: bool,
) -> ScoreGrid:
    if model.status != "frozen_prospective_shadow_no_retrospective_promotion_claim":
        raise ScoreGridShadowError("Only the frozen prospective shadow may predict")
    matches = [
        horizon
        for horizon in model.horizons
        if horizon.information_state == information_state
    ]
    if len(matches) != 1:
        raise ScoreGridShadowError(
            f"Unsupported or duplicate shadow horizon: {information_state}"
        )
    horizon = matches[0]
    targets = _validate_moneyline(parent_moneyline)
    base = _poisson_score_grid(
        expected_home_goals,
        expected_away_goals,
        tail_tolerance=model.poisson_tail_tolerance,
        minimum_max_goals=model.minimum_max_goals,
        maximum_max_goals=model.maximum_max_goals,
    )
    log_weights: dict[tuple[int, int], float] = {}
    by_result: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for score, probability in base.items():
        result = score_result(score)
        by_result[result].append(score)
        features = _score_features(
            score, horizon.feature_names, horizon.feature_scales
        )
        tilt = (
            math.fsum(
                coefficient * feature
                for coefficient, feature in zip(
                    horizon.coefficients, features, strict=True
                )
            )
            if apply_shadow_tilt
            else 0.0
        )
        log_weights[score] = math.log(probability) + tilt
    output: dict[tuple[int, int], float] = {}
    for result in RESULTS:
        scores = by_result[result]
        maximum = max(log_weights[score] for score in scores)
        weights = {
            score: math.exp(log_weights[score] - maximum) for score in scores
        }
        total = math.fsum(weights.values())
        for score, weight in weights.items():
            output[score] = targets[result] * weight / total
    _validate_grid(output, targets, model.normalization_tolerance)
    return ScoreGrid(output, tolerance=model.normalization_tolerance)


def score_grid_shadow_sha256(model: RegulationScoreGridShadowModel) -> str:
    return hashlib.sha256(_model_json(model).encode("utf-8")).hexdigest()


def dump_score_grid_shadow_model(
    model: RegulationScoreGridShadowModel, path: Path, *, created_at: datetime
) -> None:
    value = {
        "artifact_version": "regulation_score_grid_shadow_model_v1",
        "created_at": created_at.astimezone(timezone.utc).isoformat(),
        "logical_model_sha256": score_grid_shadow_sha256(model),
        "model": _model_value(model),
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_score_grid_shadow_model(path: Path) -> RegulationScoreGridShadowModel:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("artifact_version") != "regulation_score_grid_shadow_model_v1":
        raise ScoreGridShadowError("Unknown score-grid shadow artifact")
    value = raw.get("model")
    if not isinstance(value, dict):
        raise ScoreGridShadowError("Shadow artifact has no model")
    horizons = []
    for item in value.get("horizons", []):
        horizons.append(
            ShadowHorizonParameters(
                information_state=str(item["information_state"]),
                training_fixtures=int(item["training_fixtures"]),
                training_kickoff_start=_aware_datetime(
                    item["training_kickoff_start"]
                ),
                training_kickoff_end_exclusive=_aware_datetime(
                    item["training_kickoff_end_exclusive"]
                ),
                feature_names=tuple(item["feature_names"]),
                feature_scales=tuple(float(x) for x in item["feature_scales"]),
                coefficients=tuple(float(x) for x in item["coefficients"]),
                ridge_penalty=float(item["ridge_penalty"]),
                converged=bool(item["converged"]),
                iterations=int(item["iterations"]),
                penalized_objective=float(item["penalized_objective"]),
            )
        )
    model = RegulationScoreGridShadowModel(
        model_version=str(value["model_version"]),
        status=str(value["status"]),
        parent_moneyline_model_version=str(
            value["parent_moneyline_model_version"]
        ),
        model_family=str(value["model_family"]),
        recipe_frozen_at=_aware_datetime(value["recipe_frozen_at"]),
        training_kickoff_end_exclusive=_aware_datetime(
            value["training_kickoff_end_exclusive"]
        ),
        prospective_holdout_start=_aware_datetime(
            value["prospective_holdout_start"]
        ),
        poisson_tail_tolerance=float(value["poisson_tail_tolerance"]),
        minimum_max_goals=int(value["minimum_max_goals"]),
        maximum_max_goals=int(value["maximum_max_goals"]),
        normalization_tolerance=float(value["normalization_tolerance"]),
        horizons=tuple(horizons),
    )
    if raw.get("logical_model_sha256") != score_grid_shadow_sha256(model):
        raise ScoreGridShadowError("Shadow model logical hash mismatch")
    if not model.horizons or any(not item.converged for item in model.horizons):
        raise ScoreGridShadowError("Shadow artifact contains an invalid fit")
    return model


def score_result(score: tuple[int, int]) -> str:
    home_goals, away_goals = score
    if home_goals > away_goals:
        return "home_win"
    if home_goals == away_goals:
        return "draw"
    return "away_win"


def _prepare(
    row: HistoricalRateRow, config: ScoreGridShadowConfig
) -> _PreparedConditionalRow:
    if row.home_goals < 0 or row.away_goals < 0:
        raise ScoreGridShadowError("Observed scores must be nonnegative")
    home = tuple(
        _poisson_marginal(
            row.expected_home_goals,
            config.poisson_tail_tolerance,
            config.minimum_max_goals,
            config.maximum_max_goals,
        )
    )
    away = tuple(
        _poisson_marginal(
            row.expected_away_goals,
            config.poisson_tail_tolerance,
            config.minimum_max_goals,
            config.maximum_max_goals,
        )
    )
    observed = (row.home_goals, row.away_goals)
    if observed[0] >= len(home) or observed[1] >= len(away):
        raise ScoreGridShadowError(
            f"Observed score outside configured support: {row.fixture_id}"
        )
    return _PreparedConditionalRow(
        fixture_id=row.fixture_id,
        observed_score=observed,
        observed_result=score_result(observed),
        home_probabilities=home,
        away_probabilities=away,
    )


def _fit_conditional_tilt(
    rows: list[_PreparedConditionalRow], config: ScoreGridShadowConfig
) -> tuple[tuple[float, ...], bool, int, float]:
    size = len(config.feature_names)
    theta = [0.0] * size
    cache = _feature_cache(rows, config.feature_names, config.feature_scales)
    for iteration in range(1, config.maximum_newton_iterations + 1):
        gradient = [-config.ridge_penalty * value for value in theta]
        information = [
            [config.ridge_penalty if i == j else 0.0 for j in range(size)]
            for i in range(size)
        ]
        for row in rows:
            _, means, covariance = _conditional_moments(
                row, theta, cache, need_covariance=True
            )
            observed = cache[row.observed_score]
            for i in range(size):
                gradient[i] += observed[i] - means[i]
                for j in range(size):
                    information[i][j] += covariance[i][j]
        average_score_norm = max(abs(value) for value in gradient) / len(rows)
        if average_score_norm < config.optimizer_tolerance:
            return (
                tuple(theta),
                True,
                iteration,
                _objective(rows, theta, config.ridge_penalty, cache),
            )
        step = _solve(information, gradient)
        if max(abs(value) for value in step) < config.optimizer_tolerance:
            return (
                tuple(theta),
                True,
                iteration,
                _objective(rows, theta, config.ridge_penalty, cache),
            )
        current = _objective(rows, theta, config.ridge_penalty, cache)
        scale = 1.0
        while scale >= 1e-6:
            proposed = [
                value + scale * change
                for value, change in zip(theta, step, strict=True)
            ]
            proposed_objective = _objective(
                rows, proposed, config.ridge_penalty, cache
            )
            if proposed_objective < current:
                theta = proposed
                break
            scale *= 0.5
        else:
            numerically_stationary = average_score_norm < max(
                1e-7, 10.0 * config.optimizer_tolerance
            )
            return tuple(theta), numerically_stationary, iteration, current
    return (
        tuple(theta),
        False,
        config.maximum_newton_iterations,
        _objective(rows, theta, config.ridge_penalty, cache),
    )


def _objective(
    rows: list[_PreparedConditionalRow],
    theta: list[float],
    ridge_penalty: float,
    cache: dict[tuple[int, int], tuple[float, ...]],
) -> float:
    value = 0.5 * ridge_penalty * math.fsum(item * item for item in theta)
    for row in rows:
        log_z, _, _ = _conditional_moments(
            row, theta, cache, need_covariance=False
        )
        observed_features = cache[row.observed_score]
        value += log_z - math.log(
            row.home_probabilities[row.observed_score[0]]
            * row.away_probabilities[row.observed_score[1]]
        )
        value -= math.fsum(
            coefficient * feature
            for coefficient, feature in zip(
                theta, observed_features, strict=True
            )
        )
    return value


def _conditional_moments(
    row: _PreparedConditionalRow,
    theta: list[float],
    cache: dict[tuple[int, int], tuple[float, ...]],
    *,
    need_covariance: bool,
) -> tuple[float, list[float], list[list[float]]]:
    weighted = []
    maximum = -math.inf
    for home_goals, home_probability in enumerate(row.home_probabilities):
        for away_goals, away_probability in enumerate(row.away_probabilities):
            score = (home_goals, away_goals)
            if score_result(score) != row.observed_result:
                continue
            features = cache[score]
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
            means[i] += weight * features[i] / total
    covariance = [[0.0] * size for _ in range(size)]
    if need_covariance:
        for weight, (_, features) in zip(weights, weighted, strict=True):
            probability = weight / total
            for i in range(size):
                for j in range(size):
                    covariance[i][j] += probability * (
                        features[i] - means[i]
                    ) * (features[j] - means[j])
    return maximum + math.log(total), means, covariance


def _feature_cache(
    rows: list[_PreparedConditionalRow],
    names: tuple[str, ...],
    scales: tuple[float, ...],
) -> dict[tuple[int, int], tuple[float, ...]]:
    maximum_home = max(len(row.home_probabilities) for row in rows)
    maximum_away = max(len(row.away_probabilities) for row in rows)
    return {
        (home_goals, away_goals): _score_features(
            (home_goals, away_goals), names, scales
        )
        for home_goals in range(maximum_home)
        for away_goals in range(maximum_away)
    }


def _score_features(
    score: tuple[int, int],
    names: tuple[str, ...],
    scales: tuple[float, ...],
) -> tuple[float, ...]:
    home_goals, away_goals = score
    raw = {
        "home_goals": float(home_goals),
        "away_goals": float(away_goals),
        "log_factorial_sum": math.lgamma(home_goals + 1)
        + math.lgamma(away_goals + 1),
        "zero_zero": float(home_goals == away_goals == 0),
        "both_teams_to_score": float(home_goals > 0 and away_goals > 0),
    }
    try:
        return tuple(
            raw[name] / scale
            for name, scale in zip(names, scales, strict=True)
        )
    except KeyError as error:
        raise ScoreGridShadowError(
            f"Unsupported conditional tilt feature: {error.args[0]}"
        ) from None


def _poisson_score_grid(
    home_rate: float,
    away_rate: float,
    *,
    tail_tolerance: float,
    minimum_max_goals: int,
    maximum_max_goals: int,
) -> dict[tuple[int, int], float]:
    home = _poisson_marginal(
        home_rate, tail_tolerance, minimum_max_goals, maximum_max_goals
    )
    away = _poisson_marginal(
        away_rate, tail_tolerance, minimum_max_goals, maximum_max_goals
    )
    return {
        (home_goals, away_goals): home_probability * away_probability
        for home_goals, home_probability in enumerate(home)
        for away_goals, away_probability in enumerate(away)
    }


def _poisson_marginal(
    rate: float,
    tail_tolerance: float,
    minimum_max_goals: int,
    maximum_max_goals: int,
) -> list[float]:
    if not math.isfinite(rate) or rate <= 0:
        raise ScoreGridShadowError("Poisson rate must be finite and positive")
    values = [math.exp(-rate)]
    cumulative = values[0]
    goals = 0
    while cumulative < 1.0 - tail_tolerance or goals < minimum_max_goals:
        goals += 1
        if goals > maximum_max_goals:
            raise ScoreGridShadowError("Poisson support exceeded safety maximum")
        values.append(values[-1] * rate / goals)
        cumulative += values[-1]
    return [value / cumulative for value in values]


def _validate_moneyline(value: Mapping[str, float]) -> dict[str, float]:
    if set(value) != set(RESULTS):
        raise ScoreGridShadowError("Parent moneyline must have home/draw/away")
    result = {key: float(value[key]) for key in RESULTS}
    if any(not math.isfinite(item) or item <= 0 for item in result.values()):
        raise ScoreGridShadowError(
            "Parent moneyline probabilities must be finite and positive"
        )
    if not math.isclose(math.fsum(result.values()), 1.0, abs_tol=1e-10):
        raise ScoreGridShadowError("Parent moneyline must sum to one")
    return result


def _validate_grid(
    grid: dict[tuple[int, int], float],
    targets: dict[str, float],
    tolerance: float,
) -> None:
    if any(not math.isfinite(value) or value <= 0 for value in grid.values()):
        raise ScoreGridShadowError("All shadow score cells must be positive")
    if not math.isclose(math.fsum(grid.values()), 1.0, abs_tol=tolerance):
        raise ScoreGridShadowError("Shadow score grid is not normalized")
    implied = {result: 0.0 for result in RESULTS}
    for score, probability in grid.items():
        implied[score_result(score)] += probability
    for result in RESULTS:
        if not math.isclose(implied[result], targets[result], abs_tol=tolerance):
            raise ScoreGridShadowError(
                f"Shadow grid changed parent moneyline probability: {result}"
            )


def _validate_config(config: ScoreGridShadowConfig) -> None:
    if not config.model_version or not config.parent_moneyline_model_version:
        raise ScoreGridShadowError("Shadow and parent model versions are required")
    if not (
        config.training_kickoff_end_exclusive
        <= config.recipe_frozen_at
        <= config.prospective_holdout_start
    ):
        raise ScoreGridShadowError("Shadow chronology is invalid")
    if not 0 < config.poisson_tail_tolerance < 1:
        raise ScoreGridShadowError("Poisson tail tolerance is invalid")
    if not 0 < config.normalization_tolerance < 1e-6:
        raise ScoreGridShadowError("Normalization tolerance is invalid")
    if not 0 <= config.minimum_max_goals < config.maximum_max_goals:
        raise ScoreGridShadowError("Score support is invalid")
    if (
        not config.feature_names
        or len(config.feature_names) != len(config.feature_scales)
        or any(scale <= 0 for scale in config.feature_scales)
    ):
        raise ScoreGridShadowError("Conditional features are invalid")
    if "draw" in config.feature_names:
        raise ScoreGridShadowError(
            "A draw indicator is unidentified inside result-conditional blocks"
        )
    if (
        config.ridge_penalty <= 0
        or config.maximum_newton_iterations <= 0
        or config.optimizer_tolerance <= 0
        or config.minimum_fit_fixtures <= 0
        or config.minimum_complete_calendar_month_blocks < 2
    ):
        raise ScoreGridShadowError("Shadow fit/evaluation controls are invalid")


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[i][:] + [vector[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ScoreGridShadowError("Singular conditional information matrix")
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


def _model_value(model: RegulationScoreGridShadowModel) -> dict:
    value = asdict(model)
    for key in (
        "recipe_frozen_at",
        "training_kickoff_end_exclusive",
        "prospective_holdout_start",
    ):
        value[key] = getattr(model, key).astimezone(timezone.utc).isoformat()
    for item, horizon in zip(value["horizons"], model.horizons, strict=True):
        item["training_kickoff_start"] = horizon.training_kickoff_start.astimezone(
            timezone.utc
        ).isoformat()
        item["training_kickoff_end_exclusive"] = (
            horizon.training_kickoff_end_exclusive.astimezone(timezone.utc).isoformat()
        )
    return value


def _model_json(model: RegulationScoreGridShadowModel) -> str:
    return json.dumps(
        _model_value(model), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _aware_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ScoreGridShadowError("Shadow timestamps must be timezone-aware")
    return result
