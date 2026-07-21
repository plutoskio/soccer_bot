from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from soccer_bot.datasets.targets import RegulationScoreTarget
from soccer_bot.match_context import (
    FixtureDisplayMetadata,
    build_match_contexts,
)


class MatchContextTests(unittest.TestCase):
    def test_context_uses_only_results_available_by_forecast_cutoff(self) -> None:
        cutoff = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        visible = target(
            "visible",
            kickoff=cutoff - timedelta(days=4),
            home="home",
            away="opponent-a",
            home_goals=2,
            away_goals=1,
        )
        late = target(
            "late",
            kickoff=cutoff - timedelta(days=2),
            home="opponent-b",
            away="home",
            home_goals=0,
            away_goals=3,
            result_available_at=cutoff + timedelta(minutes=1),
        )
        exact_cutoff = target(
            "exact-cutoff",
            kickoff=cutoff - timedelta(days=2, hours=1),
            home="home",
            away="opponent-d",
            home_goals=1,
            away_goals=0,
            result_available_at=cutoff,
        )
        away_match = target(
            "away-visible",
            kickoff=cutoff - timedelta(days=3),
            home="opponent-c",
            away="away",
            home_goals=1,
            away_goals=1,
        )
        metadata = {
            "visible": FixtureDisplayMetadata(
                "visible", "League", "Home", "Opponent A"
            ),
            "late": FixtureDisplayMetadata(
                "late", "Cup", "Opponent B", "Home"
            ),
            "exact-cutoff": FixtureDisplayMetadata(
                "exact-cutoff", "Cup", "Home", "Opponent D"
            ),
            "away-visible": FixtureDisplayMetadata(
                "away-visible", "League", "Opponent C", "Away"
            ),
        }
        rows = [
            {
                "fixture_id": "upcoming",
                "information_state": "pre_lineup_24h_v1",
                "prediction_at": cutoff.isoformat(),
                "kickoff": (cutoff + timedelta(days=1)).isoformat(),
                "home_team_id": "home",
                "away_team_id": "away",
            }
        ]

        context = build_match_contexts(
            [visible, late, exact_cutoff, away_match],
            rows,
            metadata,
            result_availability_delay=timedelta(minutes=150),
        )[("upcoming", "pre_lineup_24h_v1")]

        self.assertEqual(
            [item["fixture_id"] for item in context["home"]["recent_matches"]],
            ["visible"],
        )
        self.assertEqual(context["home"]["trends"]["last_5"]["wins"], 1)
        self.assertEqual(
            context["home"]["trends"]["last_5"]["goals_for_per_match"],
            2.0,
        )
        self.assertEqual(context["away"]["recent_matches"][0]["outcome"], "draw")
        self.assertEqual(context["home"]["matches_last_14d"], 1)
        self.assertEqual(context["home"]["rest_days"], 5.0)


def target(
    fixture_id: str,
    *,
    kickoff: datetime,
    home: str,
    away: str,
    home_goals: int,
    away_goals: int,
    result_available_at: datetime | None = None,
) -> RegulationScoreTarget:
    result = "home_win" if home_goals > away_goals else "draw" if home_goals == away_goals else "away_win"
    return RegulationScoreTarget(
        fixture_id=fixture_id,
        competition_id="competition",
        season_id="season",
        home_team_id=home,
        away_team_id=away,
        neutral_venue=False,
        kickoff=kickoff,
        prediction_at=kickoff - timedelta(hours=24),
        home_goals=home_goals,
        away_goals=away_goals,
        result=result,
        total_goals=home_goals + away_goals,
        goal_difference=home_goals - away_goals,
        both_teams_to_score=home_goals > 0 and away_goals > 0,
        agreeing_source_codes=("test",),
        result_available_at=result_available_at,
    )


if __name__ == "__main__":
    unittest.main()
