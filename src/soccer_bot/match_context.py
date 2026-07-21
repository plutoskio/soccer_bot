from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from soccer_bot.datasets.targets import RegulationScoreTarget


@dataclass(frozen=True)
class FixtureDisplayMetadata:
    fixture_id: str
    competition_name: str
    home_team_name: str
    away_team_name: str


def load_fixture_display_metadata(connection) -> dict[str, FixtureDisplayMetadata]:
    rows = connection.execute(
        """
        SELECT
            f.fixture_id,
            coalesce(c.name, 'Unknown competition'),
            home.name,
            away.name
        FROM fixture f
        LEFT JOIN competition c USING (competition_id)
        JOIN team home ON home.team_id = f.home_team_id
        JOIN team away ON away.team_id = f.away_team_id
        """
    ).fetchall()
    return {
        row[0]: FixtureDisplayMetadata(
            fixture_id=row[0],
            competition_name=row[1],
            home_team_name=row[2],
            away_team_name=row[3],
        )
        for row in rows
    }


def build_match_contexts(
    historical_targets: Sequence[RegulationScoreTarget],
    source_rows: Sequence[Mapping[str, object]],
    metadata: Mapping[str, FixtureDisplayMetadata],
    *,
    result_availability_delay: timedelta,
) -> dict[tuple[str, str], dict]:
    """Build consumer context using only results available by each forecast cutoff."""

    if result_availability_delay <= timedelta(0):
        raise ValueError("result_availability_delay must be positive")
    by_team: dict[str, list[RegulationScoreTarget]] = defaultdict(list)
    for target in historical_targets:
        if target.fixture_id not in metadata:
            raise ValueError(f"Missing display metadata for fixture {target.fixture_id}")
        by_team[target.home_team_id].append(target)
        by_team[target.away_team_id].append(target)
    for rows in by_team.values():
        rows.sort(key=lambda item: (item.kickoff, item.fixture_id), reverse=True)

    contexts = {}
    for source in source_rows:
        fixture_id = _required_string(source, "fixture_id")
        information_state = _required_string(source, "information_state")
        prediction_at = _timestamp(source.get("prediction_at"), "prediction_at")
        target_kickoff = _timestamp(source.get("kickoff"), "kickoff")
        home_team_id = _required_string(source, "home_team_id")
        away_team_id = _required_string(source, "away_team_id")
        contexts[(fixture_id, information_state)] = {
            "cutoff_at": prediction_at.isoformat(),
            "home": _team_context(
                team_id=home_team_id,
                targets=by_team.get(home_team_id, []),
                metadata=metadata,
                prediction_at=prediction_at,
                target_kickoff=target_kickoff,
                result_availability_delay=result_availability_delay,
            ),
            "away": _team_context(
                team_id=away_team_id,
                targets=by_team.get(away_team_id, []),
                metadata=metadata,
                prediction_at=prediction_at,
                target_kickoff=target_kickoff,
                result_availability_delay=result_availability_delay,
            ),
        }
    return contexts


def _team_context(
    *,
    team_id: str,
    targets: Sequence[RegulationScoreTarget],
    metadata: Mapping[str, FixtureDisplayMetadata],
    prediction_at: datetime,
    target_kickoff: datetime,
    result_availability_delay: timedelta,
) -> dict:
    available = [
        target
        for target in targets
        if _available_at(target, result_availability_delay) < prediction_at
    ]
    records = [
        _team_match_record(
            team_id,
            target,
            metadata[target.fixture_id],
            _available_at(target, result_availability_delay),
        )
        for target in available[:10]
    ]
    return {
        "team_id": team_id,
        "rest_days": _rest_days(available, target_kickoff),
        "matches_last_7d": _recent_count(available, target_kickoff, 7),
        "matches_last_14d": _recent_count(available, target_kickoff, 14),
        "matches_last_30d": _recent_count(available, target_kickoff, 30),
        "recent_matches": records[:5],
        "trends": {
            "last_5": _trend(records[:5]),
            "last_10": _trend(records[:10]),
        },
    }


def _team_match_record(
    team_id: str,
    target: RegulationScoreTarget,
    display: FixtureDisplayMetadata,
    available_at: datetime,
) -> dict:
    was_home = team_id == target.home_team_id
    team_score = target.home_goals if was_home else target.away_goals
    opponent_score = target.away_goals if was_home else target.home_goals
    if team_score > opponent_score:
        outcome = "win"
    elif team_score == opponent_score:
        outcome = "draw"
    else:
        outcome = "loss"
    return {
        "fixture_id": target.fixture_id,
        "kickoff": target.kickoff.isoformat(),
        "available_at": available_at.isoformat(),
        "competition_name": display.competition_name,
        "opponent_name": (
            display.away_team_name if was_home else display.home_team_name
        ),
        "venue": "home" if was_home else "away",
        "neutral_venue": target.neutral_venue,
        "team_score": team_score,
        "opponent_score": opponent_score,
        "outcome": outcome,
    }


def _trend(matches: Sequence[dict]) -> dict:
    count = len(matches)
    wins = sum(item["outcome"] == "win" for item in matches)
    draws = sum(item["outcome"] == "draw" for item in matches)
    losses = sum(item["outcome"] == "loss" for item in matches)
    goals_for = sum(item["team_score"] for item in matches)
    goals_against = sum(item["opponent_score"] for item in matches)
    return {
        "sample_size": count,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for_per_match": None if not count else goals_for / count,
        "goals_against_per_match": None if not count else goals_against / count,
        "clean_sheet_rate": None
        if not count
        else sum(item["opponent_score"] == 0 for item in matches) / count,
        "both_teams_scored_rate": None
        if not count
        else sum(
            item["team_score"] > 0 and item["opponent_score"] > 0
            for item in matches
        )
        / count,
    }


def _available_at(
    target: RegulationScoreTarget, delay: timedelta
) -> datetime:
    return target.result_available_at or target.kickoff + delay


def _rest_days(
    targets: Sequence[RegulationScoreTarget], target_kickoff: datetime
) -> float | None:
    if not targets:
        return None
    return (target_kickoff - max(item.kickoff for item in targets)).total_seconds() / 86400


def _recent_count(
    targets: Sequence[RegulationScoreTarget], target_kickoff: datetime, days: int
) -> int:
    start = target_kickoff - timedelta(days=days)
    return sum(start <= item.kickoff < target_kickoff for item in targets)


def _required_string(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{field} must be a non-empty string")
    return item


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed
