from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json
from soccer_bot.datasets.corner_features import CornerFeatureRow


CANDIDATES = (
    "independent_poisson",
    "negative_binomial_marginals",
    "dependent_bivariate_count",
)


class CornerModelError(RuntimeError):
    """Raised when a joint corner model or probability is unsafe."""


@dataclass(frozen=True)
class CornerModelConfig:
    model_version: str
    status: str
    recipe_frozen_at: datetime
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    information_states: tuple[str, ...]
    probability_floor: float
    tail_tolerance: float
    minimum_max_corners: int
    maximum_max_corners: int
    minimum_fit_fixtures: int
    nb_shape_minimum: float
    nb_shape_maximum: float
    shared_fraction_minimum: float
    shared_fraction_maximum: float
    optimizer_tolerance: float
    bootstrap_replicates: int
    bootstrap_seed: int


@dataclass(frozen=True)
class CornerHorizonFit:
    information_state: str
    training_fixtures: int
    training_kickoff_start: datetime
    training_kickoff_end_exclusive: datetime
    home_nb_shape: float
    away_nb_shape: float
    shared_intensity_fraction: float
    poisson_mean_joint_log_loss: float
    negative_binomial_mean_joint_log_loss: float
    bivariate_poisson_mean_joint_log_loss: float


@dataclass(frozen=True)
class JointCornerModel:
    model_version: str
    status: str
    training_kickoff_end_exclusive: datetime
    prospective_holdout_start: datetime
    horizons: tuple[CornerHorizonFit, ...]


def load_corner_model_config(path: Path) -> CornerModelConfig:
    raw = load_json(path)
    if raw.get("parameter_status") != (
        "target_feature_candidate_recipes_and_gate_frozen_before_evaluation"
    ):
        raise CornerModelError("Corner model recipe is not frozen")
    recipe = raw.get("candidate_recipe")
    evaluation = raw.get("evaluation")
    if not isinstance(recipe, dict) or not isinstance(evaluation, dict):
        raise CornerModelError("Corner model configuration is incomplete")
    if recipe.get("implemented_candidates") != list(CANDIDATES):
        raise CornerModelError("Corner candidate set changed")
    config = CornerModelConfig(
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
        information_states=tuple(str(value) for value in raw["information_states"]),
        probability_floor=_positive_float(recipe, "probability_floor"),
        tail_tolerance=_positive_float(recipe, "tail_tolerance"),
        minimum_max_corners=_positive_int(recipe, "minimum_max_corners_per_team"),
        maximum_max_corners=_positive_int(recipe, "maximum_max_corners_per_team"),
        minimum_fit_fixtures=_positive_int(
            recipe, "minimum_fit_fixtures_per_horizon"
        ),
        nb_shape_minimum=_positive_float(
            recipe, "negative_binomial_shape_minimum"
        ),
        nb_shape_maximum=_positive_float(
            recipe, "negative_binomial_shape_maximum"
        ),
        shared_fraction_minimum=_nonnegative_float(
            recipe, "shared_intensity_fraction_minimum"
        ),
        shared_fraction_maximum=_positive_float(
            recipe, "shared_intensity_fraction_maximum"
        ),
        optimizer_tolerance=_positive_float(recipe, "optimizer_tolerance"),
        bootstrap_replicates=_positive_int(evaluation, "bootstrap_replicates"),
        bootstrap_seed=_positive_int(evaluation, "bootstrap_seed"),
    )
    if config.recipe_frozen_at >= config.prospective_holdout_start:
        raise CornerModelError("Corner recipe must freeze before prospective scoring")
    if config.training_kickoff_end_exclusive > config.recipe_frozen_at:
        raise CornerModelError("Corner training cutoff is after recipe freeze")
    if config.information_states != (
        "pre_lineup_72h_clean_v1",
        "pre_lineup_24h_v1",
    ):
        raise CornerModelError("Corner horizons changed")
    if config.minimum_max_corners > config.maximum_max_corners:
        raise CornerModelError("Invalid corner support")
    if config.nb_shape_minimum >= config.nb_shape_maximum:
        raise CornerModelError("Invalid negative-binomial bounds")
    if not 0 <= config.shared_fraction_minimum < config.shared_fraction_maximum < 1:
        raise CornerModelError("Invalid shared-intensity bounds")
    return config


