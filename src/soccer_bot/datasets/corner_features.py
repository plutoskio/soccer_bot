from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json
from soccer_bot.datasets.corners import CornerTarget
from soccer_bot.datasets.features import RegulationInferenceFixture


class CornerFeatureError(RuntimeError):
    """Raised when a chronological corner feature would be unsafe."""


@dataclass(frozen=True)
class CornerHorizon:
    information_state: str
    minutes_before_kickoff: int
    require_no_intervening_team_fixture: bool


@dataclass(frozen=True)
class CornerFeatureConfig:
    feature_version: str
    result_availability_delay_minutes: int
    horizons: tuple[CornerHorizon, ...]
    team_prior_variance: float
    team_half_life_days: float
    competition_level_prior_mean: float
    competition_level_prior_variance: float
    competition_home_prior_mean: float
    competition_home_prior_variance: float
    competition_half_life_days: float
    minimum_expected_corners: float
    maximum_expected_corners: float
    cold_start_match_threshold: int


@dataclass(frozen=True)
class CornerFeatureRow:
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
    home_corners: int
    away_corners: int
    expected_home_corners: float
    expected_away_corners: float
    home_attack_mean: float
    home_defense_mean: float
    away_attack_mean: float
    away_defense_mean: float
    competition_log_corner_level: float
    competition_home_advantage: float
    home_history_matches: int
    away_history_matches: int
    competition_history_matches: int
    home_cold_start: bool
    away_cold_start: bool


@dataclass(frozen=True)
class CornerInferenceFeatureRow:
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
    expected_home_corners: float
    expected_away_corners: float
    home_history_matches: int
    away_history_matches: int
    competition_history_matches: int
    home_cold_start: bool
    away_cold_start: bool


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
        elapsed = max(0.0, (at - self.last_time).total_seconds() / 86400.0)
        if elapsed == 0:
            return
        decay = math.exp(-math.log(2.0) * elapsed / half_life_days)
        self.mean = self.prior_mean + decay * (self.mean - self.prior_mean)
        self.variance = (
            decay * decay * self.variance
            + (1.0 - decay * decay) * self.prior_variance
        )
        self.last_time = at

    def update(self, gradient: float, information: float) -> None:
        variance = 1.0 / (1.0 / self.variance + information)
        self.mean += variance * gradient
        self.variance = variance


@dataclass
class _TeamState:
    attack: _ScalarState
    defense: _ScalarState
    matches: int = 0


@dataclass
class _CompetitionState:
    level: _ScalarState
    home_advantage: _ScalarState
    matches: int = 0


def load_corner_feature_config(path: Path) -> CornerFeatureConfig:
    raw = load_json(path)
    if raw.get("parameter_status") != (
        "target_feature_candidate_recipes_and_gate_frozen_before_evaluation"
    ):
        raise CornerFeatureError("Corner feature recipe is not frozen")
    values = raw.get("features")
    if not isinstance(values, dict):
        raise CornerFeatureError("Corner feature configuration is missing")
    config = CornerFeatureConfig(
        feature_version=_string(values, "version"),
        result_availability_delay_minutes=_positive_int(
            values, "result_availability_delay_minutes"
        ),
        horizons=(
            CornerHorizon("pre_lineup_72h_clean_v1", 72 * 60, True),
            CornerHorizon("pre_lineup_24h_v1", 24 * 60, False),
        ),
        team_prior_variance=_positive_float(
            values, "team_log_strength_prior_standard_deviation"
        )
        ** 2,
        team_half_life_days=_positive_float(
            values, "team_mean_reversion_half_life_days"
        ),
        competition_level_prior_mean=float(
            values["competition_log_corner_prior_mean"]
        ),
        competition_level_prior_variance=_positive_float(
            values, "competition_log_corner_prior_standard_deviation"
        )
        ** 2,
        competition_home_prior_mean=float(
            values["competition_home_advantage_prior_mean"]
        ),
        competition_home_prior_variance=_positive_float(
            values, "competition_home_advantage_prior_standard_deviation"
        )
        ** 2,
        competition_half_life_days=_positive_float(
            values, "competition_mean_reversion_half_life_days"
        ),
        minimum_expected_corners=_positive_float(
            values, "minimum_expected_corners"
        ),
        maximum_expected_corners=_positive_float(
            values, "maximum_expected_corners"
        ),
        cold_start_match_threshold=_positive_int(values, "cold_start_below_matches"),
    )
    if config.minimum_expected_corners >= config.maximum_expected_corners:
        raise CornerFeatureError("Corner expectation bounds are invalid")
    return config


