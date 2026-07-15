from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from soccer_bot.datasets.features import (
    RegulationInferenceFixture,
    TeamStateFeatureConfig,
)


class UpcomingFixtureError(RuntimeError):
    """Raised when an upcoming-fixture inference request is unsafe."""


@dataclass(frozen=True)
class UpcomingFixtureMetadata:
    fixture_id: str
    competition_name: str
    home_team_name: str
    away_team_name: str


def load_upcoming_inference_fixtures(
    connection,
    *,
    as_of: datetime,
    lookahead_days: int,
    feature_config: TeamStateFeatureConfig,
) -> tuple[list[RegulationInferenceFixture], dict[str, UpcomingFixtureMetadata], dict]:
    if as_of.tzinfo is None:
        raise UpcomingFixtureError("as_of must be timezone-aware")
    if lookahead_days <= 0:
        raise UpcomingFixtureError("lookahead_days must be positive")
    fixture_rows = connection.execute(
        """
        SELECT
            f.fixture_id,
            f.competition_id,
            f.season_id,
            f.home_team_id,
            f.away_team_id,
            coalesce(f.neutral_venue, false),
            f.scheduled_kickoff,
            c.name,
            home.name,
            away.name
        FROM fixture f
        JOIN competition c USING (competition_id)
        JOIN team home ON home.team_id=f.home_team_id
        JOIN team away ON away.team_id=f.away_team_id
        WHERE f.status='scheduled'
          AND f.scheduled_kickoff > ?
          AND f.scheduled_kickoff <= ?
        ORDER BY f.scheduled_kickoff, f.fixture_id
        """,
        [as_of, as_of + timedelta(days=lookahead_days)],
    ).fetchall()
    fixture_ids = [str(row[0]) for row in fixture_rows]
    schedules: dict[str, list[tuple]] = {fixture_id: [] for fixture_id in fixture_ids}
    if fixture_ids:
        placeholders = ",".join("?" for _ in fixture_ids)
        schedule_rows = connection.execute(
            f"""
            SELECT fixture_id, retrieved_at, scheduled_kickoff,
                   canonical_status, schedule_observation_id
            FROM fixture_schedule_observation
            WHERE fixture_id IN ({placeholders})
            ORDER BY fixture_id, retrieved_at, schedule_observation_id
            """,
            fixture_ids,
        ).fetchall()
        for row in schedule_rows:
            schedules[str(row[0])].append(row[1:])

    fixtures = []
    metadata = {}
    horizon_audit = []
    for row in fixture_rows:
        fixture_id = str(row[0])
        kickoff = row[6]
        allowed = []
        for horizon in feature_config.horizons:
            prediction_at = kickoff - timedelta(
                minutes=horizon.minutes_before_kickoff
            )
            reason = None
            if prediction_at > as_of:
                reason = "horizon_not_due"
            else:
                known = [
                    item
                    for item in schedules[fixture_id]
                    if item[0] <= prediction_at
                ]
                if not known:
                    reason = "no_schedule_observation_by_prediction_at"
                else:
                    latest = known[-1]
                    if latest[1] != kickoff:
                        reason = "observed_kickoff_differs_at_prediction_at"
                    elif latest[2] != "scheduled":
                        reason = "fixture_not_scheduled_at_prediction_at"
                    else:
                        allowed.append(horizon.information_state)
            horizon_audit.append(
                {
                    "fixture_id": fixture_id,
                    "information_state": horizon.information_state,
                    "prediction_at": prediction_at.isoformat(),
                    "eligible_before_clean_horizon_check": reason is None,
                    "reason": reason,
                }
            )
        fixtures.append(
            RegulationInferenceFixture(
                fixture_id=fixture_id,
                competition_id=str(row[1]),
                season_id=None if row[2] is None else str(row[2]),
                home_team_id=str(row[3]),
                away_team_id=str(row[4]),
                neutral_venue=bool(row[5]),
                kickoff=kickoff,
                allowed_information_states=tuple(sorted(allowed)),
            )
        )
        metadata[fixture_id] = UpcomingFixtureMetadata(
            fixture_id=fixture_id,
            competition_name=str(row[7]),
            home_team_name=str(row[8]),
            away_team_name=str(row[9]),
        )
    return fixtures, metadata, {
        "as_of": as_of.isoformat(),
        "lookahead_days": lookahead_days,
        "scheduled_fixtures": len(fixtures),
        "horizons": horizon_audit,
    }
