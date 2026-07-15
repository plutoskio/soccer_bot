from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse
from soccer_bot.config import load_json
from soccer_bot.datasets.targets import (
    RegulationTargetExclusion,
    TargetConstructionError,
    build_regulation_score_targets,
    load_regulation_target_exclusions,
)


class RegulationScoreTargetTests(unittest.TestCase):
    def setUp(self):
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

    def tearDown(self):
        self.warehouse.close()
        self.directory.cleanup()

    def _insert_dimensions(self):
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

    def _insert_fixture(
        self,
        fixture_id: str,
        *,
        status: str = "completed",
        kickoff: datetime | None = None,
    ):
        self.connection.execute(
            """INSERT INTO fixture (
                   fixture_id, competition_id, season_id, home_team_id,
                   away_team_id, scheduled_kickoff, status
               ) VALUES (?, 'competition', 'season', 'home', 'away', ?, ?)""",
            [fixture_id, kickoff or self.kickoff, status],
        )

    def _insert_result(
        self,
        fixture_id: str,
        source: str,
        home_goals: int,
        away_goals: int,
    ):
        self.connection.execute(
            """INSERT INTO fixture_result_observation (
                   observation_id, fixture_id, source_code, retrieved_at,
                   home_score_regulation, away_score_regulation, result_status
               ) VALUES (?, ?, ?, ?, ?, ?, 'final')""",
            [
                f"{fixture_id}-{source}",
                fixture_id,
                source,
                self.kickoff + timedelta(hours=3),
                home_goals,
                away_goals,
            ],
        )

    def test_builds_one_target_when_valid_provider_scores_agree(self):
        self._insert_fixture("fixture")
        self._insert_result("fixture", "source-a", 2, 1)
        self._insert_result("fixture", "source-b", 2, 1)

        targets = build_regulation_score_targets(self.connection)

        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.fixture_id, "fixture")
        self.assertEqual((target.home_goals, target.away_goals), (2, 1))
        self.assertEqual(target.result, "home_win")
        self.assertEqual(target.total_goals, 3)
        self.assertEqual(target.goal_difference, 1)
        self.assertTrue(target.both_teams_to_score)
        self.assertEqual(
            target.agreeing_source_codes,
            ("source-a", "source-b"),
        )
        self.assertEqual(target.prediction_at, target.kickoff - timedelta(hours=24))

    def test_administrative_result_is_excluded_by_eligibility(self):
        self._insert_fixture(
            "administrative", status="administrative_result_unplayed"
        )
        self._insert_result("administrative", "source-a", 3, 0)

        self.assertEqual(build_regulation_score_targets(self.connection), [])

    def test_conflicting_valid_final_scores_fail_the_build(self):
        self._insert_fixture("conflict")
        self._insert_result("conflict", "source-a", 1, 0)
        self._insert_result("conflict", "source-b", 2, 0)

        with self.assertRaisesRegex(
            TargetConstructionError,
            r"Conflicting final regulation scores.*\(1, 0\).*\(2, 0\)",
        ):
            build_regulation_score_targets(self.connection)

    def test_reviewed_conflict_can_be_excluded_without_weakening_new_conflicts(self):
        self._insert_fixture("reviewed-conflict")
        self._insert_result("reviewed-conflict", "source-a", 1, 0)
        self._insert_result("reviewed-conflict", "source-b", 2, 0)

        targets = build_regulation_score_targets(
            self.connection,
            reviewed_exclusions={
                "reviewed-conflict": RegulationTargetExclusion(
                    fixture_id="reviewed-conflict",
                    observed_scores=frozenset({(1, 0), (2, 0)}),
                )
            },
        )

        self.assertEqual(targets, [])

    def test_repository_reviewed_exclusion_file_is_valid(self):
        fixture_ids = load_regulation_target_exclusions(
            ROOT
            / "config"
            / "models"
            / "regulation_score_exclusions_v1.json"
        )

        self.assertEqual(len(fixture_ids), 4)
        self.assertIn("647cba36-89eb-5b54-83c0-4561c223daf3", fixture_ids)

    def test_task_spec_references_versioned_contracts_and_exclusions(self):
        specification = load_json(
            ROOT / "config" / "models" / "regulation_score_v1.json"
        )

        self.assertEqual(specification["task_version"], "regulation_score_v1")
        self.assertEqual(
            specification["target"]["eligibility_flag"],
            "eligible_result_models",
        )
        self.assertEqual(
            specification["target"]["conflicting_final_score_policy"],
            "exclude_reviewed_and_fail_unreviewed",
        )
        self.assertTrue((ROOT / specification["contract_registry"]).is_file())
        self.assertTrue(
            (ROOT / specification["target"]["reviewed_exclusions"]).is_file()
        )

    def test_reviewed_exclusion_must_match_current_score_evidence(self):
        self._insert_fixture("changed-conflict")
        self._insert_result("changed-conflict", "source-a", 1, 0)
        self._insert_result("changed-conflict", "source-b", 2, 0)

        with self.assertRaisesRegex(
            TargetConstructionError,
            "Reviewed exclusion no longer matches",
        ):
            build_regulation_score_targets(
                self.connection,
                reviewed_exclusions={
                    "changed-conflict": RegulationTargetExclusion(
                        fixture_id="changed-conflict",
                        observed_scores=frozenset({(0, 0), (1, 0)}),
                    )
                },
            )

    def test_kickoff_window_is_start_inclusive_and_end_exclusive(self):
        self._insert_fixture("early", kickoff=self.kickoff)
        self._insert_result("early", "source-a", 0, 0)
        later = self.kickoff + timedelta(days=1)
        self._insert_fixture("later", kickoff=later)
        self._insert_result("later", "source-a", 0, 1)

        targets = build_regulation_score_targets(
            self.connection,
            kickoff_start=self.kickoff,
            kickoff_end=later,
        )

        self.assertEqual([target.fixture_id for target in targets], ["early"])
        self.assertEqual(targets[0].result, "draw")
        self.assertFalse(targets[0].both_teams_to_score)


if __name__ == "__main__":
    unittest.main()