def fit_joint_corner_model(
    rows: list[CornerFeatureRow], config: CornerModelConfig
) -> JointCornerModel:
    grouped: dict[str, list[CornerFeatureRow]] = defaultdict(list)
    seen = set()
    for row in rows:
        _validate_row(row)
        if row.kickoff >= config.training_kickoff_end_exclusive:
            continue
        key = (row.fixture_id, row.information_state)
        if key in seen:
            raise CornerModelError(f"Duplicate corner feature row: {key}")
        seen.add(key)
        grouped[row.information_state].append(row)
    if set(grouped) != set(config.information_states):
        raise CornerModelError("Corner fit does not cover both horizons")
    horizons = tuple(
        _fit_horizon(state, values, config)
        for state, values in sorted(grouped.items())
    )
    return JointCornerModel(
        model_version=config.model_version,
        status=config.status,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        prospective_holdout_start=config.prospective_holdout_start,
        horizons=horizons,
    )


def corner_joint_probability(
    model: JointCornerModel,
    *,
    candidate: str,
    information_state: str,
    expected_home_corners: float,
    expected_away_corners: float,
    home_corners: int,
    away_corners: int,
) -> float:
    fit = _horizon(model, information_state)
    _validate_probability_inputs(
        expected_home_corners,
        expected_away_corners,
        home_corners,
        away_corners,
    )
    if candidate == "independent_poisson":
        return _poisson_pmf(expected_home_corners, home_corners) * _poisson_pmf(
            expected_away_corners, away_corners
        )
    if candidate == "negative_binomial_marginals":
        return _negative_binomial_pmf(
            expected_home_corners, fit.home_nb_shape, home_corners
        ) * _negative_binomial_pmf(
            expected_away_corners, fit.away_nb_shape, away_corners
        )
    if candidate == "dependent_bivariate_count":
        shared = fit.shared_intensity_fraction * min(
            expected_home_corners, expected_away_corners
        )
        return _bivariate_poisson_pmf(
            expected_home_corners - shared,
            expected_away_corners - shared,
            shared,
            home_corners,
            away_corners,
        )
    raise CornerModelError(f"Unknown corner candidate: {candidate}")


def corner_score_grid(
    model: JointCornerModel,
    config: CornerModelConfig,
    *,
    candidate: str,
    information_state: str,
    expected_home_corners: float,
    expected_away_corners: float,
) -> dict[tuple[int, int], float]:
    for maximum in range(config.minimum_max_corners, config.maximum_max_corners + 1):
        grid = {
            (home, away): corner_joint_probability(
                model,
                candidate=candidate,
                information_state=information_state,
                expected_home_corners=expected_home_corners,
                expected_away_corners=expected_away_corners,
                home_corners=home,
                away_corners=away,
            )
            for home in range(maximum + 1)
            for away in range(maximum + 1)
        }
        total = math.fsum(grid.values())
        if 1.0 - total <= config.tail_tolerance:
            return {score: value / total for score, value in grid.items()}
    raise CornerModelError("Corner grid tail exceeds configured maximum support")


