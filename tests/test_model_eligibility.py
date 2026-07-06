from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse


class FixtureModelEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.warehouse = Warehouse(
            Path(self.directory.name) / "test.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.connection = self.warehouse.connection
        self.now = datetime.now(timezone.utc)
        self._insert_complete_fixture()

    def tearDown(self):
        self.warehouse.close()
        self.directory.cleanup()

    def _insert_complete_fixture(self):
        c = self.connection
        c.execute(
            """INSERT INTO competition (
                   competition_id, name, country_code, competition_type
               ) VALUES ('competition', 'Test League', 'XX', 'domestic_league')"""
        )
        c.execute(
            """INSERT INTO season (season_id, competition_id, name)
               VALUES ('season', 'competition', '2025')"""
        )
        c.execute(
            """INSERT INTO team (team_id, name, normalized_name, team_type)
               VALUES ('home', 'Home', 'home', 'club')"""
        )
        c.execute(
            """INSERT INTO team (team_id, name, normalized_name, team_type)
               VALUES ('away', 'Away', 'away', 'club')"""
        )
        c.execute(
            """
            INSERT INTO fixture (
                fixture_id, competition_id, season_id, home_team_id, away_team_id,
                scheduled_kickoff, status
            ) VALUES ('fixture', 'competition', 'season', 'home', 'away', ?, 'completed')
            """,
            [self.now],
        )
        c.execute(
            """
            INSERT INTO fixture_result_observation (
                observation_id, fixture_id, source_code, retrieved_at,
                home_score_regulation, away_score_regulation, result_status
            ) VALUES ('result', 'fixture', 'test', ?, 2, 1, 'final')
            """,
            [self.now],
        )
        for team_id in ("home", "away"):
            c.execute(
                """
                INSERT INTO team_match_stat_observation (
                    observation_id, fixture_id, team_id, source_code, period,
                    shots, shots_on_target, corners, retrieved_at
                ) VALUES (?, 'fixture', ?, 'test', 'regulation', 10, 4, 5, ?)
                """,
                [f"team-stat-{team_id}", team_id, self.now],
            )
            c.execute(
                """
                INSERT INTO lineup_snapshot (
                    lineup_snapshot_id, fixture_id, team_id, source_code,
                    lineup_type, retrieved_at, is_complete
                ) VALUES (?, 'fixture', ?, 'test', 'confirmed', ?, true)
                """,
                [f"lineup-{team_id}", team_id, self.now],
            )
            for index in range(11):
                player_id = f"{team_id}-player-{index}"
                c.execute(
                    "INSERT INTO player VALUES (?, ?, ?, NULL, NULL, NULL, current_timestamp)",
                    [player_id, player_id, player_id],
                )
                c.execute(
                    """
                    INSERT INTO lineup_player (
                        lineup_snapshot_id, player_id, selection_role
                    ) VALUES (?, ?, 'starter')
                    """,
                    [f"lineup-{team_id}", player_id],
                )
                c.execute(
                    """
                    INSERT INTO player_match_stat_observation (
                        observation_id, fixture_id, team_id, player_id,
                        source_code, minutes_played, started, retrieved_at
                    ) VALUES (?, 'fixture', ?, ?, 'test', 90, true, ?)
                    """,
                    [f"player-stat-{player_id}", team_id, player_id, self.now],
                )

    def eligibility(self):
        return self.connection.execute(
            """SELECT eligible_result_models, eligible_team_models,
                      eligible_player_models, reason_codes
               FROM fixture_model_eligibility WHERE fixture_id='fixture'"""
        ).fetchone()

    def test_three_flags_change_with_component_quality(self):
        self.assertEqual(self.eligibility(), (True, True, True, []))

        self.connection.execute(
            "UPDATE player_match_stat_observation SET minutes_played=NULL"
        )
        self.assertEqual(
            self.eligibility(),
            (True, True, False, ["player_data_incomplete"]),
        )

        self.connection.execute("DELETE FROM team_match_stat_observation")
        self.assertEqual(
            self.eligibility(),
            (
                True,
                False,
                False,
                ["team_data_incomplete", "player_data_incomplete"],
            ),
        )

        self.connection.execute(
            "UPDATE fixture SET status='administrative_result_unplayed'"
        )
        self.assertEqual(
            self.eligibility(),
            (False, False, False, ["administrative_unplayed"]),
        )


if __name__ == "__main__":
    unittest.main()
