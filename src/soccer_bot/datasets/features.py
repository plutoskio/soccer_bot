from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json
from soccer_bot.datasets.targets import RegulationScoreTarget


class FeatureConfigurationError(ValueError):
    """Raised when feature configuration or chronological inputs are unsafe."""


@dataclass(frozen=True)
class HorizonDefinition:
    information_state: str
    minutes_before_kickoff: int
    require_no_intervening_team_fixture: bool


@dataclass(frozen=True)
class TeamStateFeatureConfig:
    feature_version: str
    result_availability_delay_minutes: int
    horizons: tuple[HorizonDefinition, ...]
    team_prior_variance: float
    team_half_life_days: float
    competition_goal_prior_mean: float
    competition_goal_prior_variance: float
    competition_home_prior_mean: float
    competition_home_prior_variance: float
    competition_half_life_days: float
    minimum_expected_goals: float
    maximum_expected_goals: float
    cold_start_match_threshold: int


@dataclass
class _ScalarState:
    mean: float
    variance: float
    prior_mean: float
    prior_variance: float
    last_time: datetime | None = None

    def project(self, at: datetime, half_life_days: float) -> None:
        if self.last_time is None:
            self.last_time = at
            return
        elapsed_days = max(0.0, (at - self.last_time).total_seconds() / 86400.0)
        if elapsed_days == 0:
            return
        phi = math.exp(-math.log(2.0) * elapsed_days / half_life_days)
        self.mean = self.prior_mean + phi * (self.mean - self.prior_mean)
        self.variance = (
            phi * phi * self.variance
            + (1.0 - phi * phi) * self.prior_variance
        )
        self.last_time = at

    def update(self, gradient: float, information: float) -> None:
        posterior_variance = 1.0 / (1.0 / self.variance + information)
        self.mean += posterior_variance * gradient
        self.variance = posterior_variance


@dataclass
class _TeamState:
    attack: _ScalarState
    defense: _ScalarState
    match_kickoffs: list[datetime]


@dataclass
class _CompetitionState:
    log_goal_level: _ScalarState
    home_advantage: _ScalarState
    matches: int = 0


@dataclass(frozen=True)
class RegulationFeatureRow:
    feature_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    home_goals: int
    away_goals: int
    home_attack_mean: float
    home_attack_std: float
    home_defense_mean: float
    home_defense_std: float
    away_attack_mean: float
    away_attack_std: float
    away_defense_mean: float
    away_defense_std: float
    competition_log_goal_level: float
    competition_log_goal_level_std: float
    competition_home_advantage: float
    competition_home_advantage_std: float
    applied_home_advantage: float
    home_log_matchup_strength: float
    away_log_matchup_strength: float
    expected_home_goals: float
    expected_away_goals: float
    home_history_matches: int
    away_history_matches: int
    competition_history_matches: int
    home_rest_days: float | None
    away_rest_days: float | None
    rest_difference_days: float | None
    home_matches_last_7d: int
    home_matches_last_14d: int
    home_matches_last_30d: int
    away_matches_last_7d: int
    away_matches_last_14d: int
    away_matches_last_30d: int
    home_cold_start: bool
    away_cold_start: bool


@dataclass(frozen=True)
class RegulationInferenceFixture:
    fixture_id: str
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    kickoff: datetime
    allowed_information_states: tuple[str, ...]


@dataclass(frozen=True)
class RegulationInferenceFeatureRow:
    feature_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    home_attack_mean: float
    home_attack_std: float
    home_defense_mean: float
    home_defense_std: float
    away_attack_mean: float
    away_attack_std: float
    away_defense_mean: float
    away_defense_std: float
    competition_log_goal_level: float
    competition_log_goal_level_std: float
    competition_home_advantage: float
    competition_home_advantage_std: float
    applied_home_advantage: float
    home_log_matchup_strength: float
    away_log_matchup_strength: float
    expected_home_goals: float
    expected_away_goals: float
    home_history_matches: int
    away_history_matches: int
    competition_history_matches: int
    home_rest_days: float | None
    away_rest_days: float | None
    rest_difference_days: float | None
    home_matches_last_7d: int
    home_matches_last_14d: int
    home_matches_last_30d: int
    away_matches_last_7d: int
    away_matches_last_14d: int
    away_matches_last_30d: int
    home_cold_start: bool
    away_cold_start: bool