def corner_total_distribution(
    model: JointCornerModel,
    config: CornerModelConfig,
    *,
    candidate: str,
    information_state: str,
    expected_home_corners: float,
    expected_away_corners: float,
) -> tuple[float, ...]:
    """Return a normalized match-total distribution without building a 2-D grid."""

    fit = _horizon(model, information_state)
    if not all(
        math.isfinite(value) and value > 0
        for value in (expected_home_corners, expected_away_corners)
    ):
        raise CornerModelError("Expected corner rates must be positive and finite")
    maximum_total = 2 * config.maximum_max_corners
    if candidate == "independent_poisson":
        probabilities = [
            _poisson_pmf(expected_home_corners + expected_away_corners, total)
            for total in range(maximum_total + 1)
        ]
    elif candidate == "negative_binomial_marginals":
        home = [
            _negative_binomial_pmf(
                expected_home_corners, fit.home_nb_shape, count
            )
            for count in range(config.maximum_max_corners + 1)
        ]
        away = [
            _negative_binomial_pmf(
                expected_away_corners, fit.away_nb_shape, count
            )
            for count in range(config.maximum_max_corners + 1)
        ]
        probabilities = [
            math.fsum(
                home[home_count] * away[total - home_count]
                for home_count in range(
                    max(0, total - config.maximum_max_corners),
                    min(config.maximum_max_corners, total) + 1,
                )
            )
            for total in range(maximum_total + 1)
        ]
    elif candidate == "dependent_bivariate_count":
        shared = fit.shared_intensity_fraction * min(
            expected_home_corners, expected_away_corners
        )
        independent_total_rate = (
            expected_home_corners + expected_away_corners - 2.0 * shared
        )
        shared_probabilities = _poisson_distribution_to_tolerance(
            shared, config.tail_tolerance, config.maximum_max_corners
        )
        probabilities = [
            math.fsum(
                shared_probability
                * _poisson_pmf(
                    independent_total_rate, total - 2 * shared_count
                )
                for shared_count, shared_probability in enumerate(
                    shared_probabilities[: total // 2 + 1]
                )
            )
            for total in range(maximum_total + 1)
        ]
    else:
        raise CornerModelError(f"Unknown corner candidate: {candidate}")
    cumulative = 0.0
    for index, probability in enumerate(probabilities):
        cumulative += probability
        if (
            index >= 2 * config.minimum_max_corners
            and 1.0 - cumulative <= config.tail_tolerance
        ):
            kept = probabilities[: index + 1]
            normalizer = math.fsum(kept)
            return tuple(value / normalizer for value in kept)
    raise CornerModelError("Corner total tail exceeds configured maximum support")


def _poisson_distribution_to_tolerance(
    rate: float, tolerance: float, maximum: int
) -> tuple[float, ...]:
    values = []
    cumulative = 0.0
    for count in range(maximum + 1):
        probability = _poisson_pmf(rate, count)
        values.append(probability)
        cumulative += probability
        if 1.0 - cumulative <= tolerance:
            return tuple(values)
    raise CornerModelError("Shared corner intensity tail exceeds support")


def corner_model_sha256(model: JointCornerModel) -> str:
    encoded = json.dumps(
        _model_value(model), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dump_corner_model(
    model: JointCornerModel, path: Path, *, created_at: datetime
) -> None:
    value = {
        "artifact_version": "joint_corner_model_v1",
        "created_at": created_at.isoformat(),
        "logical_model_sha256": corner_model_sha256(model),
        "model": _model_value(model),
    }
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_corner_model(path: Path) -> JointCornerModel:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("artifact_version") != "joint_corner_model_v1":
        raise CornerModelError("Unsupported corner model artifact")
    raw = dict(value.get("model", {}))
    try:
        horizons = tuple(
            CornerHorizonFit(
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
        model = JointCornerModel(
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
        raise CornerModelError("Malformed corner model artifact") from error
    if value.get("logical_model_sha256") != corner_model_sha256(model):
        raise CornerModelError("Corner model logical hash mismatch")
    return model


def _fit_horizon(
    information_state: str,
    rows: list[CornerFeatureRow],
    config: CornerModelConfig,
) -> CornerHorizonFit:
    ordered = sorted(rows, key=lambda row: (row.kickoff, row.fixture_id))
    if len(ordered) < config.minimum_fit_fixtures:
        raise CornerModelError(
            f"Insufficient {information_state} corner rows: {len(ordered)}"
        )
    home_shape = math.exp(
        _golden_minimize(
            lambda log_shape: _mean_marginal_loss(
                ordered, math.exp(log_shape), home=True
            ),
            math.log(config.nb_shape_minimum),
            math.log(config.nb_shape_maximum),
            config.optimizer_tolerance,
        )
    )
    away_shape = math.exp(
        _golden_minimize(
            lambda log_shape: _mean_marginal_loss(
                ordered, math.exp(log_shape), home=False
            ),
            math.log(config.nb_shape_minimum),
            math.log(config.nb_shape_maximum),
            config.optimizer_tolerance,
        )
    )
    shared_fraction = _golden_minimize(
        lambda fraction: _mean_bivariate_loss(ordered, fraction),
        config.shared_fraction_minimum,
        config.shared_fraction_maximum,
        config.optimizer_tolerance,
    )
    poisson_loss = math.fsum(
        -math.log(
            max(
                _poisson_pmf(row.expected_home_corners, row.home_corners)
                * _poisson_pmf(row.expected_away_corners, row.away_corners),
                config.probability_floor,
            )
        )
        for row in ordered
    ) / len(ordered)
    nb_loss = math.fsum(
        -math.log(
            max(
                _negative_binomial_pmf(
                    row.expected_home_corners, home_shape, row.home_corners
                )
                * _negative_binomial_pmf(
                    row.expected_away_corners, away_shape, row.away_corners
                ),
                config.probability_floor,
            )
        )
        for row in ordered
    ) / len(ordered)
    bivariate_loss = _mean_bivariate_loss(ordered, shared_fraction)
    return CornerHorizonFit(
        information_state=information_state,
        training_fixtures=len(ordered),
        training_kickoff_start=ordered[0].kickoff,
        training_kickoff_end_exclusive=config.training_kickoff_end_exclusive,
        home_nb_shape=home_shape,
        away_nb_shape=away_shape,
        shared_intensity_fraction=shared_fraction,
        poisson_mean_joint_log_loss=poisson_loss,
        negative_binomial_mean_joint_log_loss=nb_loss,
        bivariate_poisson_mean_joint_log_loss=bivariate_loss,
    )


def _mean_marginal_loss(
    rows: list[CornerFeatureRow], shape: float, *, home: bool
) -> float:
    return math.fsum(
        -math.log(
            max(
                _negative_binomial_pmf(
                    row.expected_home_corners if home else row.expected_away_corners,
                    shape,
                    row.home_corners if home else row.away_corners,
                ),
                1e-300,
            )
        )
        for row in rows
    ) / len(rows)


def _mean_bivariate_loss(rows: list[CornerFeatureRow], fraction: float) -> float:
    return math.fsum(
        -math.log(
            max(
                _bivariate_probability_for_row(row, fraction),
                1e-300,
            )
        )
        for row in rows
    ) / len(rows)


def _bivariate_probability_for_row(row: CornerFeatureRow, fraction: float) -> float:
    shared = fraction * min(
        row.expected_home_corners, row.expected_away_corners
    )
    return _bivariate_poisson_pmf(
        row.expected_home_corners - shared,
        row.expected_away_corners - shared,
        shared,
        row.home_corners,
        row.away_corners,
    )


def _negative_binomial_pmf(mean: float, shape: float, count: int) -> float:
    return math.exp(
        math.lgamma(count + shape)
        - math.lgamma(shape)
        - math.lgamma(count + 1)
        + shape * math.log(shape / (shape + mean))
        + count * math.log(mean / (shape + mean))
    )


def _poisson_pmf(rate: float, count: int) -> float:
    if rate == 0:
        return 1.0 if count == 0 else 0.0
    return math.exp(-rate + count * math.log(rate) - math.lgamma(count + 1))


def _bivariate_poisson_pmf(
    home_only_rate: float,
    away_only_rate: float,
    shared_rate: float,
    home: int,
    away: int,
) -> float:
    return math.fsum(
        _poisson_pmf(home_only_rate, home - shared)
        * _poisson_pmf(away_only_rate, away - shared)
        * _poisson_pmf(shared_rate, shared)
        for shared in range(min(home, away) + 1)
    )


def _golden_minimize(objective, lower: float, upper: float, tolerance: float) -> float:
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = objective(left)
    right_value = objective(right)
    while upper - lower > tolerance:
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
    return (lower + upper) / 2.0


def _horizon(model: JointCornerModel, information_state: str) -> CornerHorizonFit:
    fit = next(
        (row for row in model.horizons if row.information_state == information_state),
        None,
    )
    if fit is None:
        raise CornerModelError(f"Unsupported corner horizon: {information_state}")
    return fit


def _validate_row(row: CornerFeatureRow) -> None:
    if row.prediction_at.tzinfo is None or row.kickoff.tzinfo is None:
        raise CornerModelError("Corner timestamps must be timezone-aware")
    if row.prediction_at >= row.kickoff:
        raise CornerModelError("Corner prediction must precede kickoff")
    _validate_probability_inputs(
        row.expected_home_corners,
        row.expected_away_corners,
        row.home_corners,
        row.away_corners,
    )


def _validate_probability_inputs(
    expected_home: float, expected_away: float, home: int, away: int
) -> None:
    if not all(math.isfinite(value) and value > 0 for value in (expected_home, expected_away)):
        raise CornerModelError("Expected corner rates must be positive and finite")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (home, away)):
        raise CornerModelError("Observed corners must be nonnegative integers")


def _model_value(model: JointCornerModel) -> dict:
    value = asdict(model)
    value["training_kickoff_end_exclusive"] = model.training_kickoff_end_exclusive.isoformat()
    value["prospective_holdout_start"] = model.prospective_holdout_start.isoformat()
    for raw, horizon in zip(value["horizons"], model.horizons, strict=True):
        raw["training_kickoff_start"] = horizon.training_kickoff_start.isoformat()
        raw["training_kickoff_end_exclusive"] = horizon.training_kickoff_end_exclusive.isoformat()
    return value


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise CornerModelError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CornerModelError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise CornerModelError(f"{name} must include a timezone")
    return parsed


def _string(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise CornerModelError(f"{key} must be a non-empty string")
    return value


def _positive_float(raw: dict, key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CornerModelError(f"{key} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise CornerModelError(f"{key} must be positive and finite")
    return parsed


def _nonnegative_float(raw: dict, key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CornerModelError(f"{key} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise CornerModelError(f"{key} must be nonnegative and finite")
    return parsed


def _positive_int(raw: dict, key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CornerModelError(f"{key} must be a positive integer")
    return value
