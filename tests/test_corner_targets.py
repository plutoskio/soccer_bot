from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from soccer_bot.database import Warehouse
from soccer_bot.datasets.corners import build_corner_targets


ROOT = Path(__file__).resolve().parents[1]


class CornerTargetTests(unittest.TestCase):
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

    def _insert_fixture(self, fixture_id: str = "fixture") -> None:
        self.connection.execute(
            """INSERT INTO fixture (
                   fixture_id, competition_id, season_id, home_team_id,
                   away_team_id, scheduled_kickoff, status
               ) VALUES (?, 'competition', 'season', 'home', 'away', ?, 'completed')""",
            [fixture_id, self.kickoff],
        )
        self.connection.execute(
            """INSERT INTO fixture_result_observation (
                   observation_id, fixture_id, source_code, retrieved_at,
                   home_score_regulation, away_score_regulation, result_status
               ) VALUES (?, ?, 'source-a', ?, 2, 1, 'final')""",
            [f"{fixture_id}-result", fixture_id, self.kickoff + timedelta(hours=3)],
        )

    def _insert_team_stats(
        self,
        *,
        fixture_id: str = "fixture",
        source: str = "source-a",
        artifact: str = "artifact-a",
        home_corners: int = 7,
        away_corners: int = 4,
    ) -> None:
        for team, corners in (("home", home_corners), ("away", away_corners)):
            self.connection.execute(
                """INSERT INTO team_match_stat_observation (
                       observation_id, fixture_id, team_id, source_code,
                       raw_artifact_id, period, shots, shots_on_target,
                       possession_pct, corners, retrieved_at
                   ) VALUES (?, ?, ?, ?, ?, 'regulation', 10, 4, 50, ?, ?)""",
                [
                    f"{fixture_id}-{source}-{artifact}-{team}",
                    fixture_id,
                    team,
                    source,
                    artifact,
                    corners,
                    self.kickoff + timedelta(hours=3),
                ],
            )

    def test_builds_joint_corner_target_from_complete_artifact(self) -> None:
        self._insert_fixture()
        self._insert_team_stats()

        result = build_corner_targets(self.connection)

        self.assertEqual(result.conflicts, ())
        self.assertEqual(len(result.targets), 1)
        target = result.targets[0]
        self.assertEqual((target.home_corners, target.away_corners), (7, 4))
        self.assertEqual(target.total_corners, 11)
        self.assertEqual(target.corner_difference, 3)
        self.assertEqual(target.agreeing_source_codes, ("source-a",))

    def test_conflicting_provider_targets_are_excluded_and_audited(self) -> None:
        self._insert_fixture()
        self._insert_team_stats()
        self._insert_team_stats(
            source="source-b",
            artifact="artifact-b",
            home_corners=8,
            away_corners=4,
        )

        result = build_corner_targets(self.connection)

        self.assertEqual(result.targets, ())
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].observed_pairs, ((7, 4), (8, 4)))

    def test_identical_providers_are_retained_with_both_sources(self) -> None:
        self._insert_fixture()
        self._insert_team_stats()
        self._insert_team_stats(source="source-b", artifact="artifact-b")

        target = build_corner_targets(self.connection).targets[0]

        self.assertEqual(target.agreeing_source_codes, ("source-a", "source-b"))

    def test_forward_target_waits_for_actual_retrieval(self) -> None:
        self._insert_fixture()
        self._insert_team_stats()

        target = build_corner_targets(
            self.connection,
            strict_retrieval_from=self.kickoff - timedelta(days=1),
        ).targets[0]

        self.assertEqual(target.source_max_retrieved_at, self.kickoff + timedelta(hours=3))
        self.assertEqual(target.target_available_at, self.kickoff + timedelta(hours=3))

    def test_missing_corner_excludes_fixture_through_team_eligibility(self) -> None:
        self._insert_fixture()
        self._insert_team_stats()
        self.connection.execute(
            """UPDATE team_match_stat_observation
               SET corners=NULL WHERE team_id='away'"""
        )

        result = build_corner_targets(self.connection)

        self.assertEqual(result.targets, ())
        self.assertEqual(result.conflicts, ())


if __name__ == "__main__":
    unittest.main()
