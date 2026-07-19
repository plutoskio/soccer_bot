from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from soccer_bot.datasets.targets import (
    RegulationTargetExclusion,
    build_regulation_score_targets,
)


SCORED_GOAL_DETAILS = {"Normal Goal", "Penalty", "Own Goal"}


@dataclass(frozen=True)
class FirstTeamScoreTarget:
    fixture_id: str
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    kickoff: datetime
    prediction_at: datetime
    outcome: str
    first_goal_minute: int | None
    first_goal_added_minute: int | None
    first_goal_team_id: str | None
    first_goal_player_id: str | None
    first_goal_detail: str | None
    first_player_target_safe: bool
    complete_event_source: str
    target_available_at: datetime | None = None
    source_max_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class TimingTargetIssue:
    fixture_id: str
    reason: str


@dataclass(frozen=True)
class FirstTeamScoreTargetBuild:
    targets: tuple[FirstTeamScoreTarget, ...]
    issues: tuple[TimingTargetIssue, ...]

    @property
    def issue_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(issue.reason for issue in self.issues).items()))


def build_first_team_score_targets(
    connection,
    *,
    prediction_offset: timedelta = timedelta(hours=24),
    kickoff_start: datetime | None = None,
    kickoff_end: datetime | None = None,
    reviewed_result_exclusions: Mapping[
        str, RegulationTargetExclusion
    ] | None = None,
    strict_retrieval_from: datetime | None = None,
    target_availability_delay: timedelta = timedelta(minutes=150),
) -> FirstTeamScoreTargetBuild:
    """Build first-team/no-goal targets only from score-complete event artifacts."""

    score_targets = build_regulation_score_targets(
        connection,
        prediction_offset=prediction_offset,
        kickoff_start=kickoff_start,
        kickoff_end=kickoff_end,
        reviewed_exclusions=reviewed_result_exclusions,
        strict_retrieval_from=strict_retrieval_from,
        result_availability_delay=target_availability_delay,
    )
    score_by_fixture = {target.fixture_id: target for target in score_targets}
    event_rows = connection.execute(
        """
        SELECT
            match_event_id,
            fixture_id,
            team_id,
            player_id,
            source_code,
            source_event_id,
            coalesce(raw_artifact_id, ''),
            event_type,
            event_detail,
            minute,
            added_minute,
            event_data,
            retrieved_at
        FROM match_event
        WHERE source_code='api_football'
        ORDER BY fixture_id, coalesce(raw_artifact_id, ''), retrieved_at,
                 minute, added_minute, match_event_id
        """
    ).fetchall()
    by_fixture_artifact: dict[str, dict[str, list[tuple]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in event_rows:
        fixture_id = str(row[1])
        if fixture_id in score_by_fixture:
            by_fixture_artifact[fixture_id][str(row[6])].append(row)

    targets = []
    issues = []
    for score in score_targets:
        artifacts = by_fixture_artifact.get(score.fixture_id, {})
        if not artifacts:
            issues.append(TimingTargetIssue(score.fixture_id, "no_event_artifact"))
            continue
        valid = []
        for rows in artifacts.values():
            candidate = _artifact_target(rows, score)
            if candidate is not None:
                valid.append(candidate)
        if not valid:
            issues.append(
                TimingTargetIssue(score.fixture_id, "no_score_complete_event_artifact")
            )
            continue
        signatures = {candidate[0] for candidate in valid}
        if len(signatures) != 1:
            issues.append(
                TimingTargetIssue(score.fixture_id, "conflicting_first_goal_artifacts")
            )
            continue
        signature = next(iter(signatures))
        first_event = valid[0][1]
        event_retrieved_at = max(candidate[2] for candidate in valid)
        outcome, minute, added_minute = signature
        strict_forward_fixture = (
            strict_retrieval_from is not None
            and score.kickoff >= strict_retrieval_from
        )
        player_id = None
        detail = None
        player_safe = False
        team_id = None
        if first_event is not None:
            team_id = str(first_event[2])
            detail = str(first_event[8])
            tied = valid[0][3]
            player_safe = len(tied) == 1 and detail != "Own Goal"
            if player_safe and first_event[3] is not None:
                player_id = str(first_event[3])
            else:
                player_safe = False
        target_available_at = None
        source_max_retrieved_at = None
        if strict_forward_fixture:
            source_max_retrieved_at = max(
                timestamp
                for timestamp in (score.source_max_retrieved_at, event_retrieved_at)
                if timestamp is not None
            )
            target_available_at = max(
                score.kickoff + target_availability_delay,
                source_max_retrieved_at,
            )
        targets.append(
            FirstTeamScoreTarget(
                fixture_id=score.fixture_id,
                competition_id=score.competition_id,
                season_id=score.season_id,
                home_team_id=score.home_team_id,
                away_team_id=score.away_team_id,
                neutral_venue=score.neutral_venue,
                kickoff=score.kickoff,
                prediction_at=score.prediction_at,
                outcome=outcome,
                first_goal_minute=minute,
                first_goal_added_minute=added_minute,
                first_goal_team_id=team_id,
                first_goal_player_id=player_id,
                first_goal_detail=detail,
                first_player_target_safe=player_safe,
                complete_event_source="api_football",
                target_available_at=target_available_at,
                source_max_retrieved_at=source_max_retrieved_at,
            )
        )
    return FirstTeamScoreTargetBuild(tuple(targets), tuple(issues))


def _artifact_target(rows: list[tuple], score):
    goals = []
    for row in rows:
        if str(row[7]).lower() != "goal" or row[8] not in SCORED_GOAL_DETAILS:
            continue
        minute = row[9]
        if isinstance(minute, bool) or not isinstance(minute, int) or minute < 0:
            return None
        if minute > 90:
            continue
        team_id = None if row[2] is None else str(row[2])
        if team_id not in {score.home_team_id, score.away_team_id}:
            return None
        goals.append((minute, _added_minute(row[10], row[11]), row))

    home_goals = sum(str(item[2][2]) == score.home_team_id for item in goals)
    away_goals = sum(str(item[2][2]) == score.away_team_id for item in goals)
    if (home_goals, away_goals) != (score.home_goals, score.away_goals):
        return None
    retrieved_at = max(row[12] for row in rows)
    if not goals:
        return ("no_goal", None, None), None, retrieved_at, ()
    goals.sort(key=lambda item: (item[0], item[1], str(item[2][0])))
    first_minute, first_added, first_row = goals[0]
    tied = tuple(
        item[2]
        for item in goals
        if (item[0], item[1]) == (first_minute, first_added)
    )
    tied_teams = {str(row[2]) for row in tied}
    if len(tied_teams) != 1:
        return None
    outcome = (
        "home_first" if next(iter(tied_teams)) == score.home_team_id else "away_first"
    )
    return (outcome, first_minute, first_added), first_row, retrieved_at, tied


def _added_minute(column_value: object, event_data: object) -> int:
    if isinstance(column_value, int) and not isinstance(column_value, bool):
        return max(0, column_value)
    if isinstance(event_data, str):
        try:
            value = json.loads(event_data)
        except json.JSONDecodeError:
            return 0
    elif isinstance(event_data, dict):
        value = event_data
    else:
        return 0
    extra = value.get("time", {}).get("extra")
    return extra if isinstance(extra, int) and not isinstance(extra, bool) and extra >= 0 else 0