def load_team_state_feature_config(path: Path) -> TeamStateFeatureConfig:
    raw = load_json(path)
    state = raw.get("state_model", {})
    horizons = tuple(
        HorizonDefinition(
            information_state=item["information_state"],
            minutes_before_kickoff=int(item["minutes_before_kickoff"]),
            require_no_intervening_team_fixture=bool(
                item["require_no_intervening_team_fixture"]
            ),
        )
        for item in raw.get("horizons", [])
    )
    if not horizons or len({item.information_state for item in horizons}) != len(horizons):
        raise FeatureConfigurationError("Feature horizons must be non-empty and unique")
    if any(item.minutes_before_kickoff <= 0 for item in horizons):
        raise FeatureConfigurationError("Feature horizon minutes must be positive")
    config = TeamStateFeatureConfig(
        feature_version=str(raw.get("feature_version", "")),
        result_availability_delay_minutes=int(raw["result_availability_delay_minutes"]),
        horizons=horizons,
        team_prior_variance=float(state["team_prior_standard_deviation"]) ** 2,
        team_half_life_days=float(state["team_mean_reversion_half_life_days"]),
        competition_goal_prior_mean=float(state["competition_log_goal_prior_mean"]),
        competition_goal_prior_variance=float(
            state["competition_log_goal_prior_standard_deviation"]
        ) ** 2,
        competition_home_prior_mean=float(
            state["competition_home_advantage_prior_mean"]
        ),
        competition_home_prior_variance=float(
            state["competition_home_advantage_prior_standard_deviation"]
        ) ** 2,
        competition_half_life_days=float(
            state["competition_mean_reversion_half_life_days"]
        ),
        minimum_expected_goals=float(state["minimum_expected_goals"]),
        maximum_expected_goals=float(state["maximum_expected_goals"]),
        cold_start_match_threshold=int(state["cold_start_match_threshold"]),
    )
    if not config.feature_version:
        raise FeatureConfigurationError("feature_version is required")
    if min(
        config.result_availability_delay_minutes,
        config.cold_start_match_threshold,
    ) < 0:
        raise FeatureConfigurationError("Delay and thresholds must be nonnegative")
    if min(
        config.team_prior_variance,
        config.team_half_life_days,
        config.competition_goal_prior_variance,
        config.competition_home_prior_variance,
        config.competition_half_life_days,
        config.minimum_expected_goals,
    ) <= 0:
        raise FeatureConfigurationError("State variances, half-lives, and bounds must be positive")
    if config.maximum_expected_goals <= config.minimum_expected_goals:
        raise FeatureConfigurationError("Expected-goal bounds are invalid")
    return config


