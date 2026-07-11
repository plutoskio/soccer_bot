from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collection_state import (
    checkpoint_is_retryable,
    checkpoint_is_stopping,
    events_processing_result,
    reconcile_fixture_components,
    record_component_result,
    validate_events,
    validate_identity_linking,
    validate_lineups,
    validate_player_statistics,
    validate_result,
    validate_team_statistics,
)
from soccer_bot.database import Warehouse


class ComponentValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.warehouse = Warehouse(
            Path(self.directory.name) / "test.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.warehouse.register_sources()
        self.connection = self.warehouse.connection
        self.now = datetime(2026, 7, 10, 20, tzinfo=timezone.utc)
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
        for team_id, name in (("home", "Home"), ("away", "Away")):
            c.execute(
                """INSERT INTO team (
                       team_id, name, normalized_name, team_type
                   ) VALUES (?, ?, ?, 'club')""",
                [team_id, name, name.lower()],
            )
        c.execute(
            """INSERT INTO fixture (
                   fixture_id, competition_id, season_id, home_team_id,
                   away_team_id, scheduled_kickoff, status
               ) VALUES ('fixture', 'competition', 'season', 'home', 'away', ?, 'completed')""",
            [datetime(2026, 7, 10, 18, tzinfo=timezone.utc)],
        )
        c.execute(
            """INSERT INTO source_entity_map (
                   source_code, entity_type, source_entity_id,
                   internal_entity_id, match_method, confidence, review_status
               ) VALUES ('api_football', 'fixture', '100', 'fixture',
                         'provider_source_id', 1.0, 'automatic')"""
        )
        c.execute(
            """INSERT INTO fixture_schedule_observation (
                   schedule_observation_id, fixture_id, source_code,
                   fixture_source_id, provider_status, canonical_status,
                   scheduled_kickoff, retrieved_at, raw_artifact_id
               ) VALUES ('schedule', 'fixture', 'api_football', '100', 'FT',
                         'final', ?, ?, 'schedule-artifact')""",
            [datetime(2026, 7, 10, 18, tzinfo=timezone.utc), self.now],
        )
        c.execute(
            """INSERT INTO fixture_result_observation (
                   observation_id, fixture_id, source_code, raw_artifact_id,
                   retrieved_at, home_score_regulation, away_score_regulation,
                   result_status
               ) VALUES ('result', 'fixture', 'api_football', 'result-artifact',
                         ?, 2, 1, 'final')""",
            [self.now],
        )
        for team_id in ("home", "away"):
            c.execute(
                """INSERT INTO team_match_stat_observation (
                       observation_id, fixture_id, team_id, source_code,
                       raw_artifact_id, period, shots, shots_on_target,
                       corners, retrieved_at
                   ) VALUES (?, 'fixture', ?, 'api_football', 'team-artifact',
                             'regulation', 10, 4, 5, ?)""",
                [f"team-stat-{team_id}", team_id, self.now],
            )
            c.execute(
                """INSERT INTO lineup_snapshot (
                       lineup_snapshot_id, fixture_id, team_id, source_code,
                       raw_artifact_id, lineup_type, retrieved_at, is_complete
                   ) VALUES (?, 'fixture', ?, 'api_football', 'lineup-artifact',
                             'confirmed', ?, true)""",
                [f"lineup-{team_id}", team_id, self.now],
            )
            for index in range(11):
                player_id = f"{team_id}-player-{index}"
                c.execute(
                    """INSERT INTO player (
                           player_id, full_name, normalized_name
                       ) VALUES (?, ?, ?)""",
                    [player_id, player_id, player_id],
                )
                c.execute(
                    """INSERT INTO lineup_player (
                           lineup_snapshot_id, player_id, selection_role
                       ) VALUES (?, ?, 'starter')""",
                    [f"lineup-{team_id}", player_id],
                )
                c.execute(
                    """INSERT INTO player_match_stat_observation (
                           observation_id, fixture_id, team_id, player_id,
                           source_code, raw_artifact_id, minutes_played,
                           started, goals, assists, shots, shots_on_target,
                           passes, accurate_passes, retrieved_at
                       ) VALUES (?, 'fixture', ?, ?, 'api_football',
                                 'player-artifact', 90, true, 0, 0,
                                 2, 1, 20, 16, ?)""",
                    [f"player-stat-{player_id}", team_id, player_id, self.now],
                )

    def test_complete_components_are_valid(self):
        self.assertEqual("complete", validate_result(self.connection, "fixture").state)
        self.assertEqual(
            "complete", validate_lineups(self.connection, "fixture", now=self.now).state
        )
        self.assertEqual(
            "complete",
            validate_team_statistics(self.connection, "fixture", now=self.now).state,
        )
        self.assertEqual(
            "complete",
            validate_player_statistics(self.connection, "fixture", now=self.now).state,
        )

    def test_lineup_validator_rejects_missing_starter(self):
        self.connection.execute(
            "DELETE FROM lineup_player WHERE lineup_snapshot_id='lineup-home' "
            "AND player_id='home-player-0'"
        )
        result = validate_lineups(self.connection, "fixture", now=self.now)
        self.assertEqual("invalid", result.state)
        self.assertEqual("invalid_lineups", result.reason_code)

    def test_partial_player_statistics_remain_retryable(self):
        self.connection.execute(
            "DELETE FROM player_match_stat_observation "
            "WHERE observation_id='player-stat-away-player-0'"
        )
        result = validate_player_statistics(self.connection, "fixture", now=self.now)
        self.assertEqual("retryable", result.state)
        self.assertEqual("incomplete_player_statistics", result.reason_code)

    def test_provider_unavailable_player_stats_are_terminally_recorded(self):
        self.connection.execute("DELETE FROM player_match_stat_observation")
        self.connection.execute(
            """
            INSERT INTO data_quality_issue (
                issue_id, rule_code, severity, entity_type,
                internal_entity_id, source_code, raw_artifact_id, details
            ) VALUES ('unavailable-player-stats', 'api_player_stats_unavailable',
                      'warning', 'fixture', 'fixture', 'api_football',
                      'unavailable-artifact', '{"reason":"provider_absent"}')
            """
        )
        result = validate_player_statistics(
            self.connection, "fixture", now=self.now
        )
        self.assertEqual("unavailable", result.state)
        self.assertEqual("unavailable-artifact", result.last_raw_artifact_id)

        record_component_result(
            self.connection,
            fixture_id="fixture",
            source_code="api_football",
            component_code="player_statistics",
            result=result,
            now=self.now,
        )
        self.connection.execute(
            "UPDATE data_quality_issue SET status='resolved' WHERE issue_id='unavailable-player-stats'"
        )
        reconcile_fixture_components(
            self.connection, "fixture", "api_football", self.now
        )
        self.assertEqual(
            ("unavailable",),
            self.connection.execute(
                """
                SELECT state FROM fixture_collection_component
                WHERE fixture_id='fixture' AND component_code='player_statistics'
                """
            ).fetchone(),
        )

    def test_unresolved_identity_is_nonblocking_terminal_after_final(self):
        self.connection.execute(
            """
            INSERT INTO source_entity_map (
                source_code, entity_type, source_entity_id, internal_entity_id,
                match_method, confidence, review_status
            ) VALUES ('api_football_lineup', 'player', '100|1|home player',
                      'home-player-0', 'unresolved_alias', 0, 'pending')
            """
        )
        result = validate_identity_linking(self.connection, "fixture")
        self.assertEqual("terminal", result.state)
        self.assertEqual("unresolved_identity_warning", result.reason_code)
        self.assertEqual(1, result.details["unresolved"])

    def test_empty_processed_events_are_complete(self):
        result = events_processing_result(
            processed=True,
            event_count=0,
            raw_artifact_id="events-artifact",
        )
        record_component_result(
            self.connection,
            fixture_id="fixture",
            source_code="api_football",
            component_code="events",
            result=result,
            now=self.now,
        )
        stored = validate_events(self.connection, "fixture", now=self.now)
        self.assertEqual("complete", stored.state)
        self.assertEqual(0, stored.details["event_count"])

    def test_reconciliation_persists_component_states(self):
        record_component_result(
            self.connection,
            fixture_id="fixture",
            source_code="api_football",
            component_code="events",
            result=events_processing_result(processed=True, event_count=0),
            now=self.now,
        )
        results = reconcile_fixture_components(
            self.connection, "fixture", "api_football", self.now
        )
        self.assertEqual("complete", results["result"].state)
        self.assertEqual("complete", results["events"].state)
        rows = self.connection.execute(
            """SELECT component_code, state, required_for_fixture_terminal
               FROM fixture_collection_component
               WHERE fixture_id='fixture' AND source_code='api_football'
               ORDER BY component_code"""
        ).fetchall()
        self.assertEqual(
            [
                ("events", "complete", True),
                ("identity_linking", "complete", False),
                ("lineups", "complete", True),
                ("player_statistics", "complete", True),
                ("result", "complete", True),
                ("team_statistics", "complete", True),
            ],
            rows,
        )


class CheckpointStateTests(unittest.TestCase):
    def test_retryable_and_stopping_states_are_distinct(self):
        self.assertTrue(checkpoint_is_retryable("incomplete"))
        self.assertTrue(checkpoint_is_retryable("rate_limited"))
        self.assertFalse(checkpoint_is_stopping("incomplete"))
        self.assertTrue(checkpoint_is_stopping("succeeded"))
        self.assertTrue(checkpoint_is_stopping("terminal"))
        self.assertTrue(checkpoint_is_stopping("skipped"))
        self.assertTrue(checkpoint_is_stopping("skipped_with_reason"))


if __name__ == "__main__":
    unittest.main()
