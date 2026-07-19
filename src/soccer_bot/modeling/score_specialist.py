from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json
from soccer_bot.modeling.score_grid import (
    ScoreGridCandidate,
    ScoreGridResearchConfig,
    ScoreRatePrediction,
    fit_score_grid_candidate,
    poisson_score_grid,
    transform_score_grid,
)


class ScoreSpecialistError(RuntimeError):
    """Raised when the specialized score model or artifact is unsafe."""


@dataclass(frozen=True)
class ScoreSpecialistConfig:
    model_version: str
    status: str
    parent_rate_model_version: str
    model_family: str
    recipe_frozen_at: datetime
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    information_states: tuple[str, ...]
    poisson_tail_tolerance: float
    minimum_max_goals: int
    maximum_max_goals: int
    probability_floor: float
    candidate: ScoreGridCandidate
    prospective_gate: dict


@dataclass(frozen=True)
class ScoreSpecialistHorizon:
    information_state: str
    training_fixtures: int
    training_kickoff_start: datetime
    training_kickoff_end_exclusive: datetime
    parameters: dict[str, float]
    converged: bool
    iterations: int
    objective: float


@dataclass(frozen=True)
class RegulationScoreSpecialistModel:
    model_version: str
    status: str
    parent_rate_model_version: str
    model_family: str
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    horizons: tuple[ScoreSpecialistHorizon, ...]


def load_score_specialist_config(path: Path) -> ScoreSpecialistConfig:
    value = load_json(path)
    if value.get("parameter_status") != (
        "recipe_and_gate_frozen_before_first_eligible_prediction"
    ):
        raise ScoreSpecialistError("Specialist recipe is not frozen")
    support = value.get("support")
    candidate_value = value.get("candidate")
    invariants = value.get("invariants")
    gate = value.get("prospective_gate")
    if not all(isinstance(item, dict) for item in (support, candidate_value, invariants, gate)):
        raise ScoreSpecialistError("Specialist configuration sections are missing")
    if invariants.get("one_grid_for_all_score_family_contracts") is not True:
        raise ScoreSpecialistError("Score-family contracts must share one grid")
    if invariants.get("moneyline_must_match_specialized_1x2") is not False:
        raise ScoreSpecialistError("Specialist score model cannot be forced to match 1X2")
    if gate.get("moneyline_difference_policy") != (
        "diagnostic_only_not_a_rejection_gate"
    ):
        raise ScoreSpecialistError("Moneyline disagreement policy changed")
    names = candidate_value.get("features")
    scales = candidate_value.get("feature_scales")
    if not isinstance(names, list) or not isinstance(scales, dict):
        raise ScoreSpecialistError("Specialist score features are invalid")
    candidate = ScoreGridCandidate(
        model_key=_string(candidate_value, "model_key"),
        family=_string(candidate_value, "family"),
        minimum_fit_fixtures=_positive_int(
            candidate_value.get("minimum_fit_fixtures_per_horizon"),
            "minimum_fit_fixtures_per_horizon",
        ),
        optimizer_tolerance=_positive_float(
            candidate_value.get("optimizer_tolerance"), "optimizer_tolerance"
        ),
        feature_names=tuple(_nonempty(item, "feature") for item in names),
        feature_scales=tuple(
            _positive_float(scales.get(name), f"feature scale {name}")
            for name in names
        ),
        ridge_penalty=_positive_float(
            candidate_value.get("ridge_penalty"), "ridge_penalty"
        ),
        maximum_newton_iterations=_positive_int(
            candidate_value.get("maximum_newton_iterations"),
            "maximum_newton_iterations",
        ),
    )
    if candidate.family != "exponential_tilt":
        raise ScoreSpecialistError("Specialist v1 must use exponential tilt")
    states = value.get("information_states")
    if states != ["pre_lineup_72h_clean_v1", "pre_lineup_24h_v1"]:
        raise ScoreSpecialistError("Specialist horizons changed")
    config = ScoreSpecialistConfig(
        model_version=_string(value, "model_version"),
        status=_string(value, "status"),
        parent_rate_model_version=_string(value, "parent_rate_model_version"),
        model_family=_string(value, "model_family"),
        recipe_frozen_at=_timestamp(value.get("recipe_frozen_at"), "recipe_frozen_at"),
        training_kickoff_end_exclusive=_timestamp(
            value.get("training_kickoff_end_exclusive"),
            "training_kickoff_end_exclusive",
        ),
        prospective_holdout_start=_timestamp(
            value.get("prospective_holdout_kickoff_start_inclusive"),
            "prospective_holdout_kickoff_start_inclusive",
        ),
        information_states=tuple(states),
        poisson_tail_tolerance=_positive_float(
            support.get("poisson_tail_tolerance"), "poisson_tail_tolerance"
        ),
        minimum_max_goals=_positive_int(
            support.get("minimum_max_goals_per_team"),
            "minimum_max_goals_per_team",
        ),
        maximum_max_goals=_positive_int(
            support.get("maximum_max_goals_per_team"),
            "maximum_max_goals_per_team",
        ),
        probability_floor=_positive_float(
            support.get("probability_floor"), "probability_floor"
        ),
        candidate=candidate,
        prospective_gate=gate,
    )
    if config.recipe_frozen_at >= config.prospective_holdout_start:
        raise ScoreSpecialistError("Recipe must freeze before prospective holdout")
    if config.training_kickoff_end_exclusive > config.recipe_frozen_at:
        raise ScoreSpecialistError("Training cannot extend beyond recipe freeze")
    if config.minimum_max_goals > config.maximum_max_goals:
        raise ScoreSpecialistError("Invalid score support bounds")
    return config


