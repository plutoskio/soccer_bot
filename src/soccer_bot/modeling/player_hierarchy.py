from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import random

from soccer_bot.config import load_json
from soccer_bot.datasets.players import (
    ConfirmedLineupFixture,
    PlayerMatchTarget,
)


class PlayerModelError(RuntimeError):
    """Raised when the player hierarchy would be incoherent or leak information."""


@dataclass(frozen=True)
class PlayerHierarchyConfig:
    model_version: str
    information_state: str
    source_code: str
    result_availability_delay_minutes: int
    production_fit_end_exclusive: datetime
    supported_positions: tuple[str, ...]
    goal_rate_prior_minutes: float
    assist_rate_prior_minutes: float
    starter_minutes_prior_appearances: float
    starter_minute_bins: tuple[int, ...]
    unattributed_goal_share: float
    minimum_player_history_minutes_for_full_label: int
    candidate_log_rate_coefficient: float
    maximum_absolute_log_rate_adjustment: float
    team_history_matches_for_full_weight: int
    apply_to_public_champion: bool
    diagnostic_start: datetime
    forbidden_prospective_start: datetime
    bootstrap_replicates: int
    bootstrap_seed: int


@dataclass(frozen=True)
class PositionParameters:
    position_code: str
    history_minutes: int
    history_starts: int
    goal_rate_per_minute: float
    assist_rate_per_minute: float
    starter_minute_bin_probabilities: tuple[float, ...]
    starter_minute_bin_means: tuple[float, ...]


@dataclass(frozen=True)
class PlayerParameters:
    player_id: str
    position_code: str
    history_minutes: int
    history_starts: int
    goals: int
    assists: int
    goal_rate_per_minute: float
    assist_rate_per_minute: float
    starter_minute_bin_probabilities: tuple[float, ...]
    expected_starter_minutes: float


@dataclass(frozen=True)
class TeamLineupParameters:
    team_id: str
    lineup_history_matches: int
    typical_attack_index: float


@dataclass(frozen=True)
class ConfirmedLineupPlayerModel:
    model_version: str
    information_state: str
    fit_end_exclusive: datetime
    training_rows: int
    training_fixtures: int
    training_players: int
    assisted_goal_probability: float
    minute_bin_upper_bounds: tuple[int, ...]
    position_parameters: tuple[PositionParameters, ...]
    player_parameters: tuple[PlayerParameters, ...]
    team_parameters: tuple[TeamLineupParameters, ...]
    lineup_adjustment_status: str
    apply_to_public_champion: bool


@dataclass(frozen=True)
class PlayerPropPrediction:
    player_id: str
    team_id: str
    position_code: str
    selection_role: str
    history_minutes: int
    history_starts: int
    expected_minutes: float | None
    minute_bin_probabilities: tuple[float, ...] | None
    expected_goals: float | None
    goal_count_probabilities_0_1_2_3_plus: tuple[float, ...] | None
    anytime_goal_probability: float | None
    expected_assists: float | None
    assist_count_probabilities_0_1_2_3_plus: tuple[float, ...] | None
    anytime_assist_probability: float | None
    score_or_assist_probability: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TeamLineupPrediction:
    team_id: str
    base_expected_goals: float
    candidate_expected_goals: float
    raw_lineup_attack_index: float
    typical_lineup_attack_index: float
    lineup_history_matches: int
    candidate_log_rate_adjustment: float
    player_expected_goals_sum: float
    residual_expected_goals: float
    player_expected_assists_sum: float
    expected_unassisted_goals: float
    authorized_to_replace_champion_rate: bool


