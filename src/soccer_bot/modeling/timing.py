from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json


OUTCOMES = ("home_first", "away_first", "no_goal")


class FirstScoreModelError(RuntimeError):
    """Raised when first-score fitting or inference is not safe."""


@dataclass(frozen=True)
class FirstScoreObservation:
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    outcome: str
    expected_home_goals: float
    expected_away_goals: float


@dataclass(frozen=True)
class FirstScoreConfig:
    model_version: str
    status: str
    recipe_frozen_at: datetime
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    information_states: tuple[str, ...]
    probability_floor: float
    temperature_minimum: float
    temperature_maximum: float
    bias_minimum: float
    bias_maximum: float
    bias_ridge_penalty: float
    optimizer_tolerance: float
    maximum_coordinate_cycles: int
    minimum_fit_fixtures: int
    bootstrap_replicates: int
    bootstrap_seed: int


@dataclass(frozen=True)
class FirstScoreHorizonFit:
    information_state: str
    training_fixtures: int
    training_kickoff_start: datetime
    training_kickoff_end_exclusive: datetime
    temperature: float
    home_bias: float
    away_bias: float
    mean_log_loss_before: float
    mean_log_loss_after: float
    coordinate_cycles: int
    converged: bool


@dataclass(frozen=True)
class FirstScoreTimingModel:
    model_version: str
    status: str
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    horizons: tuple[FirstScoreHorizonFit, ...]


def load_first_score_config(path: Path) -> FirstScoreConfig:
    raw = load_json(path)
    if raw.get("parameter_status") != (
        "team_target_recipe_and_gate_frozen_player_target_not_authorized"
    ):
        raise FirstScoreModelError("First-score team recipe is not frozen")
    baseline = raw.get("baseline")
    candidate = raw.get("candidate")
    evaluation = raw.get("evaluation")
    if not all(isinstance(value, dict) for value in (baseline, candidate, evaluation)):
        raise FirstScoreModelError("First-score configuration sections are missing")
    if baseline.get("team_outcomes") != list(OUTCOMES):
        raise FirstScoreModelError("First-score outcomes changed")
    if candidate.get("model_family") != "competing_risk_vector_calibration":
        raise FirstScoreModelError("Unsupported first-score candidate")
    config = FirstScoreConfig(
        model_version=_string(raw, "model_version"),
        status=_string(raw, "research_status"),
        recipe_frozen_at=_timestamp(raw.get("recipe_frozen_at"), "recipe_frozen_at"),
        training_kickoff_end_exclusive=_timestamp(
            raw.get("training_kickoff_end_exclusive"),
            "training_kickoff_end_exclusive",
        ),
        prospective_holdout_start=_timestamp(
            raw.get("prospective_holdout_kickoff_start_inclusive"),
            "prospective_holdout_kickoff_start_inclusive",
        ),
        information_states=tuple(str(value) for value in raw["information_states"][:2]),
        probability_floor=_positive_float(candidate, "probability_floor"),
        temperature_minimum=_positive_float(candidate, "temperature_minimum"),
        temperature_maximum=_positive_float(candidate, "temperature_maximum"),
        bias_minimum=float(candidate["class_bias_minimum"]),
        bias_maximum=float(candidate["class_bias_maximum"]),
        bias_ridge_penalty=_positive_float(candidate, "class_bias_ridge_penalty"),
        optimizer_tolerance=_positive_float(candidate, "optimizer_tolerance"),
        maximum_coordinate_cycles=_positive_int(
            candidate, "maximum_coordinate_cycles"
        ),
        minimum_fit_fixtures=_positive_int(
            candidate, "minimum_fit_fixtures_per_horizon"
        ),
        bootstrap_replicates=_positive_int(evaluation, "bootstrap_replicates"),
        bootstrap_seed=_positive_int(evaluation, "bootstrap_seed"),
    )
    if config.recipe_frozen_at >= config.prospective_holdout_start:
        raise FirstScoreModelError("Recipe must freeze before prospective scoring")
    if config.training_kickoff_end_exclusive > config.recipe_frozen_at:
        raise FirstScoreModelError("Training cutoff is after recipe freeze")
    if config.temperature_minimum >= config.temperature_maximum:
        raise FirstScoreModelError("Invalid temperature bounds")
    if config.bias_minimum >= config.bias_maximum:
        raise FirstScoreModelError("Invalid bias bounds")
    if not 0 < config.probability_floor < 1 / 3:
        raise FirstScoreModelError("Invalid probability floor")
    if config.information_states != (
        "pre_lineup_72h_clean_v1",
        "pre_lineup_24h_v1",
    ):
        raise FirstScoreModelError("First-score pre-lineup horizons changed")
    return config