def fit_regulation_score_specialist(
    rows: list[ScoreRatePrediction], config: ScoreSpecialistConfig
) -> RegulationScoreSpecialistModel:
    if not rows:
        raise ScoreSpecialistError("Specialist fit requires prediction rows")
    grouped: dict[str, list[ScoreRatePrediction]] = defaultdict(list)
    keys = set()
    for row in rows:
        if row.kickoff >= config.training_kickoff_end_exclusive:
            continue
        key = (row.fixture_id, row.information_state)
        if key in keys:
            raise ScoreSpecialistError(f"Duplicate specialist fit row: {key}")
        keys.add(key)
        grouped[row.information_state].append(row)
    if set(grouped) != set(config.information_states):
        raise ScoreSpecialistError("Specialist fit does not cover both horizons")
    research = _research_config(config)
    horizons = []
    for information_state, values in sorted(grouped.items()):
        fit = fit_score_grid_candidate(
            values,
            config.candidate,
            config=research,
            window_key="all_history_specialist_fit",
            information_state=information_state,
        )
        if not fit.converged:
            raise ScoreSpecialistError(
                f"Specialist fit did not converge for {information_state}"
            )
        horizons.append(
            ScoreSpecialistHorizon(
                information_state=information_state,
                training_fixtures=len(values),
                training_kickoff_start=min(row.kickoff for row in values),
                training_kickoff_end_exclusive=(
                    config.training_kickoff_end_exclusive
                ),
                parameters=fit.parameters,
                converged=fit.converged,
                iterations=fit.iterations,
                objective=fit.objective,
            )
        )
    return RegulationScoreSpecialistModel(
        model_version=config.model_version,
        status=config.status,
        parent_rate_model_version=config.parent_rate_model_version,
        model_family=config.model_family,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        prospective_holdout_start=config.prospective_holdout_start,
        horizons=tuple(horizons),
    )


def specialist_score_grid(
    model: RegulationScoreSpecialistModel,
    config: ScoreSpecialistConfig,
    *,
    information_state: str,
    expected_home_goals: float,
    expected_away_goals: float,
) -> dict[tuple[int, int], float]:
    horizons = {item.information_state: item for item in model.horizons}
    if information_state not in horizons:
        raise ScoreSpecialistError(f"Unsupported specialist horizon: {information_state}")
    research = _research_config(config)
    base = poisson_score_grid(expected_home_goals, expected_away_goals, research)
    grid = transform_score_grid(
        base,
        config.candidate,
        horizons[information_state].parameters,
    )
    if not math.isclose(math.fsum(grid.values()), 1.0, abs_tol=1e-10):
        raise ScoreSpecialistError("Specialist score grid is not normalized")
    return grid