@dataclass(frozen=True)
class ConfirmedLineupPrediction:
    model_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    base_prediction_at: datetime
    base_model_version: str
    base_model_sha256: str
    lineup_raw_artifact_id: str
    lineup_schedule_observation_id: str
    home: TeamLineupPrediction
    away: TeamLineupPrediction
    players: tuple[PlayerPropPrediction, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PlayerDiagnosticPrediction:
    fixture_id: str
    kickoff: datetime
    player_id: str
    position_code: str
    actual_minutes: int
    actual_goal: bool
    actual_assist: bool
    history_minutes: int
    history_starts: int
    expected_minutes: float
    minute_bin_probability: float
    goal_probability: float
    assist_probability: float
    baseline_goal_probability: float
    baseline_assist_probability: float


def load_player_hierarchy_config(path: Path) -> PlayerHierarchyConfig:
    raw = load_json(path)
    if raw.get("research_status") != "component_models_and_prospective_shadow_only":
        raise PlayerModelError("Player model must remain shadow-only")
    if raw.get("parameter_status") != (
        "structural_recipe_frozen_before_timestamp_safe_prospective_scoring"
    ):
        raise PlayerModelError("Player model structural recipe is not frozen")
    evaluation = raw["evaluation"]
    inference = raw["inference"]
    if evaluation.get("historical_confirmed_lineup_evaluation_allowed") is not False:
        raise PlayerModelError("Post-match lineups cannot become historical predictions")
    if inference.get("market_features_allowed") is not False:
        raise PlayerModelError("Player prediction model cannot consume market prices")
    if inference.get("trading_actions_allowed") is not False:
        raise PlayerModelError("Player shadow model cannot place trades")
    if inference.get("substitute_unconditional_props_enabled") is not False:
        raise PlayerModelError("Substitute appearance semantics are not validated")
    priors = raw["hierarchical_priors"]
    adjustment = raw["lineup_adjustment"]
    config = PlayerHierarchyConfig(
        model_version=str(raw["model_version"]),
        information_state=str(raw["information_state"]),
        source_code=str(raw["source_code"]),
        result_availability_delay_minutes=int(
            raw["result_availability_delay_minutes"]
        ),
        production_fit_end_exclusive=datetime.fromisoformat(
            raw["production_fit_end_exclusive"]
        ),
        supported_positions=tuple(str(item) for item in raw["supported_positions"]),
        goal_rate_prior_minutes=float(priors["goal_rate_prior_minutes"]),
        assist_rate_prior_minutes=float(priors["assist_rate_prior_minutes"]),
        starter_minutes_prior_appearances=float(
            priors["starter_minutes_prior_appearances"]
        ),
        starter_minute_bins=tuple(int(item) for item in priors["starter_minute_bins"]),
        unattributed_goal_share=float(priors["unattributed_goal_share"]),
        minimum_player_history_minutes_for_full_label=int(
            priors["minimum_player_history_minutes_for_full_label"]
        ),
        candidate_log_rate_coefficient=float(
            adjustment["candidate_log_rate_coefficient"]
        ),
        maximum_absolute_log_rate_adjustment=float(
            adjustment["maximum_absolute_log_rate_adjustment"]
        ),
        team_history_matches_for_full_weight=int(
            adjustment["team_history_matches_for_full_weight"]
        ),
        apply_to_public_champion=bool(adjustment["apply_to_public_champion"]),
        diagnostic_start=datetime.fromisoformat(
            evaluation["component_diagnostic_start"]
        ),
        forbidden_prospective_start=datetime.fromisoformat(
            evaluation["forbidden_prospective_start"]
        ),
        bootstrap_replicates=int(
            evaluation["paired_month_block_bootstrap_replicates"]
        ),
        bootstrap_seed=int(evaluation["bootstrap_seed"]),
    )
    if config.apply_to_public_champion:
        raise PlayerModelError("Unvalidated lineup adjustment cannot replace champion")
    if config.production_fit_end_exclusive > config.forbidden_prospective_start:
        raise PlayerModelError("Production fit may not consume prospective outcomes")
    if (
        config.production_fit_end_exclusive.tzinfo is None
        or config.diagnostic_start.tzinfo is None
        or config.forbidden_prospective_start.tzinfo is None
    ):
        raise PlayerModelError("All player model boundaries require timezones")
    if config.diagnostic_start >= config.forbidden_prospective_start:
        raise PlayerModelError("Diagnostic window must precede prospective period")
    if sorted(config.starter_minute_bins) != list(config.starter_minute_bins):
        raise PlayerModelError("Starter minute bins must be ordered")
    if config.starter_minute_bins[-1] != 130:
        raise PlayerModelError("Starter minute bins must cover the target support")
    positive = (
        config.goal_rate_prior_minutes,
        config.assist_rate_prior_minutes,
        config.starter_minutes_prior_appearances,
        config.team_history_matches_for_full_weight,
    )
    if min(positive) <= 0:
        raise PlayerModelError("Player model prior strengths must be positive")
    if config.bootstrap_replicates < 100:
        raise PlayerModelError("Player diagnostics require at least 100 bootstrap replicates")
    if not 0 < config.unattributed_goal_share < 1:
        raise PlayerModelError("Unattributed goal share must be in (0,1)")
    return config


def fit_confirmed_lineup_player_model(
    rows: list[PlayerMatchTarget],
    config: PlayerHierarchyConfig,
) -> ConfirmedLineupPlayerModel:
    training = [
        row
        for row in rows
        if row.result_available_at < config.production_fit_end_exclusive
    ]
    if not training:
        raise PlayerModelError("No player rows precede the frozen fit boundary")
    positions = _fit_position_parameters(training, config)
    position_map = {item.position_code: item for item in positions}
    player_groups: dict[str, list[PlayerMatchTarget]] = defaultdict(list)
    for row in training:
        player_groups[row.player_id].append(row)
    players = tuple(
        _fit_player_parameters(player_id, values, position_map, config)
        for player_id, values in sorted(player_groups.items())
    )
    player_map = {item.player_id: item for item in players}
    teams = _fit_team_typical_lineups(training, player_map, position_map, config)
    team_goal_values: dict[tuple[str, str], int] = {}
    total_assists = 0
    for row in training:
        team_goal_values[(row.fixture_id, row.team_id)] = row.team_goals
        total_assists += row.assists
    total_team_goals = sum(team_goal_values.values())
    if total_team_goals <= 0:
        raise PlayerModelError("Training data has no team goals")
    assisted_goal_probability = _clamp(total_assists / total_team_goals, 0.01, 0.99)
    return ConfirmedLineupPlayerModel(
        model_version=config.model_version,
        information_state=config.information_state,
        fit_end_exclusive=config.production_fit_end_exclusive,
        training_rows=len(training),
        training_fixtures=len({row.fixture_id for row in training}),
        training_players=len(players),
        assisted_goal_probability=assisted_goal_probability,
        minute_bin_upper_bounds=config.starter_minute_bins,
        position_parameters=positions,
        player_parameters=players,
        team_parameters=teams,
        lineup_adjustment_status=(
            "candidate_only_requires_timestamp_safe_prospective_gate"
        ),
        apply_to_public_champion=False,
    )


def predict_confirmed_lineup(
    lineup: ConfirmedLineupFixture,
    model: ConfirmedLineupPlayerModel,
    config: PlayerHierarchyConfig,
    *,
    base_prediction_at: datetime,
    base_model_version: str,
    base_model_sha256: str,
    base_home_expected_goals: float,
    base_away_expected_goals: float,
) -> ConfirmedLineupPrediction:
    if lineup.prediction_at >= lineup.kickoff:
        raise PlayerModelError("Confirmed lineup was not retrieved before kickoff")
    if base_prediction_at >= lineup.prediction_at:
        raise PlayerModelError("Base prediction must strictly precede lineup retrieval")
    if min(base_home_expected_goals, base_away_expected_goals) <= 0:
        raise PlayerModelError("Base expected-goal rates must be positive")
    players = {item.player_id: item for item in model.player_parameters}
    positions = {item.position_code: item for item in model.position_parameters}
    teams = {item.team_id: item for item in model.team_parameters}
    output: list[PlayerPropPrediction] = []
    team_outputs = {}
    for team_id, base_rate in (
        (lineup.home_team_id, base_home_expected_goals),
        (lineup.away_team_id, base_away_expected_goals),
    ):
        team_lineup = [item for item in lineup.players if item.team_id == team_id]
        starters = [item for item in team_lineup if item.selection_role == "starter"]
        if len(starters) != 11:
            raise PlayerModelError(f"Team does not have exactly 11 starters: {team_id}")
        prepared = []
        for item in starters:
            player, position = _parameters_for_lineup_player(item, players, positions)
            expected_minutes = _expected_minutes(
                player.starter_minute_bin_probabilities,
                position.starter_minute_bin_means,
            )
            prepared.append((item, player, position, expected_minutes))
        raw_goal_weights = [
            player.goal_rate_per_minute * minutes
            for _, player, _, minutes in prepared
        ]
        raw_assist_weights = [
            player.assist_rate_per_minute * minutes
            for _, player, _, minutes in prepared
        ]
        score_shares = _normalized_shares(
            raw_goal_weights, 1.0 - config.unattributed_goal_share
        )
        assist_per_goal = _capped_assist_shares(
            raw_assist_weights,
            model.assisted_goal_probability,
            score_shares,
        )
        for index, (item, player, _, expected_minutes) in enumerate(prepared):
            goal_lambda = base_rate * score_shares[index]
            assist_lambda = base_rate * assist_per_goal[index]
            warnings = []
            if player.history_minutes < config.minimum_player_history_minutes_for_full_label:
                warnings.append("sparse_player_history_strong_position_shrinkage")
            if player.history_minutes == 0:
                warnings.append("position_prior_only")
            output.append(
                PlayerPropPrediction(
                    player_id=item.player_id,
                    team_id=team_id,
                    position_code=player.position_code,
                    selection_role="starter",
                    history_minutes=player.history_minutes,
                    history_starts=player.history_starts,
                    expected_minutes=expected_minutes,
                    minute_bin_probabilities=player.starter_minute_bin_probabilities,
                    expected_goals=goal_lambda,
                    goal_count_probabilities_0_1_2_3_plus=_poisson_0_1_2_3_plus(goal_lambda),
                    anytime_goal_probability=1.0 - math.exp(-goal_lambda),
                    expected_assists=assist_lambda,
                    assist_count_probabilities_0_1_2_3_plus=_poisson_0_1_2_3_plus(assist_lambda),
                    anytime_assist_probability=1.0 - math.exp(-assist_lambda),
                    score_or_assist_probability=(
                        1.0 - math.exp(-base_rate * (score_shares[index] + assist_per_goal[index]))
                    ),
                    warnings=tuple(warnings),
                )
            )
        for item in team_lineup:
            if item.selection_role == "starter":
                continue
            player, _, = _parameters_for_lineup_player(item, players, positions)
            output.append(
                PlayerPropPrediction(
                    player_id=item.player_id,
                    team_id=team_id,
                    position_code=player.position_code,
                    selection_role=item.selection_role,
                    history_minutes=player.history_minutes,
                    history_starts=player.history_starts,
                    expected_minutes=None,
                    minute_bin_probabilities=None,
                    expected_goals=None,
                    goal_count_probabilities_0_1_2_3_plus=None,
                    anytime_goal_probability=None,
                    expected_assists=None,
                    assist_count_probabilities_0_1_2_3_plus=None,
                    anytime_assist_probability=None,
                    score_or_assist_probability=None,
                    warnings=("substitute_appearance_target_not_semantically_validated",),
                )
            )
        actual_index = math.fsum(raw_goal_weights)
        team_param = teams.get(team_id)
        typical_index = actual_index if team_param is None else team_param.typical_attack_index
        history = 0 if team_param is None else team_param.lineup_history_matches
        reliability = min(1.0, history / config.team_history_matches_for_full_weight)
        raw_log_delta = 0.0
        if actual_index > 0 and typical_index > 0:
            raw_log_delta = math.log(actual_index / typical_index)
        candidate_delta = _clamp(
            config.candidate_log_rate_coefficient * reliability * raw_log_delta,
            -config.maximum_absolute_log_rate_adjustment,
            config.maximum_absolute_log_rate_adjustment,
        )
        candidate_rate = base_rate * math.exp(candidate_delta)
        goal_sum = math.fsum(base_rate * share for share in score_shares)
        assist_sum = math.fsum(base_rate * share for share in assist_per_goal)
        team_outputs[team_id] = TeamLineupPrediction(
            team_id=team_id,
            base_expected_goals=base_rate,
            candidate_expected_goals=candidate_rate,
            raw_lineup_attack_index=actual_index,
            typical_lineup_attack_index=typical_index,
            lineup_history_matches=history,
            candidate_log_rate_adjustment=candidate_delta,
            player_expected_goals_sum=goal_sum,
            residual_expected_goals=base_rate - goal_sum,
            player_expected_assists_sum=assist_sum,
            expected_unassisted_goals=base_rate - assist_sum,
            authorized_to_replace_champion_rate=False,
        )
    warnings = [
        "confirmed_lineup_model_is_prospective_shadow_only",
        "candidate_team_rate_adjustment_not_authorized_for_champion_replacement",
        "defensive_lineup_adjustment_not_available",
        "first_scorer_requires_separate_event_time_model",
    ]
    return ConfirmedLineupPrediction(
        model_version=model.model_version,
        fixture_id=lineup.fixture_id,
        information_state=model.information_state,
        prediction_at=lineup.prediction_at,
        kickoff=lineup.kickoff,
        base_prediction_at=base_prediction_at,
        base_model_version=base_model_version,
        base_model_sha256=base_model_sha256,
        lineup_raw_artifact_id=lineup.raw_artifact_id,
        lineup_schedule_observation_id=lineup.schedule_observation_id,
        home=team_outputs[lineup.home_team_id],
        away=team_outputs[lineup.away_team_id],
        players=tuple(sorted(output, key=lambda item: (item.team_id, item.selection_role, item.player_id))),
        warnings=tuple(warnings),
    )


def evaluate_player_components(
    rows: list[PlayerMatchTarget],
    config: PlayerHierarchyConfig,
) -> tuple[list[PlayerDiagnosticPrediction], dict]:
    """Chronological starter-only component diagnostic.

    Actual starter status is a post-match label in this warehouse. These rows are
    intentionally marked non-promotable and stop before the prospective period.
    """

    candidates = [
        row for row in rows if row.kickoff < config.forbidden_prospective_start
    ]
    warmup = [
        row for row in candidates if row.result_available_at < config.diagnostic_start
    ]
    if not warmup:
        raise PlayerModelError("Component diagnostic requires a historical warmup")
    priors = {
        item.position_code: item
        for item in _fit_position_parameters(warmup, config)
    }
    predictions_by_time: dict[datetime, list[PlayerMatchTarget]] = defaultdict(list)
    updates_by_time: dict[datetime, list[PlayerMatchTarget]] = defaultdict(list)
    for row in candidates:
        predictions_by_time[row.kickoff].append(row)
        updates_by_time[row.result_available_at].append(row)
    state: dict[str, list[PlayerMatchTarget]] = defaultdict(list)
    output = []
    for at in sorted(set(predictions_by_time) | set(updates_by_time)):
        if at >= config.diagnostic_start:
            for row in sorted(
                predictions_by_time.get(at, []),
                key=lambda item: (item.fixture_id, item.team_id, item.player_id),
            ):
                if not row.started:
                    continue
                prior = priors[row.position_code]
                player = _fit_player_parameters(
                    row.player_id,
                    state.get(row.player_id, []),
                    priors,
                    config,
                    fallback_position=row.position_code,
                )
                expected_minutes = _expected_minutes(
                    player.starter_minute_bin_probabilities,
                    prior.starter_minute_bin_means,
                )
                goal_probability = 1.0 - math.exp(
                    -player.goal_rate_per_minute * expected_minutes
                )
                assist_probability = 1.0 - math.exp(
                    -player.assist_rate_per_minute * expected_minutes
                )
                baseline_minutes = _expected_minutes(
                    prior.starter_minute_bin_probabilities,
                    prior.starter_minute_bin_means,
                )
                output.append(
                    PlayerDiagnosticPrediction(
                        fixture_id=row.fixture_id,
                        kickoff=row.kickoff,
                        player_id=row.player_id,
                        position_code=row.position_code,
                        actual_minutes=row.minutes_played,
                        actual_goal=row.goals > 0,
                        actual_assist=row.assists > 0,
                        history_minutes=player.history_minutes,
                        history_starts=player.history_starts,
                        expected_minutes=expected_minutes,
                        minute_bin_probability=player.starter_minute_bin_probabilities[
                            _minute_bin(row.minutes_played, config.starter_minute_bins)
                        ],
                        goal_probability=goal_probability,
                        assist_probability=assist_probability,
                        baseline_goal_probability=1.0 - math.exp(
                            -prior.goal_rate_per_minute * baseline_minutes
                        ),
                        baseline_assist_probability=1.0 - math.exp(
                            -prior.assist_rate_per_minute * baseline_minutes
                        ),
                    )
                )
        # Strict inequality is enforced by predicting before applying updates at
        # the same timestamp.
        for row in updates_by_time.get(at, []):
            state[row.player_id].append(row)
    if not output:
        raise PlayerModelError("No starter diagnostic rows in the configured window")
    return output, _summarize_diagnostics(output, config)


def player_model_sha256(model: ConfirmedLineupPlayerModel) -> str:
    value = asdict(model)
    value["fit_end_exclusive"] = model.fit_end_exclusive.isoformat()
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def load_confirmed_lineup_player_model(path: Path) -> ConfirmedLineupPlayerModel:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
    value = raw.get("model", raw)
    positions = tuple(PositionParameters(**item) for item in value.pop("position_parameters"))
    players = tuple(PlayerParameters(**item) for item in value.pop("player_parameters"))
    teams = tuple(TeamLineupParameters(**item) for item in value.pop("team_parameters"))
    value["fit_end_exclusive"] = datetime.fromisoformat(value["fit_end_exclusive"])
    value["minute_bin_upper_bounds"] = tuple(value["minute_bin_upper_bounds"])
    model = ConfirmedLineupPlayerModel(
        position_parameters=positions,
        player_parameters=players,
        team_parameters=teams,
        **value,
    )
    expected = raw.get("logical_model_sha256")
    if expected is not None and expected != player_model_sha256(model):
        raise PlayerModelError("Player model logical hash mismatch")
    return model


def _fit_position_parameters(
    rows: list[PlayerMatchTarget],
    config: PlayerHierarchyConfig,
) -> tuple[PositionParameters, ...]:
    grouped: dict[str, list[PlayerMatchTarget]] = defaultdict(list)
    for row in rows:
        if row.position_code in config.supported_positions:
            grouped[row.position_code].append(row)
    output = []
    for position in config.supported_positions:
        values = grouped.get(position, [])
        minutes = sum(row.minutes_played for row in values)
        starts = [row for row in values if row.started]
        if minutes <= 0 or not starts:
            raise PlayerModelError(f"Position prior lacks evidence: {position}")
        counts = [0] * len(config.starter_minute_bins)
        sums = [0.0] * len(config.starter_minute_bins)
        for row in starts:
            index = _minute_bin(row.minutes_played, config.starter_minute_bins)
            counts[index] += 1
            sums[index] += row.minutes_played
        probabilities = _dirichlet_probabilities(counts, prior=1.0)
        means = tuple(
            sums[index] / count if count else _bin_default_mean(index, config.starter_minute_bins)
            for index, count in enumerate(counts)
        )
        output.append(
            PositionParameters(
                position_code=position,
                history_minutes=minutes,
                history_starts=len(starts),
                goal_rate_per_minute=sum(row.goals for row in values) / minutes,
                assist_rate_per_minute=sum(row.assists for row in values) / minutes,
                starter_minute_bin_probabilities=probabilities,
                starter_minute_bin_means=means,
            )
        )
    return tuple(output)


def _fit_player_parameters(
    player_id: str,
    rows: list[PlayerMatchTarget],
    positions: dict[str, PositionParameters],
    config: PlayerHierarchyConfig,
    *,
    fallback_position: str | None = None,
) -> PlayerParameters:
    position = fallback_position or _modal_position(rows)
    if position not in positions:
        raise PlayerModelError(f"Unsupported player position: {position}")
    prior = positions[position]
    minutes = sum(row.minutes_played for row in rows)
    starts = [row for row in rows if row.started]
    goal_rate = (
        sum(row.goals for row in rows)
        + config.goal_rate_prior_minutes * prior.goal_rate_per_minute
    ) / (minutes + config.goal_rate_prior_minutes)
    assist_rate = (
        sum(row.assists for row in rows)
        + config.assist_rate_prior_minutes * prior.assist_rate_per_minute
    ) / (minutes + config.assist_rate_prior_minutes)
    counts = [0] * len(config.starter_minute_bins)
    for row in starts:
        counts[_minute_bin(row.minutes_played, config.starter_minute_bins)] += 1
    denominator = len(starts) + config.starter_minutes_prior_appearances
    probabilities = tuple(
        (
            counts[index]
            + config.starter_minutes_prior_appearances
            * prior.starter_minute_bin_probabilities[index]
        )
        / denominator
        for index in range(len(counts))
    )
    expected = _expected_minutes(probabilities, prior.starter_minute_bin_means)
    return PlayerParameters(
        player_id=player_id,
        position_code=position,
        history_minutes=minutes,
        history_starts=len(starts),
        goals=sum(row.goals for row in rows),
        assists=sum(row.assists for row in rows),
        goal_rate_per_minute=goal_rate,
        assist_rate_per_minute=assist_rate,
        starter_minute_bin_probabilities=probabilities,
        expected_starter_minutes=expected,
    )


def _fit_team_typical_lineups(
    rows: list[PlayerMatchTarget],
    players: dict[str, PlayerParameters],
    positions: dict[str, PositionParameters],
    config: PlayerHierarchyConfig,
) -> tuple[TeamLineupParameters, ...]:
    fixtures: dict[tuple[str, str], list[PlayerMatchTarget]] = defaultdict(list)
    kickoff: dict[tuple[str, str], datetime] = {}
    for row in rows:
        if row.started:
            key = (row.fixture_id, row.team_id)
            fixtures[key].append(row)
            kickoff[key] = row.kickoff
    team_values: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for key, values in fixtures.items():
        if len(values) != 11:
            continue
        index = 0.0
        for row in values:
            player = players[row.player_id]
            position = positions[player.position_code]
            expected = _expected_minutes(
                player.starter_minute_bin_probabilities,
                position.starter_minute_bin_means,
            )
            index += player.goal_rate_per_minute * expected
        if index > 0:
            team_values[key[1]].append((kickoff[key], index))
    output = []
    for team_id, values in sorted(team_values.items()):
        recent = sorted(values)[-config.team_history_matches_for_full_weight :]
        output.append(
            TeamLineupParameters(
                team_id=team_id,
                lineup_history_matches=len(recent),
                typical_attack_index=math.fsum(value for _, value in recent) / len(recent),
            )
        )
    return tuple(output)


def _parameters_for_lineup_player(item, players, positions):
    player = players.get(item.player_id)
    if player is not None:
        return player, positions[player.position_code]
    position_code = item.position_code[:1].upper() if item.position_code else ""
    if position_code not in positions:
        raise PlayerModelError(
            f"Unknown player has no supported position: {item.player_id}"
        )
    position = positions[position_code]
    player = PlayerParameters(
        player_id=item.player_id,
        position_code=position_code,
        history_minutes=0,
        history_starts=0,
        goals=0,
        assists=0,
        goal_rate_per_minute=position.goal_rate_per_minute,
        assist_rate_per_minute=position.assist_rate_per_minute,
        starter_minute_bin_probabilities=position.starter_minute_bin_probabilities,
        expected_starter_minutes=_expected_minutes(
            position.starter_minute_bin_probabilities,
            position.starter_minute_bin_means,
        ),
    )
    return player, position


def _normalized_shares(weights: list[float], total: float) -> tuple[float, ...]:
    denominator = math.fsum(weights)
    if denominator <= 0:
        raise PlayerModelError("Player allocation weights must have positive mass")
    shares = tuple(total * value / denominator for value in weights)
    if not math.isclose(math.fsum(shares), total, abs_tol=1e-12):
        raise PlayerModelError("Player shares do not reconcile")
    return shares


def _capped_assist_shares(
    weights: list[float],
    total: float,
    scoring_shares: tuple[float, ...],
) -> tuple[float, ...]:
    shares = list(_normalized_shares(weights, total))
    active = set(range(len(shares)))
    for _ in range(len(shares) + 1):
        overflow = 0.0
        newly_capped = set()
        for index in active:
            cap = 1.0 - scoring_shares[index]
            if shares[index] > cap:
                overflow += shares[index] - cap
                shares[index] = cap
                newly_capped.add(index)
        active -= newly_capped
        if overflow <= 1e-15:
            break
        weight_sum = math.fsum(weights[index] for index in active)
        if not active or weight_sum <= 0:
            raise PlayerModelError("Cannot reconcile scorer-assister exclusivity")
        for index in active:
            shares[index] += overflow * weights[index] / weight_sum
    if not math.isclose(math.fsum(shares), total, abs_tol=1e-10):
        raise PlayerModelError("Assist shares do not reconcile")
    if any(shares[i] + scoring_shares[i] > 1.0 + 1e-12 for i in range(len(shares))):
        raise PlayerModelError("Player cannot score and assist the same goal")
    return tuple(shares)


def _poisson_0_1_2_3_plus(rate: float) -> tuple[float, float, float, float]:
    p0 = math.exp(-rate)
    p1 = p0 * rate
    p2 = p1 * rate / 2.0
    tail = max(0.0, 1.0 - p0 - p1 - p2)
    probabilities = (p0, p1, p2, tail)
    total = math.fsum(probabilities)
    return tuple(value / total for value in probabilities)  # type: ignore[return-value]


def _minute_bin(minutes: int, upper_bounds: tuple[int, ...]) -> int:
    for index, bound in enumerate(upper_bounds):
        if minutes <= bound:
            return index
    raise PlayerModelError(f"Minutes exceed configured support: {minutes}")


def _bin_default_mean(index: int, upper_bounds: tuple[int, ...]) -> float:
    lower = 1 if index == 0 else upper_bounds[index - 1] + 1
    return (lower + upper_bounds[index]) / 2.0


def _dirichlet_probabilities(counts: list[int], *, prior: float) -> tuple[float, ...]:
    denominator = sum(counts) + prior * len(counts)
    return tuple((count + prior) / denominator for count in counts)


def _expected_minutes(probabilities, means) -> float:
    if len(probabilities) != len(means):
        raise PlayerModelError("Minute probability and mean vectors differ")
    if not math.isclose(math.fsum(probabilities), 1.0, abs_tol=1e-10):
        raise PlayerModelError("Minute probabilities do not sum to one")
    return math.fsum(p * m for p, m in zip(probabilities, means, strict=True))


def _modal_position(rows: list[PlayerMatchTarget]) -> str:
    if not rows:
        raise PlayerModelError("Cannot infer position without history")
    counts = Counter(row.position_code for row in rows)
    return sorted(counts, key=lambda value: (-counts[value], value))[0]


def _summarize_diagnostics(
    rows: list[PlayerDiagnosticPrediction], config: PlayerHierarchyConfig
) -> dict:
    def log_loss(probability: float, outcome: bool) -> float:
        p = _clamp(probability, 1e-12, 1.0 - 1e-12)
        return -math.log(p if outcome else 1.0 - p)

    grouped: dict[str, list[PlayerDiagnosticPrediction]] = defaultdict(list)
    for row in rows:
        grouped[f"{row.kickoff.year:04d}"].append(row)
    metrics = []
    for fold, values in sorted(grouped.items()):
        metrics.append(
            {
                "fold": fold,
                "rows": len(values),
                "players": len({row.player_id for row in values}),
                "fixtures": len({row.fixture_id for row in values}),
                "anytime_goal_log_loss": math.fsum(
                    log_loss(row.goal_probability, row.actual_goal) for row in values
                ) / len(values),
                "position_baseline_goal_log_loss": math.fsum(
                    log_loss(row.baseline_goal_probability, row.actual_goal)
                    for row in values
                ) / len(values),
                "anytime_assist_log_loss": math.fsum(
                    log_loss(row.assist_probability, row.actual_assist) for row in values
                ) / len(values),
                "position_baseline_assist_log_loss": math.fsum(
                    log_loss(row.baseline_assist_probability, row.actual_assist)
                    for row in values
                ) / len(values),
                "starter_minutes_log_score": -math.fsum(
                    math.log(max(row.minute_bin_probability, 1e-12)) for row in values
                ) / len(values),
                "starter_minutes_mean_absolute_error": math.fsum(
                    abs(row.expected_minutes - row.actual_minutes) for row in values
                ) / len(values),
            }
        )
    comparisons = []
    for target, probability_name, baseline_name in (
        ("goal", "goal_probability", "baseline_goal_probability"),
        ("assist", "assist_probability", "baseline_assist_probability"),
    ):
        blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row in rows:
            outcome = row.actual_goal if target == "goal" else row.actual_assist
            blocks[(row.kickoff.year, row.kickoff.month)].append(
                log_loss(getattr(row, probability_name), outcome)
                - log_loss(getattr(row, baseline_name), outcome)
            )
        lower, upper, better = _paired_block_interval(
            blocks,
            replicates=config.bootstrap_replicates,
            seed=config.bootstrap_seed + (0 if target == "goal" else 1),
        )
        all_deltas = [value for values in blocks.values() for value in values]
        comparisons.append(
            {
                "target": target,
                "metric": "anytime_log_loss",
                "challenger": "player_hierarchical_rate_and_minutes",
                "baseline": "position_rate_and_minutes_prior",
                "rows": len(all_deltas),
                "calendar_month_blocks": len(blocks),
                "mean_delta_challenger_minus_baseline": math.fsum(all_deltas)
                / len(all_deltas),
                "paired_month_block_bootstrap_95_lower": lower,
                "paired_month_block_bootstrap_95_upper": upper,
                "bootstrap_probability_challenger_is_better": better,
                "lower_is_better": True,
            }
        )
    return {
        "evaluation_version": "confirmed_lineup_player_component_diagnostic_v1",
        "status": "diagnostic_only_not_promotion_evidence",
        "historical_confirmed_lineup_evaluation": False,
        "actual_starter_status_source": "postmatch_label_not_timestamped_pregame",
        "prospective_period_accessed": False,
        "forbidden_prospective_start": config.forbidden_prospective_start.isoformat(),
        "rows": len(rows),
        "fold_metrics": metrics,
        "paired_month_block_comparisons": comparisons,
        "calibration": {
            "anytime_goal": _calibration_bins(
                rows, "goal_probability", "actual_goal"
            ),
            "anytime_assist": _calibration_bins(
                rows, "assist_probability", "actual_assist"
            ),
        },
        "calibration_status": "blocked_until_timestamp_safe_confirmed_lineup_cohort",
    }


def _paired_block_interval(
    blocks: dict[tuple[int, int], list[float]],
    *,
    replicates: int,
    seed: int,
) -> tuple[float, float, float]:
    summaries = [
        (math.fsum(values), len(values)) for _, values in sorted(blocks.items())
    ]
    if not summaries:
        raise PlayerModelError("Paired bootstrap requires calendar-month blocks")
    generator = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sampled = [summaries[generator.randrange(len(summaries))] for _ in summaries]
        estimates.append(
            math.fsum(item[0] for item in sampled)
            / sum(item[1] for item in sampled)
        )
    estimates.sort()
    lower = estimates[int(0.025 * (replicates - 1))]
    upper = estimates[int(0.975 * (replicates - 1))]
    better = sum(value < 0 for value in estimates) / replicates
    return lower, upper, better


def _calibration_bins(rows, probability_name: str, outcome_name: str) -> list[dict]:
    edges = (0.0, 0.02, 0.05, 0.10, 0.20, 1.0000000001)
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(len(edges) - 1)]
    for row in rows:
        probability = getattr(row, probability_name)
        for index in range(len(edges) - 1):
            if edges[index] <= probability < edges[index + 1]:
                bins[index].append((probability, getattr(row, outcome_name)))
                break
    output = []
    for index, values in enumerate(bins):
        if not values:
            continue
        output.append(
            {
                "lower_inclusive": edges[index],
                "upper_exclusive": min(1.0, edges[index + 1]),
                "rows": len(values),
                "mean_probability": math.fsum(value[0] for value in values)
                / len(values),
                "observed_frequency": sum(value[1] for value in values)
                / len(values),
            }
        )
    return output


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))
