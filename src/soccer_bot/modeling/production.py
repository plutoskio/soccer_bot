from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.datasets.features import (
    RegulationFeatureRow,
    RegulationInferenceFeatureRow,
)
from soccer_bot.modeling.calibration import temperature_scale_probabilities
from soccer_bot.modeling.rich_rates import (
    RichRateConfig,
    RichRateFeatureRow,
    RichRateInferenceFeatureRow,
    apply_rich_rate_correction,
    fit_rich_rate_coefficients,
)
from soccer_bot.modeling.reproducibility import (
    validate_champion_reproducibility,
)
from soccer_bot.modeling.walk_forward import (
    WalkForwardConfig,
    fit_score_rate_scales,
    moneyline_probabilities,
)
from soccer_bot.prediction_integrity import champion_prediction_rows_sha256


class ProductionModelError(RuntimeError):
    """Raised when a champion refit or inference artifact is inconsistent."""


@dataclass(frozen=True)
class ChampionHorizonParameters:
    information_state: str
    training_fixtures: int
    home_rate_scale: float
    away_rate_scale: float
    xg_signal_coefficient: float
    shots_signal_coefficient: float
    temperature: float


@dataclass(frozen=True)
class RegulationChampionModel:
    model_version: str
    contract: str
    model_class: str
    feature_version: str
    rich_feature_version: str
    calibration_policy: str
    distribution_limitation: str
    horizons: tuple[ChampionHorizonParameters, ...]


@dataclass(frozen=True)
class ChampionMoneylinePrediction:
    model_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    expected_home_goals: float
    expected_away_goals: float
    raw_home_win_probability: float
    raw_draw_probability: float
    raw_away_win_probability: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    home_history_matches: int
    away_history_matches: int
    home_xg_history: int
    away_xg_history: int
    home_shots_history: int
    away_shots_history: int
    source_max_retrieved_at: datetime | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _ScaledTrainingRow:
    fixture_id: str
    information_state: str
    expected_home_goals: float
    expected_away_goals: float
    home_goals: int
    away_goals: int


def fit_regulation_champion(
    feature_rows: list[RegulationFeatureRow],
    rich_rows: list[RichRateFeatureRow],
    *,
    temperatures: dict[str, float],
    model_specification: dict,
    rich_config: RichRateConfig,
    walk_forward_config: WalkForwardConfig,
) -> RegulationChampionModel:
    if not feature_rows or not rich_rows:
        raise ProductionModelError("Champion refit requires non-empty features")
    if model_specification.get("parameter_status") != (
        "recipe_frozen_after_final_test_production_parameters_refittable"
    ):
        raise ProductionModelError("Champion recipe is not frozen for refit")
    scales = {
        fit.information_state: fit
        for fit in fit_score_rate_scales(feature_rows, walk_forward_config)
    }
    feature_groups: dict[str, list[RegulationFeatureRow]] = defaultdict(list)
    rich_groups: dict[str, list[RichRateFeatureRow]] = defaultdict(list)
    for row in feature_rows:
        feature_groups[row.information_state].append(row)
    for row in rich_rows:
        rich_groups[row.information_state].append(row)
    if set(feature_groups) != set(rich_groups) or set(feature_groups) != set(
        temperatures
    ):
        raise ProductionModelError(
            "Feature, rich-feature, and calibration horizons differ"
        )
    horizons = []
    for information_state, values in sorted(feature_groups.items()):
        scale = scales[information_state]
        training_rows = [
            _ScaledTrainingRow(
                fixture_id=row.fixture_id,
                information_state=row.information_state,
                expected_home_goals=_clamp(
                    row.expected_home_goals * scale.home_rate_scale,
                    walk_forward_config.minimum_expected_goals,
                    walk_forward_config.maximum_expected_goals,
                ),
                expected_away_goals=_clamp(
                    row.expected_away_goals * scale.away_rate_scale,
                    walk_forward_config.minimum_expected_goals,
                    walk_forward_config.maximum_expected_goals,
                ),
                home_goals=row.home_goals,
                away_goals=row.away_goals,
            )
            for row in values
        ]
        coefficients, converged, _ = fit_rich_rate_coefficients(
            training_rows, rich_groups[information_state], rich_config
        )
        if not converged:
            raise ProductionModelError(
                f"Rich-rate refit did not converge for {information_state}"
            )
        temperature = temperatures[information_state]
        if temperature <= 0:
            raise ProductionModelError("Frozen temperature must be positive")
        horizons.append(
            ChampionHorizonParameters(
                information_state=information_state,
                training_fixtures=len(values),
                home_rate_scale=scale.home_rate_scale,
                away_rate_scale=scale.away_rate_scale,
                xg_signal_coefficient=coefficients["xg_signal"],
                shots_signal_coefficient=coefficients["shots_signal"],
                temperature=temperature,
            )
        )
    return RegulationChampionModel(
        model_version=str(model_specification["model_version"]),
        contract=str(model_specification["contract"]),
        model_class=str(model_specification["model_class"]),
        feature_version=str(model_specification["feature_version"]),
        rich_feature_version=str(model_specification["rich_feature_version"]),
        calibration_policy=str(
            model_specification["production_refit"]["temperature"]
        ),
        distribution_limitation=str(
            model_specification["inference"]["distribution_limitation"]
        ),
        horizons=tuple(horizons),
    )