def baseline_first_team_probabilities(
    expected_home_goals: float,
    expected_away_goals: float,
) -> dict[str, float]:
    """Convert regulation goal rates into a homogeneous goal-race baseline."""

    if not _positive_finite(expected_home_goals) or not _positive_finite(
        expected_away_goals
    ):
        raise FirstScoreModelError("Expected goal rates must be positive and finite")
    total = expected_home_goals + expected_away_goals
    no_goal = math.exp(-total)
    scoring = 1.0 - no_goal
    return {
        "home_first": scoring * expected_home_goals / total,
        "away_first": scoring * expected_away_goals / total,
        "no_goal": no_goal,
    }


def fit_first_score_timing_model(
    observations: list[FirstScoreObservation],
    config: FirstScoreConfig,
) -> FirstScoreTimingModel:
    grouped: dict[str, list[FirstScoreObservation]] = defaultdict(list)
    seen = set()
    for row in observations:
        _validate_observation(row)
        if row.kickoff >= config.training_kickoff_end_exclusive:
            continue
        key = (row.fixture_id, row.information_state)
        if key in seen:
            raise FirstScoreModelError(f"Duplicate first-score row: {key}")
        seen.add(key)
        grouped[row.information_state].append(row)
    if set(grouped) != set(config.information_states):
        raise FirstScoreModelError("First-score fit does not cover both horizons")
    fits = tuple(
        _fit_horizon(information_state, rows, config)
        for information_state, rows in sorted(grouped.items())
    )
    return FirstScoreTimingModel(
        model_version=config.model_version,
        status=config.status,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        prospective_holdout_start=config.prospective_holdout_start,
        horizons=fits,
    )


def first_team_probabilities(
    model: FirstScoreTimingModel,
    *,
    information_state: str,
    expected_home_goals: float,
    expected_away_goals: float,
) -> dict[str, float]:
    fit = next(
        (row for row in model.horizons if row.information_state == information_state),
        None,
    )
    if fit is None:
        raise FirstScoreModelError(f"Unsupported information state: {information_state}")
    baseline = baseline_first_team_probabilities(
        expected_home_goals, expected_away_goals
    )
    return _calibrate(
        baseline,
        temperature=fit.temperature,
        home_bias=fit.home_bias,
        away_bias=fit.away_bias,
    )