class ChronologicalTeamStateBuilder:
    """Build point-in-time features with batched, order-invariant result updates."""

    def __init__(self, config: TeamStateFeatureConfig) -> None:
        self.config = config
        self.teams: dict[str, _TeamState] = {}
        self.competitions: dict[str, _CompetitionState] = {}

    def build(self, targets: list[RegulationScoreTarget]) -> list[RegulationFeatureRow]:
        # A builder may be reused by callers; each build must start from the
        # configured priors rather than carrying state from an earlier run.
        self.teams = {}
        self.competitions = {}
        ordered_targets = sorted(targets, key=lambda row: (row.kickoff, row.fixture_id))
        _validate_unique_targets(ordered_targets)
        team_schedules = _index_team_schedules(ordered_targets)
        snapshots: dict[datetime, list[tuple[RegulationScoreTarget, HorizonDefinition]]] = defaultdict(list)
        results: dict[datetime, list[RegulationScoreTarget]] = defaultdict(list)
        for target in ordered_targets:
            for horizon in self.config.horizons:
                prediction_at = target.kickoff - timedelta(
                    minutes=horizon.minutes_before_kickoff
                )
                if horizon.require_no_intervening_team_fixture and _has_intervening_fixture(
                    target, prediction_at, team_schedules
                ):
                    continue
                snapshots[prediction_at].append((target, horizon))
            available_at = target.kickoff + timedelta(
                minutes=self.config.result_availability_delay_minutes
            )
            results[available_at].append(target)

        rows = []
        for timestamp in sorted(set(snapshots) | set(results)):
            for target, horizon in sorted(
                snapshots.get(timestamp, []),
                key=lambda item: (item[0].fixture_id, item[1].information_state),
            ):
                rows.append(self._snapshot(target, horizon, timestamp))
            if timestamp in results:
                self._apply_result_batch(results[timestamp], timestamp)
        return sorted(rows, key=lambda row: (row.kickoff, row.fixture_id, row.information_state))

    def build_inference(
        self,
        historical_targets: list[RegulationScoreTarget],
        upcoming_fixtures: list[RegulationInferenceFixture],
        *,
        as_of: datetime,
    ) -> list[RegulationInferenceFeatureRow]:
        """Replay historical state and snapshot only due, still-upcoming fixtures."""

        self.teams = {}
        self.competitions = {}
        ordered_targets = sorted(
            historical_targets, key=lambda row: (row.kickoff, row.fixture_id)
        )
        ordered_fixtures = sorted(
            upcoming_fixtures, key=lambda row: (row.kickoff, row.fixture_id)
        )
        _validate_unique_subjects([*ordered_targets, *ordered_fixtures])
        team_schedules = _index_team_schedules(
            [*ordered_targets, *ordered_fixtures]
        )
        snapshots: dict[
            datetime, list[tuple[RegulationInferenceFixture, HorizonDefinition]]
        ] = defaultdict(list)
        for fixture in ordered_fixtures:
            if fixture.kickoff <= as_of:
                continue
            allowed = set(fixture.allowed_information_states)
            for horizon in self.config.horizons:
                prediction_at = fixture.kickoff - timedelta(
                    minutes=horizon.minutes_before_kickoff
                )
                if prediction_at > as_of or horizon.information_state not in allowed:
                    continue
                if (
                    horizon.require_no_intervening_team_fixture
                    and _has_intervening_fixture(
                        fixture, prediction_at, team_schedules
                    )
                ):
                    continue
                snapshots[prediction_at].append((fixture, horizon))
        if not snapshots:
            return []
        latest_snapshot = max(snapshots)
        results: dict[datetime, list[RegulationScoreTarget]] = defaultdict(list)
        for target in ordered_targets:
            available_at = target.kickoff + timedelta(
                minutes=self.config.result_availability_delay_minutes
            )
            if available_at <= latest_snapshot:
                results[available_at].append(target)
        rows = []
        for timestamp in sorted(set(snapshots) | set(results)):
            for fixture, horizon in sorted(
                snapshots.get(timestamp, []),
                key=lambda item: (item[0].fixture_id, item[1].information_state),
            ):
                values = self._snapshot_values(fixture, timestamp)
                rows.append(
                    RegulationInferenceFeatureRow(
                        information_state=horizon.information_state,
                        **values,
                    )
                )
            if timestamp in results:
                self._apply_result_batch(results[timestamp], timestamp)
        return sorted(
            rows,
            key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
        )

    def _new_team(self) -> _TeamState:
        return _TeamState(
            attack=_ScalarState(0.0, self.config.team_prior_variance, 0.0, self.config.team_prior_variance),
            defense=_ScalarState(0.0, self.config.team_prior_variance, 0.0, self.config.team_prior_variance),
            match_kickoffs=[],
        )

    def _new_competition(self) -> _CompetitionState:
        return _CompetitionState(
            log_goal_level=_ScalarState(
                self.config.competition_goal_prior_mean,
                self.config.competition_goal_prior_variance,
                self.config.competition_goal_prior_mean,
                self.config.competition_goal_prior_variance,
            ),
            home_advantage=_ScalarState(
                self.config.competition_home_prior_mean,
                self.config.competition_home_prior_variance,
                self.config.competition_home_prior_mean,
                self.config.competition_home_prior_variance,
            ),
        )

    def _project(self, target: RegulationScoreTarget, at: datetime) -> tuple[_TeamState, _TeamState, _CompetitionState]:
        home = self.teams.setdefault(target.home_team_id, self._new_team())
        away = self.teams.setdefault(target.away_team_id, self._new_team())
        competition = self.competitions.setdefault(target.competition_id, self._new_competition())
        for scalar in (home.attack, home.defense, away.attack, away.defense):
            scalar.project(at, self.config.team_half_life_days)
        for scalar in (competition.log_goal_level, competition.home_advantage):
            scalar.project(at, self.config.competition_half_life_days)
        return home, away, competition

    def _expected(self, home: _TeamState, away: _TeamState, competition: _CompetitionState, neutral: bool) -> tuple[float, float, float]:
        applied_home = 0.0 if neutral else competition.home_advantage.mean
        home_log = competition.log_goal_level.mean + applied_home + home.attack.mean - away.defense.mean
        away_log = competition.log_goal_level.mean + away.attack.mean - home.defense.mean
        return (
            _clamp(math.exp(home_log), self.config.minimum_expected_goals, self.config.maximum_expected_goals),
            _clamp(math.exp(away_log), self.config.minimum_expected_goals, self.config.maximum_expected_goals),
            applied_home,
        )

    def _snapshot(self, target: RegulationScoreTarget, horizon: HorizonDefinition, at: datetime) -> RegulationFeatureRow:
        return RegulationFeatureRow(
            information_state=horizon.information_state,
            home_goals=target.home_goals,
            away_goals=target.away_goals,
            **self._snapshot_values(target, at),
        )

    def _snapshot_values(self, target, at: datetime) -> dict:
        home, away, competition = self._project(target, at)
        expected_home, expected_away, applied_home = self._expected(
            home, away, competition, target.neutral_venue
        )
        home_rest = _rest_days(home.match_kickoffs, target.kickoff)
        away_rest = _rest_days(away.match_kickoffs, target.kickoff)
        return dict(
            feature_version=self.config.feature_version,
            fixture_id=target.fixture_id,
            prediction_at=at,
            kickoff=target.kickoff,
            competition_id=target.competition_id,
            season_id=target.season_id,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
            neutral_venue=target.neutral_venue,
            home_attack_mean=home.attack.mean,
            home_attack_std=math.sqrt(home.attack.variance),
            home_defense_mean=home.defense.mean,
            home_defense_std=math.sqrt(home.defense.variance),
            away_attack_mean=away.attack.mean,
            away_attack_std=math.sqrt(away.attack.variance),
            away_defense_mean=away.defense.mean,
            away_defense_std=math.sqrt(away.defense.variance),
            competition_log_goal_level=competition.log_goal_level.mean,
            competition_log_goal_level_std=math.sqrt(competition.log_goal_level.variance),
            competition_home_advantage=competition.home_advantage.mean,
            competition_home_advantage_std=math.sqrt(competition.home_advantage.variance),
            applied_home_advantage=applied_home,
            home_log_matchup_strength=home.attack.mean - away.defense.mean,
            away_log_matchup_strength=away.attack.mean - home.defense.mean,
            expected_home_goals=expected_home,
            expected_away_goals=expected_away,
            home_history_matches=len(home.match_kickoffs),
            away_history_matches=len(away.match_kickoffs),
            competition_history_matches=competition.matches,
            home_rest_days=home_rest,
            away_rest_days=away_rest,
            rest_difference_days=None if home_rest is None or away_rest is None else home_rest - away_rest,
            home_matches_last_7d=_recent_count(home.match_kickoffs, target.kickoff, 7),
            home_matches_last_14d=_recent_count(home.match_kickoffs, target.kickoff, 14),
            home_matches_last_30d=_recent_count(home.match_kickoffs, target.kickoff, 30),
            away_matches_last_7d=_recent_count(away.match_kickoffs, target.kickoff, 7),
            away_matches_last_14d=_recent_count(away.match_kickoffs, target.kickoff, 14),
            away_matches_last_30d=_recent_count(away.match_kickoffs, target.kickoff, 30),
            home_cold_start=len(home.match_kickoffs) < self.config.cold_start_match_threshold,
            away_cold_start=len(away.match_kickoffs) < self.config.cold_start_match_threshold,
        )

    def _apply_result_batch(self, batch: list[RegulationScoreTarget], at: datetime) -> None:
        gradients: dict[int, float] = defaultdict(float)
        information: dict[int, float] = defaultdict(float)
        states: dict[int, _ScalarState] = {}
        projected = []
        for target in sorted(batch, key=lambda row: row.fixture_id):
            home, away, competition = self._project(target, at)
            expected_home, expected_away, applied_home = self._expected(
                home, away, competition, target.neutral_venue
            )
            residual_home = target.home_goals - expected_home
            residual_away = target.away_goals - expected_away
            terms = [
                (competition.log_goal_level, residual_home + residual_away, expected_home + expected_away),
                (home.attack, residual_home, expected_home),
                (away.defense, -residual_home, expected_home),
                (away.attack, residual_away, expected_away),
                (home.defense, -residual_away, expected_away),
            ]
            if not target.neutral_venue:
                terms.append((competition.home_advantage, residual_home, expected_home))
            for state, gradient, info in terms:
                key = id(state)
                states[key] = state
                gradients[key] += gradient
                information[key] += info
            projected.append((target, home, away, competition))
        for key, state in states.items():
            state.update(gradients[key], information[key])
        for target, home, away, competition in projected:
            home.match_kickoffs.append(target.kickoff)
            away.match_kickoffs.append(target.kickoff)
            competition.matches += 1