class ChronologicalCornerFeatureBuilder:
    """Build corner-rate features using only results available at prediction time."""

    def __init__(self, config: CornerFeatureConfig) -> None:
        self.config = config
        self.teams: dict[str, _TeamState] = {}
        self.competitions: dict[str, _CompetitionState] = {}

    def build(self, targets: list[CornerTarget]) -> list[CornerFeatureRow]:
        self.teams = {}
        self.competitions = {}
        ordered = sorted(targets, key=lambda row: (row.kickoff, row.fixture_id))
        _validate_targets(ordered)
        schedules = _team_schedules(ordered)
        snapshots: dict[datetime, list[tuple[CornerTarget, CornerHorizon]]] = defaultdict(list)
        results: dict[datetime, list[CornerTarget]] = defaultdict(list)
        for target in ordered:
            for horizon in self.config.horizons:
                prediction_at = target.kickoff - timedelta(
                    minutes=horizon.minutes_before_kickoff
                )
                if horizon.require_no_intervening_team_fixture and _intervening(
                    target, prediction_at, schedules
                ):
                    continue
                snapshots[prediction_at].append((target, horizon))
            available_at = target.target_available_at or (
                target.kickoff
                + timedelta(minutes=self.config.result_availability_delay_minutes)
            )
            results[available_at].append(target)
        output = []
        for timestamp in sorted(set(snapshots) | set(results)):
            for target, horizon in sorted(
                snapshots.get(timestamp, []),
                key=lambda item: (item[0].fixture_id, item[1].information_state),
            ):
                output.append(self._snapshot(target, horizon, timestamp))
            if timestamp in results:
                self._observe_batch(results[timestamp], timestamp)
        return sorted(
            output,
            key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
        )

    def build_inference(
        self,
        historical_targets: list[CornerTarget],
        upcoming_fixtures: list[RegulationInferenceFixture],
        *,
        as_of: datetime,
    ) -> list[CornerInferenceFeatureRow]:
        if as_of.tzinfo is None:
            raise CornerFeatureError("Corner inference as_of must be timezone-aware")
        self.teams = {}
        self.competitions = {}
        history = sorted(
            historical_targets, key=lambda row: (row.kickoff, row.fixture_id)
        )
        upcoming = sorted(
            upcoming_fixtures, key=lambda row: (row.kickoff, row.fixture_id)
        )
        _validate_targets(history)
        identifiers = [row.fixture_id for row in [*history, *upcoming]]
        if len(identifiers) != len(set(identifiers)):
            raise CornerFeatureError("Historical and upcoming corner fixtures overlap")
        schedules = _team_schedules([*history, *upcoming])
        snapshots: dict[
            datetime, list[tuple[RegulationInferenceFixture, CornerHorizon]]
        ] = defaultdict(list)
        for fixture in upcoming:
            if fixture.kickoff <= as_of:
                continue
            allowed = set(fixture.allowed_information_states)
            for horizon in self.config.horizons:
                prediction_at = fixture.kickoff - timedelta(
                    minutes=horizon.minutes_before_kickoff
                )
                if prediction_at > as_of or horizon.information_state not in allowed:
                    continue
                if horizon.require_no_intervening_team_fixture and _intervening(
                    fixture, prediction_at, schedules
                ):
                    continue
                snapshots[prediction_at].append((fixture, horizon))
        if not snapshots:
            return []
        latest_snapshot = max(snapshots)
        results: dict[datetime, list[CornerTarget]] = defaultdict(list)
        for target in history:
            available_at = target.target_available_at or (
                target.kickoff
                + timedelta(minutes=self.config.result_availability_delay_minutes)
            )
            if available_at <= latest_snapshot:
                results[available_at].append(target)
        output = []
        for timestamp in sorted(set(snapshots) | set(results)):
            for fixture, horizon in sorted(
                snapshots.get(timestamp, []),
                key=lambda item: (item[0].fixture_id, item[1].information_state),
            ):
                output.append(self._snapshot_inference(fixture, horizon, timestamp))
            if timestamp in results:
                self._observe_batch(results[timestamp], timestamp)
        return sorted(
            output,
            key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
        )

    def _team(self, team_id: str, at: datetime) -> _TeamState:
        state = self.teams.get(team_id)
        if state is None:
            state = _TeamState(
                attack=_ScalarState(0.0, self.config.team_prior_variance, 0.0, self.config.team_prior_variance),
                defense=_ScalarState(0.0, self.config.team_prior_variance, 0.0, self.config.team_prior_variance),
            )
            self.teams[team_id] = state
        state.attack.project(at, self.config.team_half_life_days)
        state.defense.project(at, self.config.team_half_life_days)
        return state

    def _competition(self, competition_id: str, at: datetime) -> _CompetitionState:
        state = self.competitions.get(competition_id)
        if state is None:
            state = _CompetitionState(
                level=_ScalarState(
                    self.config.competition_level_prior_mean,
                    self.config.competition_level_prior_variance,
                    self.config.competition_level_prior_mean,
                    self.config.competition_level_prior_variance,
                ),
                home_advantage=_ScalarState(
                    self.config.competition_home_prior_mean,
                    self.config.competition_home_prior_variance,
                    self.config.competition_home_prior_mean,
                    self.config.competition_home_prior_variance,
                ),
            )
            self.competitions[competition_id] = state
        state.level.project(at, self.config.competition_half_life_days)
        state.home_advantage.project(at, self.config.competition_half_life_days)
        return state

    def _rates(self, target: CornerTarget, at: datetime) -> tuple[float, float]:
        home = self._team(target.home_team_id, at)
        away = self._team(target.away_team_id, at)
        competition = self._competition(target.competition_id, at)
        advantage = 0.0 if target.neutral_venue else competition.home_advantage.mean
        home_rate = _clamp(
            math.exp(competition.level.mean + advantage + home.attack.mean + away.defense.mean),
            self.config.minimum_expected_corners,
            self.config.maximum_expected_corners,
        )
        away_rate = _clamp(
            math.exp(competition.level.mean + away.attack.mean + home.defense.mean),
            self.config.minimum_expected_corners,
            self.config.maximum_expected_corners,
        )
        return home_rate, away_rate

    def _snapshot(
        self, target: CornerTarget, horizon: CornerHorizon, at: datetime
    ) -> CornerFeatureRow:
        home_rate, away_rate = self._rates(target, at)
        home = self.teams[target.home_team_id]
        away = self.teams[target.away_team_id]
        competition = self.competitions[target.competition_id]
        return CornerFeatureRow(
            feature_version=self.config.feature_version,
            fixture_id=target.fixture_id,
            information_state=horizon.information_state,
            prediction_at=at,
            kickoff=target.kickoff,
            competition_id=target.competition_id,
            season_id=target.season_id,
            home_team_id=target.home_team_id,
            away_team_id=target.away_team_id,
            neutral_venue=target.neutral_venue,
            home_corners=target.home_corners,
            away_corners=target.away_corners,
            expected_home_corners=home_rate,
            expected_away_corners=away_rate,
            home_attack_mean=home.attack.mean,
            home_defense_mean=home.defense.mean,
            away_attack_mean=away.attack.mean,
            away_defense_mean=away.defense.mean,
            competition_log_corner_level=competition.level.mean,
            competition_home_advantage=(
                0.0 if target.neutral_venue else competition.home_advantage.mean
            ),
            home_history_matches=home.matches,
            away_history_matches=away.matches,
            competition_history_matches=competition.matches,
            home_cold_start=home.matches < self.config.cold_start_match_threshold,
            away_cold_start=away.matches < self.config.cold_start_match_threshold,
        )

    def _snapshot_inference(
        self,
        fixture: RegulationInferenceFixture,
        horizon: CornerHorizon,
        at: datetime,
    ) -> CornerInferenceFeatureRow:
        home_rate, away_rate = self._rates(fixture, at)
        home = self.teams[fixture.home_team_id]
        away = self.teams[fixture.away_team_id]
        competition = self.competitions[fixture.competition_id]
        return CornerInferenceFeatureRow(
            feature_version=self.config.feature_version,
            fixture_id=fixture.fixture_id,
            information_state=horizon.information_state,
            prediction_at=at,
            kickoff=fixture.kickoff,
            competition_id=fixture.competition_id,
            season_id=fixture.season_id,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            neutral_venue=fixture.neutral_venue,
            expected_home_corners=home_rate,
            expected_away_corners=away_rate,
            home_history_matches=home.matches,
            away_history_matches=away.matches,
            competition_history_matches=competition.matches,
            home_cold_start=home.matches < self.config.cold_start_match_threshold,
            away_cold_start=away.matches < self.config.cold_start_match_threshold,
        )

    def _observe_batch(self, targets: list[CornerTarget], at: datetime) -> None:
        updates = []
        for target in sorted(targets, key=lambda row: row.fixture_id):
            home_rate, away_rate = self._rates(target, at)
            updates.append((target, home_rate, away_rate))
        gradients: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for target, home_rate, away_rate in updates:
            home_gradient = target.home_corners - home_rate
            away_gradient = target.away_corners - away_rate
            for key, gradient, information in (
                (("team_attack", target.home_team_id), home_gradient, home_rate),
                (("team_defense", target.away_team_id), home_gradient, home_rate),
                (("team_attack", target.away_team_id), away_gradient, away_rate),
                (("team_defense", target.home_team_id), away_gradient, away_rate),
                (("competition_level", target.competition_id), home_gradient + away_gradient, home_rate + away_rate),
            ):
                gradients[key][0] += gradient
                gradients[key][1] += information
            if not target.neutral_venue:
                gradients[("competition_home", target.competition_id)][0] += home_gradient
                gradients[("competition_home", target.competition_id)][1] += home_rate
        for (kind, identifier), (gradient, information) in sorted(gradients.items()):
            if kind == "team_attack":
                self.teams[identifier].attack.update(gradient, information)
            elif kind == "team_defense":
                self.teams[identifier].defense.update(gradient, information)
            elif kind == "competition_level":
                self.competitions[identifier].level.update(gradient, information)
            else:
                self.competitions[identifier].home_advantage.update(gradient, information)
        for target, _home_rate, _away_rate in updates:
            self.teams[target.home_team_id].matches += 1
            self.teams[target.away_team_id].matches += 1
            self.competitions[target.competition_id].matches += 1