def first_score_model_sha256(model: FirstScoreTimingModel) -> str:
    encoded = json.dumps(
        _model_value(model), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dump_first_score_model(
    model: FirstScoreTimingModel, path: Path, *, created_at: datetime
) -> None:
    value = {
        "artifact_version": "first_score_timing_model_v1",
        "created_at": created_at.isoformat(),
        "logical_model_sha256": first_score_model_sha256(model),
        "model": _model_value(model),
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_first_score_model(path: Path) -> FirstScoreTimingModel:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("artifact_version") != "first_score_timing_model_v1":
        raise FirstScoreModelError("Unsupported first-score artifact")
    raw = dict(value.get("model", {}))
    try:
        horizons = tuple(
            FirstScoreHorizonFit(
                **{
                    **item,
                    "training_kickoff_start": _timestamp(
                        item["training_kickoff_start"], "training_kickoff_start"
                    ),
                    "training_kickoff_end_exclusive": _timestamp(
                        item["training_kickoff_end_exclusive"],
                        "training_kickoff_end_exclusive",
                    ),
                }
            )
            for item in raw.pop("horizons")
        )
        model = FirstScoreTimingModel(
            **{
                **raw,
                "training_kickoff_end_exclusive": _timestamp(
                    raw["training_kickoff_end_exclusive"],
                    "training_kickoff_end_exclusive",
                ),
                "prospective_holdout_start": _timestamp(
                    raw["prospective_holdout_start"], "prospective_holdout_start"
                ),
                "horizons": horizons,
            }
        )
    except (KeyError, TypeError) as error:
        raise FirstScoreModelError("Malformed first-score artifact") from error
    if value.get("logical_model_sha256") != first_score_model_sha256(model):
        raise FirstScoreModelError("First-score model logical hash mismatch")
    return model


def _fit_horizon(
    information_state: str,
    rows: list[FirstScoreObservation],
    config: FirstScoreConfig,
) -> FirstScoreHorizonFit:
    ordered = sorted(rows, key=lambda row: (row.kickoff, row.fixture_id))
    if len(ordered) < config.minimum_fit_fixtures:
        raise FirstScoreModelError(
            f"Insufficient {information_state} rows: {len(ordered)}"
        )
    baselines = [
        baseline_first_team_probabilities(
            row.expected_home_goals, row.expected_away_goals
        )
        for row in ordered
    ]
    parameters = [1.0, 0.0, 0.0]
    bounds = [
        (config.temperature_minimum, config.temperature_maximum),
        (config.bias_minimum, config.bias_maximum),
        (config.bias_minimum, config.bias_maximum),
    ]

    def objective(values: list[float]) -> float:
        temperature, home_bias, away_bias = values
        loss = math.fsum(
            -math.log(
                max(
                    _calibrate(
                        baseline,
                        temperature=temperature,
                        home_bias=home_bias,
                        away_bias=away_bias,
                    )[row.outcome],
                    config.probability_floor,
                )
            )
            for baseline, row in zip(baselines, ordered, strict=True)
        ) / len(ordered)
        return loss + config.bias_ridge_penalty * (
            home_bias * home_bias + away_bias * away_bias
        )

    previous = objective(parameters)
    converged = False
    completed_cycles = 0
    for cycle in range(1, config.maximum_coordinate_cycles + 1):
        for coordinate, (lower, upper) in enumerate(bounds):
            parameters[coordinate] = _golden_coordinate(
                objective,
                parameters,
                coordinate,
                lower,
                upper,
                config.optimizer_tolerance,
            )
        current = objective(parameters)
        completed_cycles = cycle
        if previous - current < config.optimizer_tolerance:
            converged = True
            break
        previous = current
    if not converged:
        raise FirstScoreModelError(f"First-score optimizer did not converge: {information_state}")
    before = math.fsum(-math.log(baseline[row.outcome]) for baseline, row in zip(baselines, ordered, strict=True)) / len(ordered)
    after = math.fsum(
        -math.log(
            _calibrate(
                baseline,
                temperature=parameters[0],
                home_bias=parameters[1],
                away_bias=parameters[2],
            )[row.outcome]
        )
        for baseline, row in zip(baselines, ordered, strict=True)
    ) / len(ordered)
    return FirstScoreHorizonFit(
        information_state=information_state,
        training_fixtures=len(ordered),
        training_kickoff_start=ordered[0].kickoff,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        temperature=parameters[0],
        home_bias=parameters[1],
        away_bias=parameters[2],
        mean_log_loss_before=before,
        mean_log_loss_after=after,
        coordinate_cycles=completed_cycles,
        converged=converged,
    )


def _calibrate(
    probabilities: dict[str, float],
    *,
    temperature: float,
    home_bias: float,
    away_bias: float,
) -> dict[str, float]:
    if not _positive_finite(temperature):
        raise FirstScoreModelError("Temperature must be positive and finite")
    logits = {
        "home_first": math.log(probabilities["home_first"]) / temperature + home_bias,
        "away_first": math.log(probabilities["away_first"]) / temperature + away_bias,
        "no_goal": math.log(probabilities["no_goal"]) / temperature,
    }
    maximum = max(logits.values())
    weights = {key: math.exp(value - maximum) for key, value in logits.items()}
    total = math.fsum(weights.values())
    result = {key: weights[key] / total for key in OUTCOMES}
    if not math.isclose(math.fsum(result.values()), 1.0, abs_tol=1e-12):
        raise FirstScoreModelError("First-score probabilities are not normalized")
    return result


def _golden_coordinate(
    objective,
    parameters: list[float],
    coordinate: int,
    lower: float,
    upper: float,
    tolerance: float,
) -> float:
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)

    def at(value: float) -> float:
        candidate = list(parameters)
        candidate[coordinate] = value
        return objective(candidate)

    left_value = at(left)
    right_value = at(right)
    while upper - lower > tolerance:
        if left_value <= right_value:
            upper = right
            right = left
            right_value = left_value
            left = upper - ratio * (upper - lower)
            left_value = at(left)
        else:
            lower = left
            left = right
            left_value = right_value
            right = lower + ratio * (upper - lower)
            right_value = at(right)
    return (lower + upper) / 2.0


def _validate_observation(row: FirstScoreObservation) -> None:
    if row.outcome not in OUTCOMES:
        raise FirstScoreModelError(f"Unknown first-score outcome: {row.outcome}")
    if row.prediction_at.tzinfo is None or row.kickoff.tzinfo is None:
        raise FirstScoreModelError("First-score timestamps must be timezone-aware")
    if row.prediction_at >= row.kickoff:
        raise FirstScoreModelError("First-score prediction must precede kickoff")
    baseline_first_team_probabilities(
        row.expected_home_goals, row.expected_away_goals
    )


def _model_value(model: FirstScoreTimingModel) -> dict:
    value = asdict(model)
    value["training_kickoff_end_exclusive"] = model.training_kickoff_end_exclusive.isoformat()
    value["prospective_holdout_start"] = model.prospective_holdout_start.isoformat()
    for raw, horizon in zip(value["horizons"], model.horizons, strict=True):
        raw["training_kickoff_start"] = horizon.training_kickoff_start.isoformat()
        raw["training_kickoff_end_exclusive"] = horizon.training_kickoff_end_exclusive.isoformat()
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise FirstScoreModelError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FirstScoreModelError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise FirstScoreModelError(f"{name} must include a timezone")
    return parsed


def _string(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise FirstScoreModelError(f"{key} must be a non-empty string")
    return value


def _positive_float(raw: dict, key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FirstScoreModelError(f"{key} must be numeric")
    parsed = float(value)
    if not _positive_finite(parsed):
        raise FirstScoreModelError(f"{key} must be positive and finite")
    return parsed


def _positive_int(raw: dict, key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FirstScoreModelError(f"{key} must be a positive integer")
    return value


def _positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0