def score_specialist_sha256(model: RegulationScoreSpecialistModel) -> str:
    body = json.dumps(
        _model_value(model), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def dump_score_specialist_model(
    model: RegulationScoreSpecialistModel, path: Path, *, created_at: datetime
) -> None:
    value = {
        "artifact_version": "regulation_score_specialist_model_v1",
        "created_at": created_at.isoformat(),
        "logical_model_sha256": score_specialist_sha256(model),
        "model": _model_value(model),
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_score_specialist_model(path: Path) -> RegulationScoreSpecialistModel:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("artifact_version") != "regulation_score_specialist_model_v1":
        raise ScoreSpecialistError("Unsupported specialist artifact version")
    raw = dict(value.get("model", {}))
    try:
        horizons = tuple(
            ScoreSpecialistHorizon(
                **{
                    **item,
                    "training_kickoff_start": _timestamp(
                        item["training_kickoff_start"],
                        "training_kickoff_start",
                    ),
                    "training_kickoff_end_exclusive": _timestamp(
                        item["training_kickoff_end_exclusive"],
                        "training_kickoff_end_exclusive",
                    ),
                }
            )
            for item in raw.pop("horizons")
        )
        model = RegulationScoreSpecialistModel(
            horizons=horizons,
            **{
                **raw,
                "training_kickoff_end_exclusive": _timestamp(
                    raw["training_kickoff_end_exclusive"],
                    "training_kickoff_end_exclusive",
                ),
                "prospective_holdout_start": _timestamp(
                    raw["prospective_holdout_start"],
                    "prospective_holdout_start",
                ),
            },
        )
    except (KeyError, TypeError) as error:
        raise ScoreSpecialistError("Malformed specialist artifact") from error
    if value.get("logical_model_sha256") != score_specialist_sha256(model):
        raise ScoreSpecialistError("Specialist model logical hash mismatch")
    return model


def _model_value(model: RegulationScoreSpecialistModel) -> dict:
    value = asdict(model)
    value["training_kickoff_end_exclusive"] = (
        model.training_kickoff_end_exclusive.isoformat()
    )
    value["prospective_holdout_start"] = model.prospective_holdout_start.isoformat()
    for output, horizon in zip(value["horizons"], model.horizons, strict=True):
        output["training_kickoff_start"] = horizon.training_kickoff_start.isoformat()
        output["training_kickoff_end_exclusive"] = (
            horizon.training_kickoff_end_exclusive.isoformat()
        )
    return value


def _research_config(config: ScoreSpecialistConfig) -> ScoreGridResearchConfig:
    return ScoreGridResearchConfig(
        model_version=config.model_version,
        research_status=config.status,
        baseline_model_key="independent_poisson_score_grid",
        moneyline_control_model_key="diagnostic_only_moneyline_control",
        moneyline_control_temperature_minimum=0.5,
        moneyline_control_temperature_maximum=2.0,
        moneyline_control_optimizer_tolerance=1e-8,
        moneyline_control_minimum_fit_fixtures=config.candidate.minimum_fit_fixtures,
        forbidden_kickoff_start=config.training_kickoff_end_exclusive,
        poisson_tail_tolerance=config.poisson_tail_tolerance,
        minimum_max_goals=config.minimum_max_goals,
        maximum_max_goals=config.maximum_max_goals,
        probability_floor=config.probability_floor,
        windows=(),
        candidates=(config.candidate,),
        selection_primary_metric="exact_score_log_loss",
        selection_require_negative_every_horizon=True,
        selection_tie_break_metrics=(
            "total_goals_log_loss",
            "goal_difference_log_loss",
        ),
        confirmation_exact_upper_below_zero=True,
        confirmation_nonpositive_metrics=(
            "total_goals_log_loss",
            "goal_difference_log_loss",
        ),
        confirmation_moneyline_delta_maximum=math.inf,
        bootstrap_replicates=int(config.prospective_gate["bootstrap_replicates"]),
        bootstrap_seed=int(config.prospective_gate["bootstrap_seed"]),
    )


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ScoreSpecialistError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScoreSpecialistError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ScoreSpecialistError(f"{name} must include a timezone")
    return parsed


def _string(value: dict, name: str) -> str:
    return _nonempty(value.get(name), name)


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScoreSpecialistError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScoreSpecialistError(f"{name} must be a positive integer")
    return value


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ScoreSpecialistError(f"{name} must be positive")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ScoreSpecialistError(f"{name} must be positive") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ScoreSpecialistError(f"{name} must be positive")
    return parsed