def feature_rows_sha256(rows: list[RegulationFeatureRow]) -> str:
    serializable = []
    for row in rows:
        value = asdict(row)
        value["prediction_at"] = row.prediction_at.astimezone(timezone.utc).isoformat()
        value["kickoff"] = row.kickoff.astimezone(timezone.utc).isoformat()
        serializable.append(value)
    body = json.dumps(serializable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_unique_targets(targets: list[RegulationScoreTarget]) -> None:
    _validate_unique_subjects(targets)


def _validate_unique_subjects(targets) -> None:
    fixture_ids = [target.fixture_id for target in targets]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise FeatureConfigurationError(
            "Feature subjects must contain one row per fixture"
        )


def _index_team_schedules(targets) -> dict[str, list[datetime]]:
    schedules: dict[str, list[datetime]] = defaultdict(list)
    for target in targets:
        schedules[target.home_team_id].append(target.kickoff)
        schedules[target.away_team_id].append(target.kickoff)
    for kickoffs in schedules.values():
        kickoffs.sort()
    return schedules


def _has_intervening_fixture(
    target,
    prediction_at: datetime,
    team_schedules: dict[str, list[datetime]],
) -> bool:
    for team_id in (target.home_team_id, target.away_team_id):
        kickoffs = team_schedules[team_id]
        # A kickoff exactly at the evaluation anchor also invalidates the
        # clean horizon: its result is not yet available at that instant.
        next_index = bisect_left(kickoffs, prediction_at)
        if next_index < len(kickoffs) and kickoffs[next_index] < target.kickoff:
            return True
    return False


def _rest_days(kickoffs: list[datetime], target_kickoff: datetime) -> float | None:
    if not kickoffs:
        return None
    return (target_kickoff - max(kickoffs)).total_seconds() / 86400.0


def _recent_count(kickoffs: list[datetime], target_kickoff: datetime, days: int) -> int:
    start = target_kickoff - timedelta(days=days)
    return sum(start <= kickoff < target_kickoff for kickoff in kickoffs)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
