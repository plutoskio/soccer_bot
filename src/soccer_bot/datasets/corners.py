from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


class CornerTargetError(RuntimeError):
    """Raised when team-eligible facts cannot produce safe corner targets."""


@dataclass(frozen=True)
class CornerTarget:
    fixture_id: str
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    neutral_venue: bool
    kickoff: datetime
    prediction_at: datetime
    home_corners: int
    away_corners: int
    total_corners: int
    corner_difference: int
    agreeing_source_codes: tuple[str, ...]
    target_available_at: datetime | None = None
    source_max_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class CornerTargetConflict:
    fixture_id: str
    observed_pairs: tuple[tuple[int, int], ...]
    source_codes: tuple[str, ...]
    reason: str = "conflicting_regulation_corner_targets"


@dataclass(frozen=True)
class CornerTargetBuild:
    targets: tuple[CornerTarget, ...]
    conflicts: tuple[CornerTargetConflict, ...]


def build_corner_targets(
    connection,
    *,
    prediction_offset: timedelta = timedelta(hours=24),
    kickoff_start: datetime | None = None,
    kickoff_end: datetime | None = None,
    strict_retrieval_from: datetime | None = None,
    target_availability_delay: timedelta = timedelta(minutes=150),
) -> CornerTargetBuild:
    """Build one regulation corner target per safe team-eligible fixture.

    Complete provider artifacts must agree on both team counts. A disagreement
    is excluded and returned in the audit rather than silently choosing one
    provider. Duplicate identical observations inside an artifact are harmless.
    """

    if prediction_offset < timedelta(0):
        raise ValueError("prediction_offset must be nonnegative")
    if target_availability_delay <= timedelta(0):
        raise ValueError("target_availability_delay must be positive")
    for value, name in (
        (kickoff_start, "kickoff_start"),
        (kickoff_end, "kickoff_end"),
        (strict_retrieval_from, "strict_retrieval_from"),
    ):
        if value is not None and value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")

    conditions = [
        "e.eligible_team_models",
        "f.scheduled_kickoff IS NOT NULL",
        "s.period = 'regulation'",
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
            coalesce(f.neutral_venue, false),
            f.scheduled_kickoff,
            s.source_code,
            coalesce(s.raw_artifact_id, ''),
            s.team_id,
            s.shots,
            s.shots_on_target,
            s.corners,
            s.possession_pct,
            s.passes,
            s.accurate_passes,
            s.retrieved_at
        FROM fixture_model_eligibility e
        JOIN fixture f USING (fixture_id)
        JOIN team_match_stat_observation s USING (fixture_id)
        WHERE {' AND '.join(conditions)}
        ORDER BY f.scheduled_kickoff, f.fixture_id, s.source_code,
                 coalesce(s.raw_artifact_id, ''), s.team_id, s.retrieved_at,
                 s.observation_id
        """,
        parameters,
    ).fetchall()

    grouped: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        grouped[str(row[0])].append(row)

    targets = []
    conflicts = []
    for fixture_id, fixture_rows in grouped.items():
        artifacts: dict[tuple[str, str], list[tuple]] = defaultdict(list)
        for row in fixture_rows:
            artifacts[(str(row[7]), str(row[8]))].append(row)
        complete = []
        for (source_code, _artifact_key), artifact_rows in artifacts.items():
            pair = _valid_artifact_pair(artifact_rows)
            if pair is None:
                continue
            complete.append(
                (
                    pair,
                    source_code,
                    max(row[16] for row in artifact_rows),
                )
            )
        if not complete:
            raise CornerTargetError(
                f"Team-eligible fixture has no complete corner artifact: {fixture_id}"
            )
        observed_pairs = {item[0] for item in complete}
        source_codes = tuple(sorted({item[1] for item in complete}))
        if len(observed_pairs) != 1:
            conflicts.append(
                CornerTargetConflict(
                    fixture_id=fixture_id,
                    observed_pairs=tuple(sorted(observed_pairs)),
                    source_codes=source_codes,
                )
            )
            continue

        first = fixture_rows[0]
        kickoff = first[6]
        home_corners, away_corners = next(iter(observed_pairs))
        retrieved_at = max(item[2] for item in complete)
        strict_forward_fixture = (
            strict_retrieval_from is not None
            and kickoff >= strict_retrieval_from
        )
        targets.append(
            CornerTarget(
                fixture_id=fixture_id,
                competition_id=str(first[1]),
                season_id=None if first[2] is None else str(first[2]),
                home_team_id=str(first[3]),
                away_team_id=str(first[4]),
                neutral_venue=bool(first[5]),
                kickoff=kickoff,
                prediction_at=kickoff - prediction_offset,
                home_corners=home_corners,
                away_corners=away_corners,
                total_corners=home_corners + away_corners,
                corner_difference=home_corners - away_corners,
                agreeing_source_codes=source_codes,
                target_available_at=(
                    max(kickoff + target_availability_delay, retrieved_at)
                    if strict_forward_fixture
                    else None
                ),
                source_max_retrieved_at=(
                    retrieved_at if strict_forward_fixture else None
                ),
            )
        )
    return CornerTargetBuild(
        targets=tuple(targets),
        conflicts=tuple(conflicts),
    )


def _valid_artifact_pair(rows: list[tuple]) -> tuple[int, int] | None:
    first = rows[0]
    home_team_id = str(first[3])
    away_team_id = str(first[4])
    by_team: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        team_id = str(row[9])
        if team_id not in {home_team_id, away_team_id}:
            return None
        if not _valid_core_team_row(row):
            return None
        by_team[team_id].append(row)
    if set(by_team) != {home_team_id, away_team_id}:
        return None
    values = {}
    for team_id, team_rows in by_team.items():
        corners = {row[12] for row in team_rows}
        if len(corners) != 1:
            return None
        values[team_id] = next(iter(corners))
    return int(values[home_team_id]), int(values[away_team_id])


def _valid_core_team_row(row: tuple) -> bool:
    shots = row[10]
    shots_on_target = row[11]
    corners = row[12]
    possession = row[13]
    passes = row[14]
    accurate_passes = row[15]
    return (
        isinstance(shots, int)
        and shots >= 0
        and isinstance(shots_on_target, int)
        and 0 <= shots_on_target <= shots
        and isinstance(corners, int)
        and corners >= 0
        and (possession is None or 0 <= possession <= 100)
        and (passes is None or passes >= 0)
        and (accurate_passes is None or accurate_passes >= 0)
        and (
            passes is None
            or accurate_passes is None
            or accurate_passes <= passes
        )
    )