def predict_regulation_moneyline(
    feature_rows: list[RegulationInferenceFeatureRow],
    rich_rows: list[RichRateInferenceFeatureRow | RichRateFeatureRow],
    model: RegulationChampionModel,
    *,
    rich_config: RichRateConfig,
    walk_forward_config: WalkForwardConfig,
) -> list[ChampionMoneylinePrediction]:
    parameters = {row.information_state: row for row in model.horizons}
    rich = {(row.fixture_id, row.information_state): row for row in rich_rows}
    output = []
    for row in feature_rows:
        if row.information_state not in parameters:
            raise ProductionModelError(
                f"Unsupported inference horizon: {row.information_state}"
            )
        key = (row.fixture_id, row.information_state)
        if key not in rich:
            raise ProductionModelError(f"Missing rich features for {key}")
        params = parameters[row.information_state]
        base_home = _clamp(
            row.expected_home_goals * params.home_rate_scale,
            walk_forward_config.minimum_expected_goals,
            walk_forward_config.maximum_expected_goals,
        )
        base_away = _clamp(
            row.expected_away_goals * params.away_rate_scale,
            walk_forward_config.minimum_expected_goals,
            walk_forward_config.maximum_expected_goals,
        )
        rich_row = rich[key]
        home_rate, away_rate = apply_rich_rate_correction(
            base_home,
            base_away,
            rich_row,
            {
                "xg_signal": params.xg_signal_coefficient,
                "shots_signal": params.shots_signal_coefficient,
            },
            rich_config,
        )
        raw, _ = moneyline_probabilities(
            home_rate,
            away_rate,
            0.0,
            walk_forward_config.poisson_tail_tolerance,
        )
        calibrated = temperature_scale_probabilities(raw, params.temperature)
        warnings = []
        if row.home_cold_start:
            warnings.append("home_team_cold_start")
        if row.away_cold_start:
            warnings.append("away_team_cold_start")
        if min(rich_row.home_xg_history, rich_row.away_xg_history) == 0:
            warnings.append("xg_signal_unavailable_or_prior_only")
        if min(rich_row.home_shots_history, rich_row.away_shots_history) == 0:
            warnings.append("shots_signal_unavailable_or_prior_only")
        warnings.append("moneyline_calibration_not_score_grid_coherent")
        output.append(
            ChampionMoneylinePrediction(
                model_version=model.model_version,
                fixture_id=row.fixture_id,
                information_state=row.information_state,
                prediction_at=row.prediction_at,
                kickoff=row.kickoff,
                competition_id=row.competition_id,
                season_id=row.season_id,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                expected_home_goals=home_rate,
                expected_away_goals=away_rate,
                raw_home_win_probability=raw["home_win"],
                raw_draw_probability=raw["draw"],
                raw_away_win_probability=raw["away_win"],
                home_win_probability=calibrated["home_win"],
                draw_probability=calibrated["draw"],
                away_win_probability=calibrated["away_win"],
                home_history_matches=row.home_history_matches,
                away_history_matches=row.away_history_matches,
                home_xg_history=rich_row.home_xg_history,
                away_xg_history=rich_row.away_xg_history,
                home_shots_history=rich_row.home_shots_history,
                away_shots_history=rich_row.away_shots_history,
                source_max_retrieved_at=_maximum_timestamp(
                    row.source_max_retrieved_at,
                    getattr(rich_row, "source_max_retrieved_at", None),
                ),
                warnings=tuple(warnings),
            )
        )
    return sorted(
        output,
        key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
    )


def champion_model_sha256(model: RegulationChampionModel) -> str:
    body = json.dumps(
        asdict(model), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_regulation_champion(
    path: Path,
    *,
    model_config_path: Path | None = None,
    repository_root: Path | None = None,
) -> RegulationChampionModel:
    resolved_root = (
        Path(__file__).resolve().parents[3]
        if repository_root is None
        else repository_root
    )
    validate_champion_reproducibility(
        model_path=path,
        model_config_path=(
            resolved_root / "config" / "models" / "regulation_champion_v1.json"
            if model_config_path is None
            else model_config_path
        ),
        repository_root=resolved_root,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    model = raw.get("model", raw)
    horizons = tuple(
        ChampionHorizonParameters(**item) for item in model.pop("horizons")
    )
    value = RegulationChampionModel(horizons=horizons, **model)
    if raw.get("logical_model_sha256") not in {
        None,
        champion_model_sha256(value),
    }:
        raise ProductionModelError("Champion model logical hash mismatch")
    return value


def prediction_rows_sha256(rows: list[ChampionMoneylinePrediction]) -> str:
    return champion_prediction_rows_sha256([asdict(row) for row in rows])


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _maximum_timestamp(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None