def _validate_targets(targets: list[CornerTarget]) -> None:
    identifiers = [row.fixture_id for row in targets]
    if len(identifiers) != len(set(identifiers)):
        raise CornerFeatureError("Corner targets must be unique by fixture")
    for row in targets:
        if row.kickoff.tzinfo is None:
            raise CornerFeatureError("Corner kickoff must be timezone-aware")
        if row.home_team_id == row.away_team_id:
            raise CornerFeatureError("Corner fixture teams must be distinct")
        if min(row.home_corners, row.away_corners) < 0:
            raise CornerFeatureError("Corner counts must be nonnegative")


def _team_schedules(targets: list[CornerTarget]) -> dict[str, list[datetime]]:
    schedules: dict[str, list[datetime]] = defaultdict(list)
    for row in targets:
        schedules[row.home_team_id].append(row.kickoff)
        schedules[row.away_team_id].append(row.kickoff)
    for values in schedules.values():
        values.sort()
    return schedules


def _intervening(
    target: CornerTarget,
    prediction_at: datetime,
    schedules: dict[str, list[datetime]],
) -> bool:
    for team_id in (target.home_team_id, target.away_team_id):
        values = schedules[team_id]
        index = bisect_left(values, prediction_at)
        while index < len(values) and values[index] < target.kickoff:
            if values[index] != target.kickoff:
                return True
            index += 1
    return False


def _string(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise CornerFeatureError(f"{key} must be a non-empty string")
    return value


def _positive_float(raw: dict, key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CornerFeatureError(f"{key} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise CornerFeatureError(f"{key} must be positive and finite")
    return value


def _positive_int(raw: dict, key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CornerFeatureError(f"{key} must be a positive integer")
    return value


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def corner_feature_rows_sha256(rows: list[CornerFeatureRow]) -> str:
    digest = hashlib.sha256()
    for row in sorted(
        rows, key=lambda item: (item.kickoff, item.fixture_id, item.information_state)
    ):
        value = asdict(row)
        value["prediction_at"] = row.prediction_at.isoformat()
        value["kickoff"] = row.kickoff.isoformat()
        digest.update(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()
