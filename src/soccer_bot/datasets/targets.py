from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from soccer_bot.config import load_json


class TargetConstructionError(RuntimeError):
    """Raised when eligible canonical facts cannot produce one safe target."""


@dataclass(frozen=True)
class RegulationScoreTarget:
    fixture_id: str
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    kickoff: datetime
    prediction_at: datetime
    home_goals: int
    away_goals: int
    result: str
    total_goals: int
    goal_difference: int
    both_teams_to_score: bool
    agreeing_source_codes: tuple[str, ...]
    result_available_at: datetime | None = None
    source_max_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class RegulationTargetExclusion:
    fixture_id: str
    observed_scores: frozenset[tuple[int, int]]
    rule_code: str = "conflicting_final_regulation_score"


def build_regulation_score_targets(
    connection,
    *,
    prediction_offset: timedelta = timedelta(hours=24),
    kickoff_start: datetime | None = None,
    kickoff_end: datetime | None = None,
    reviewed_exclusions: Mapping[str, RegulationTargetExclusion] | None = None,
    strict_retrieval_from: datetime | None = None,
    result_availability_delay: timedelta = timedelta(minutes=150),
) -> list[RegulationScoreTarget]:
    """Build deterministic regulation targets from result-eligible fixtures.

    Multiple providers may support one target only when their valid final
    regulation scores agree. A reviewed conflict is excluded only while its
    exact configured evidence still matches; any new or changed conflict fails
    the build rather than selecting a provider silently.
    """

    if prediction_offset < timedelta(0):
        raise ValueError("prediction_offset must be nonnegative")
    if result_availability_delay <= timedelta(0):
        raise ValueError("result_availability_delay must be positive")
    if strict_retrieval_from is not None and strict_retrieval_from.tzinfo is None:
        raise ValueError("strict_retrieval_from must be timezone-aware")

    conditions = [
        "e.eligible_result_models",
        "f.scheduled_kickoff IS NOT NULL",
        "r.result_status = 'final'",
        "r.home_score_regulation IS NOT NULL",
        "r.away_score_regulation IS NOT NULL",
        "r.home_score_regulation >= 0",
        "r.away_score_regulation >= 0",
    ]
    parameters = []
    if kickoff_start is not None:
        conditions.append("f.scheduled_kickoff >= ?")
        parameters.append(kickoff_start)
    if kickoff_end is not None:
        conditions.append("f.scheduled_kickoff < ?")
        parameters.append(kickoff_end)

    rows = connection.execute(
        f"""
        SELECT
            f.fixture_id,
            f.competition_id,
            f.season_id,
            f.home_team_id,
            f.away_team_id,
            f.scheduled_kickoff,
            coalesce(f.neutral_venue, false),
            r.home_score_regulation,
            r.away_score_regulation,
            r.source_code,
            r.retrieved_at
        FROM fixture_model_eligibility e
        JOIN fixture f USING (fixture_id)
        JOIN fixture_result_observation r USING (fixture_id)
        WHERE {' AND '.join(conditions)}
        ORDER BY f.scheduled_kickoff, f.fixture_id, r.source_code,
                 r.retrieved_at, r.observation_id
        """,
        parameters,
    ).fetchall()

    exclusions = reviewed_exclusions or {}
    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)

    targets = []
    for fixture_id, fixture_rows in grouped.items():
        score_pairs = {(row[7], row[8]) for row in fixture_rows}
        if fixture_id in exclusions:
            expected = exclusions[fixture_id].observed_scores
            if score_pairs != expected or len(score_pairs) < 2:
                raise TargetConstructionError(
                    f"Reviewed exclusion no longer matches fixture {fixture_id}: "
                    f"expected={sorted(expected)}, actual={sorted(score_pairs)}"
                )
            continue
        if len(score_pairs) != 1:
            ordered_scores = sorted(score_pairs)
            raise TargetConstructionError(
                f"Conflicting final regulation scores for fixture {fixture_id}: "
                f"{ordered_scores}"
            )
        first = fixture_rows[0]
        home_goals, away_goals = next(iter(score_pairs))
        if home_goals > away_goals:
            result = "home_win"
        elif home_goals == away_goals:
            result = "draw"
        else:
            result = "away_win"
        kickoff = first[5]
        source_retrieved_at = max(row[10] for row in fixture_rows)
        strict_forward_fixture = (
            strict_retrieval_from is not None
            and kickoff >= strict_retrieval_from
        )
        result_available_at = (
            max(kickoff + result_availability_delay, source_retrieved_at)
            if strict_forward_fixture
            else None
        )
        targets.append(
            RegulationScoreTarget(
                fixture_id=fixture_id,
                competition_id=first[1],
                season_id=first[2],
                home_team_id=first[3],
                away_team_id=first[4],
                neutral_venue=first[6],
                kickoff=kickoff,
                prediction_at=kickoff - prediction_offset,
                home_goals=home_goals,
                away_goals=away_goals,
                result=result,
                total_goals=home_goals + away_goals,
                goal_difference=home_goals - away_goals,
                both_teams_to_score=home_goals > 0 and away_goals > 0,
                agreeing_source_codes=tuple(
                    sorted({row[9] for row in fixture_rows})
                ),
                result_available_at=result_available_at,
                source_max_retrieved_at=(
                    source_retrieved_at if strict_forward_fixture else None
                ),
            )
        )
    return targets


def load_regulation_target_exclusions(
    path: Path,
) -> dict[str, RegulationTargetExclusion]:
    specification = load_json(path)
    if specification.get("policy") != (
        "exclude_reviewed_conflicts_and_fail_on_any_unreviewed_conflict"
    ):
        raise TargetConstructionError("Unknown regulation target exclusion policy")
    fixtures = specification.get("fixtures")
    if not isinstance(fixtures, list):
        raise TargetConstructionError("Target exclusions fixtures must be a list")
    exclusions = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise TargetConstructionError("Each target exclusion must be an object")
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise TargetConstructionError("Target exclusion requires fixture_id")
        if fixture.get("rule_code") != "conflicting_final_regulation_score":
            raise TargetConstructionError(
                f"Unexpected exclusion rule for fixture {fixture_id}"
            )
        if fixture.get("decision") != "exclude_pending_canonical_resolution":
            raise TargetConstructionError(
                f"Unexpected exclusion decision for fixture {fixture_id}"
            )
        if fixture_id in exclusions:
            raise TargetConstructionError("Duplicate fixture in target exclusions")
        observed_scores = fixture.get("observed_scores")
        if not isinstance(observed_scores, list) or len(observed_scores) < 2:
            raise TargetConstructionError(
                f"Target exclusion {fixture_id} requires conflicting observed_scores"
            )
        score_pairs = set()
        for score in observed_scores:
            if not isinstance(score, dict):
                raise TargetConstructionError(
                    f"Invalid observed score for fixture {fixture_id}"
                )
            home_goals = score.get("home_goals")
            away_goals = score.get("away_goals")
            if (
                isinstance(home_goals, bool)
                or isinstance(away_goals, bool)
                or not isinstance(home_goals, int)
                or not isinstance(away_goals, int)
                or home_goals < 0
                or away_goals < 0
            ):
                raise TargetConstructionError(
                    f"Invalid observed score for fixture {fixture_id}"
                )
            score_pairs.add((home_goals, away_goals))
        if len(score_pairs) < 2:
            raise TargetConstructionError(
                f"Target exclusion {fixture_id} does not contain a conflict"
            )
        exclusions[fixture_id] = RegulationTargetExclusion(
            fixture_id=fixture_id,
            observed_scores=frozenset(score_pairs),
        )
    return exclusions
