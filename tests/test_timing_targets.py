from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from soccer_bot.database import Warehouse
from soccer_bot.datasets.timing import build_first_team_score_targets


ROOT = Path(__file__).resolve().parents[1]


class FirstTeamScoreTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.warehouse = Warehouse(
            Path(self.directory.name) / "test.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.connection = self.warehouse.connection
        self.kickoff = datetime(2025, 5, 10, 14, 0, tzinfo=timezone.utc)
        self._insert_dimensions()

    def tearDown(self) -> None:
        self.warehouse.close()
        self.directory.cleanup()

    def _insert_dimensions(self) -> None:
        self.connection.execute(
            """INSERT INTO competition (
                   competition_id, name, country_code, competition_type
               ) VALUES ('competition', 'Test League', 'XX', 'domestic_league')"""
        )
        self.connection.execute(
            """INSERT INTO season (season_id, competition_id, name)
               VALUES ('season', 'competition', '2025')"""
        )
        self.connection.execute(
            """INSERT INTO team (team_id, name, normalized_name, team_type)
               VALUES ('home', 'Home', 'home', 'club'),
                      ('away', 'Away', 'away', 'club')"""
        )

    def _insert_fixture(self, home_goals: int, away_goals: int) -> None:
        self.connection.execute(
            """INSERT INTO fixture (
                   fixture_id, competition_id, season_id, home_team_id,
                   away_team_id, scheduled_kickoff, status
               ) VALUES ('fixture', 'competition', 'season', 'home', 'away',
                         ?, 'completed')""",
            [self.kickoff],
        )
        self.connection.execute(
            """INSERT INTO fixture_result_observation (
                   observation_id, fixture_id, source_code, retrieved_at,
                   home_score_regulation, away_score_regulation, result_status
               ) VALUES ('result', 'fixture', 'api_football', ?, ?, ?, 'final')""",
            [self.kickoff + timedelta(hours=3), home_goals, away_goals],
        )

    def _insert_event(
        self,
        event_id: str,
        *,
        team: str | None,
        minute: int,
        detail: str = "Normal Goal",
        player: str | None = "player",
        artifact: str = "artifact",
        event_type: str = "Goal",
        extra: int | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO match_event (
                   match_event_id, fixture_id, team_id, player_id, source_code,
                   source_event_id, raw_artifact_id, event_type, event_detail,
                   minute, event_data, retrieved_at
               ) VALUES (?, 'fixture', ?, ?, 'api_football', ?, ?, ?, ?, ?, ?, ?)""",
            [
                event_id,
                team,
                player,
                event_id,
                artifact,
                event_type,
                detail,
                minute,
                json.dumps({"time": {"elapsed": minute, "extra": extra}}),
                self.kickoff + timedelta(hours=3),
            ],
        )

    def test_builds_home_first_target_from_score_complete_artifact(self) -> None:
        self._insert_fixture(2, 1)
        self._insert_event("goal-1", team="home", minute=12, player="home-player")
        self._insert_event("goal-2", team="away", minute=50, player="away-player")
        self._insert_event("goal-3", team="home", minute=90, extra=2)

        result = build_first_team_score_targets(self.connection)

        self.assertEqual(result.issue_counts, {})
        target = result.targets[0]
        self.assertEqual(target.outcome, "home_first")
        self.assertEqual(target.first_goal_minute, 12)
        self.assertEqual(target.first_goal_player_id, "home-player")
        self.assertTrue(target.first_player_target_safe)

    def test_scoreless_match_requires_an_event_artifact(self) -> None:
        self._insert_fixture(0, 0)

        missing = build_first_team_score_targets(self.connection)
        self.assertEqual(missing.targets, ())
        self.assertEqual(missing.issue_counts, {"no_event_artifact": 1})

        self._insert_event(
            "card",
            team="home",
            minute=20,
            detail="Yellow Card",
            event_type="Card",
        )
        complete = build_first_team_score_targets(self.connection)
        self.assertEqual(complete.targets[0].outcome, "no_goal")

    def test_extra_time_and_shootout_events_do_not_enter_regulation(self) -> None:
        self._insert_fixture(1, 0)
        self._insert_event("regulation", team="home", minute=70)
        self._insert_event("extra-time", team="away", minute=110)
        self._insert_event("shootout", team="away", minute=120, detail="Penalty")

        target = build_first_team_score_targets(self.connection).targets[0]

        self.assertEqual(target.outcome, "home_first")
        self.assertEqual(target.first_goal_minute, 70)

    def test_mismatched_event_artifact_is_excluded(self) -> None:
        self._insert_fixture(2, 0)
        self._insert_event("only-goal", team="home", minute=10)

        result = build_first_team_score_targets(self.connection)

        self.assertEqual(result.targets, ())
        self.assertEqual(result.issue_counts, {"no_score_complete_event_artifact": 1})

    def test_own_goal_is_safe_for_team_but_not_player_target(self) -> None:
        self._insert_fixture(0, 1)
        self._insert_event(
            "own-goal",
            team="away",
            minute=8,
            detail="Own Goal",
            player="home-defender",
        )

        target = build_first_team_score_targets(self.connection).targets[0]

        self.assertEqual(target.outcome, "away_first")
        self.assertFalse(target.first_player_target_safe)
        self.assertIsNone(target.first_goal_player_id)


if __name__ == "__main__":
    unittest.main()
