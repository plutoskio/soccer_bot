from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.backfill_executor import (
    BackfillValidationError,
    HistoricalBackfillExecutor,
    validate_manifest,
)
from soccer_bot.database import Warehouse
from soccer_bot.http import HttpResponse
from soccer_bot.loaders import api_player_identity_key
from soccer_bot.raw_store import RawArtifactStore


def detailed_match(fixture_id: int = 9001) -> dict:
    def starters(prefix: int, label: str) -> list[dict]:
        return [
            {"player": {
                "id": prefix + index, "name": f"{label} {index}",
                "number": index, "pos": "F", "grid": f"{(index - 1) // 4 + 1}:{(index - 1) % 4 + 1}",
            }}
            for index in range(1, 12)
        ]

    def player_stat(player_id: int, name: str) -> dict:
        return {
            "player": {"id": player_id, "name": name},
            "statistics": [{
                "games": {
                    "minutes": 90, "number": 9, "position": "Attacker",
                    "rating": "7.0", "captain": False, "substitute": False,
                },
                "goals": {"total": 0, "assists": 0, "conceded": 0, "saves": None},
                "shots": {"total": 1, "on": 0},
                "passes": {"key": 0, "total": 20, "accuracy": 16},
                "tackles": {"total": 0, "blocks": 0, "interceptions": 0},
                "duels": {"total": 1, "won": 1},
                "dribbles": {"attempts": 0, "success": 0, "past": 0},
                "fouls": {"drawn": 0, "committed": 0},
                "cards": {"yellow": 0, "yellowred": 0, "red": 0},
                "penalty": {"won": 0, "commited": 0, "scored": 0, "missed": 0, "saved": 0},
            }],
        }

    home_starters = starters(1000, "Home")
    away_starters = starters(2000, "Away")
    return {
        "fixture": {
            "id": fixture_id, "date": "2025-08-01T18:00:00+00:00",
            "status": {"short": "FT"}, "venue": {"name": "Test Ground"},
        },
        "league": {
            "id": 39, "name": "Premier League", "country": "England",
            "season": 2025, "round": "Regular Season - 1",
        },
        "teams": {
            "home": {"id": 10, "name": "Home FC"},
            "away": {"id": 20, "name": "Away FC"},
        },
        "score": {
            "fulltime": {"home": 1, "away": 0},
            "halftime": {"home": 0, "away": 0},
        },
        "lineups": [
            {"team": {"id": 10, "name": "Home FC"}, "formation": "4-3-3", "startXI": home_starters, "substitutes": []},
            {"team": {"id": 20, "name": "Away FC"}, "formation": "4-3-3", "startXI": away_starters, "substitutes": []},
        ],
        "events": [],
        "players": [
            {"team": {"id": 10, "name": "Home FC"}, "players": [
                player_stat(item["player"]["id"], item["player"]["name"]) for item in home_starters
            ]},
            {"team": {"id": 20, "name": "Away FC"}, "players": [
                player_stat(item["player"]["id"], item["player"]["name"]) for item in away_starters
            ]},
        ],
        "statistics": [
            {"team": {"id": 10, "name": "Home FC"}, "statistics": [
                {"type": "Total Shots", "value": 10},
                {"type": "Shots on Goal", "value": 4},
                {"type": "Corner Kicks", "value": 5},
            ]},
            {"team": {"id": 20, "name": "Away FC"}, "statistics": [
                {"type": "Total Shots", "value": 8},
                {"type": "Shots on Goal", "value": 2},
                {"type": "Corner Kicks", "value": 3},
            ]},
        ],
    }


class FakeHttp:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def get(self, base_url, path, *, params=None, headers=None, timeout=30.0):
        self.calls.append({"base_url": base_url, "path": path, "params": params})
        return HttpResponse(
            f"{base_url}{path}", 200, {"content-type": "application/json"},
            json.dumps(self.payload).encode(),
        )


class BackfillExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.warehouse = Warehouse(
            self.root / "test.duckdb", ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.warehouse.register_sources()
        self.batch = {
            "batch_id": "api-football-39-2025-0001",
            "league_id": 39, "season": 2025, "fixture_ids": [9001],
        }
        self.row = {
            "api_fixture_id": 9001, "league_id": 39, "season": 2025,
            "action": "NEW_FIXTURE", "home_api_team_id": 10,
            "away_api_team_id": 20, "kickoff": "2025-08-01T18:00:00+00:00",
        }
        self.config = {
            "api_football": {
                "daily_limit": 7500, "reserve_calls": 250,
                "minimum_interval_seconds": 0, "fixture_batch_size": 20,
                "request_timeout_seconds": 5,
            },
            "validation": {
                "minimum_participating_players": 22,
                "passing_coverage_warning_threshold": 0.8,
                "kickoff_tolerance_seconds": 60,
            },
        }

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    def executor(self, fake: FakeHttp) -> HistoricalBackfillExecutor:
        return HistoricalBackfillExecutor(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=fake,
            api_key="test-key",
            config=self.config,
            batches=[self.batch],
            manifest_rows=[self.row],
            manifest_sha256="manifest-v1",
        )

    def test_manifest_rejects_duplicate_fixture_assignment(self):
        duplicate = dict(self.batch, batch_id="api-football-39-2025-0002")
        with self.assertRaisesRegex(BackfillValidationError, "more than one batch"):
            validate_manifest([self.batch, duplicate], [self.row])

    def test_success_is_transactional_checkpointed_and_idempotent(self):
        match = detailed_match()
        match["lineups"][0]["startXI"][0]["player"]["name"] = "A. Zoubir"
        match["players"][0]["players"][0]["player"] = {
            "id": 9999, "name": "Abdellah Zoubir"
        }
        match["players"][0]["players"][0]["statistics"][0]["games"]["number"] = 1
        fake = FakeHttp({"errors": [], "response": [match]})
        progress = io.StringIO()
        executor = self.executor(fake)
        with redirect_stdout(progress):
            first = executor.run(maximum_batches=1, execute=True)
        progress_text = progress.getvalue()
        self.assertIn("Starting historical backfill: 1 batches", progress_text)
        self.assertIn("Progress 1/1 batches (100.0%)", progress_text)
        self.assertIn("API calls 1", progress_text)
        self.assertIn("last api-football-39-2025-0001", progress_text)
        self.assertEqual(1, first["api_calls"])
        self.assertEqual(1, first["completed_batches"])
        self.assertEqual(1, first["global_quality_audits"])
        self.assertEqual(1, len(fake.calls))
        self.assertEqual(1, self.warehouse.connection.execute("SELECT count(*) FROM fixture").fetchone()[0])
        self.assertEqual(22, self.warehouse.connection.execute(
            "SELECT count(*) FROM player_match_stat_observation"
        ).fetchone()[0])
        stats_id = self.warehouse.connection.execute(
            "SELECT internal_entity_id FROM source_entity_map WHERE source_code='api_football' AND entity_type='player' AND source_entity_id=?",
            [api_player_identity_key(9999, "Abdellah Zoubir")],
        ).fetchone()[0]
        lineup_id = self.warehouse.connection.execute(
            """SELECT lp.player_id FROM lineup_snapshot ls
               JOIN lineup_player lp USING (lineup_snapshot_id)
               WHERE ls.fixture_id=(SELECT internal_entity_id FROM source_entity_map
                   WHERE source_code='api_football' AND entity_type='fixture' AND source_entity_id='9001')
                 AND ls.team_id=(SELECT internal_entity_id FROM source_entity_map
                   WHERE source_code='api_football' AND entity_type='team' AND source_entity_id='10')
                 AND lp.selection_role='starter' AND lp.formation_grid='1:1'"""
        ).fetchone()[0]
        self.assertEqual(lineup_id, stats_id)
        self.assertIn(
            api_player_identity_key(9999, "Abdellah Zoubir"),
            executor.loader._api_runtime_source_ids_by_internal[stats_id],
        )
        checkpoint = self.warehouse.connection.execute(
            "SELECT status, requested_count, returned_count, validated_count FROM historical_backfill_batch_checkpoint"
        ).fetchone()
        self.assertEqual(("succeeded", 1, 1, 1), checkpoint)

        second = self.executor(fake).run(maximum_batches=1, execute=True)
        self.assertEqual(0, second["api_calls"])
        self.assertEqual([], second["selected_batches"])
        self.assertEqual(1, len(fake.calls))

    def test_raw_validation_failure_never_writes_relational_rows(self):
        match = detailed_match()
        match["lineups"][0]["startXI"].pop()
        fake = FakeHttp({"errors": [], "response": [match]})
        with self.assertRaisesRegex(BackfillValidationError, "Raw fixture validation failed"):
            self.executor(fake).run(maximum_batches=1, execute=True)
        self.assertEqual(0, self.warehouse.connection.execute("SELECT count(*) FROM fixture").fetchone()[0])
        self.assertEqual(1, self.warehouse.connection.execute("SELECT count(*) FROM raw_artifact").fetchone()[0])
        self.assertEqual("failed", self.warehouse.connection.execute(
            "SELECT status FROM historical_backfill_batch_checkpoint"
        ).fetchone()[0])

        # Explicit retry reuses the immutable raw response instead of spending
        # another API call on data already known to be structurally incomplete.
        retried = self.executor(fake)
        with self.assertRaisesRegex(BackfillValidationError, "Raw fixture validation failed"):
            retried.run(maximum_batches=1, execute=True, retry_failed=True)
        self.assertEqual(0, retried.api_calls)
        self.assertEqual(1, retried.cache_hits)
        self.assertEqual(1, len(fake.calls))

    def test_negative_team_stat_is_rejected_by_batch_scoped_validation(self):
        match = detailed_match()
        match["statistics"][0]["statistics"][2]["value"] = -1
        fake = FakeHttp({"errors": [], "response": [match]})

        with self.assertRaisesRegex(
            BackfillValidationError, "Relational fixture validation failed"
        ):
            self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture"
        ).fetchone()[0])

    def test_low_passing_coverage_is_saved_as_warning_not_rejected(self):
        match = detailed_match()
        # Seven of 22 participants lack complete passing data: 68.18%
        # coverage. Passing fields remain honest nulls, while the otherwise
        # complete fixture is retained.
        for record in match["players"][0]["players"][:7]:
            record["statistics"][0]["passes"]["accuracy"] = None
        fake = FakeHttp({"errors": [], "response": [match]})

        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        self.assertEqual(7, self.warehouse.connection.execute(
            """SELECT count(*) FROM player_match_stat_observation
               WHERE minutes_played > 0 AND accurate_passes IS NULL"""
        ).fetchone()[0])
        issue = self.warehouse.connection.execute(
            """SELECT severity, entity_type, status
               FROM data_quality_issue
               WHERE rule_code='low_player_passing_coverage'"""
        ).fetchone()
        self.assertEqual(("warning", "fixture", "open"), issue)

    def test_missing_provider_player_blocks_are_retained_as_warning(self):
        match = detailed_match()
        match["players"] = []
        fake = FakeHttp({"errors": [], "response": [match]})

        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        self.assertEqual(1, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture_result_observation"
        ).fetchone()[0])
        self.assertEqual(2, self.warehouse.connection.execute(
            "SELECT count(*) FROM lineup_snapshot"
        ).fetchone()[0])
        self.assertEqual(2, self.warehouse.connection.execute(
            "SELECT count(*) FROM team_match_stat_observation"
        ).fetchone()[0])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM player_match_stat_observation"
        ).fetchone()[0])
        issue = self.warehouse.connection.execute(
            """SELECT severity, entity_type, status
               FROM data_quality_issue
               WHERE rule_code='api_player_stats_unavailable'"""
        ).fetchone()
        self.assertEqual(("warning", "fixture", "open"), issue)
        validation = self.warehouse.connection.execute(
            "SELECT validation FROM historical_backfill_batch_checkpoint"
        ).fetchone()[0]
        validation = json.loads(validation) if isinstance(validation, str) else validation
        raw_state = validation["raw"]["fixtures"][0]
        relational_state = validation["relational"]["fixtures"][0]
        self.assertTrue(raw_state["accepted_partial"])
        self.assertTrue(raw_state["player_stats_unavailable"])
        self.assertFalse(relational_state["player_stats_expected"])
        self.assertEqual(0, relational_state["player_rows"])

    def test_missing_team_and_player_sections_retain_score_lineups_and_events(self):
        match = detailed_match()
        match["statistics"] = []
        match["players"] = []
        fake = FakeHttp({"errors": [], "response": [match]})

        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        self.assertEqual(1, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture_result_observation"
        ).fetchone()[0])
        self.assertEqual(2, self.warehouse.connection.execute(
            "SELECT count(*) FROM lineup_snapshot"
        ).fetchone()[0])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM team_match_stat_observation"
        ).fetchone()[0])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM player_match_stat_observation"
        ).fetchone()[0])
        issues = self.warehouse.connection.execute(
            """SELECT rule_code, severity FROM data_quality_issue
               WHERE rule_code IN (
                   'api_team_stats_unavailable', 'api_player_stats_unavailable'
               ) ORDER BY rule_code"""
        ).fetchall()
        self.assertEqual([
            ("api_player_stats_unavailable", "warning"),
            ("api_team_stats_unavailable", "warning"),
        ], issues)
        validation = self.warehouse.connection.execute(
            "SELECT validation FROM historical_backfill_batch_checkpoint"
        ).fetchone()[0]
        validation = json.loads(validation) if isinstance(validation, str) else validation
        raw_state = validation["raw"]["fixtures"][0]
        relational_state = validation["relational"]["fixtures"][0]
        self.assertTrue(raw_state["accepted_partial"])
        self.assertTrue(raw_state["team_stats_unavailable"])
        self.assertTrue(raw_state["player_stats_unavailable"])
        self.assertFalse(relational_state["team_stats_expected"])
        self.assertFalse(relational_state["player_stats_expected"])

    def test_one_team_stat_block_is_rejected_as_malformed_partial_data(self):
        match = detailed_match()
        match["statistics"] = match["statistics"][:1]
        match["players"] = []
        fake = FakeHttp({"errors": [], "response": [match]})

        with self.assertRaisesRegex(
            BackfillValidationError, "Raw fixture validation failed"
        ):
            self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture"
        ).fetchone()[0])

    def test_explicit_unplayed_administrative_result_stores_only_result(self):
        match = detailed_match()
        match["events"] = []
        match["players"] = []
        match["lineups"] = [
            {"team": {"id": 10, "name": "Home FC"}, "formation": None},
            {"team": {"id": 20, "name": "Away FC"}, "formation": None},
        ]
        match["statistics"] = [
            {"team": {"id": 10, "name": "Home FC"}, "statistics": []},
            {"team": {"id": 20, "name": "Away FC"}, "statistics": []},
        ]
        self.config["validation"]["administrative_result_fixtures"] = {
            "9001": {
                "classification": "administrative_result_unplayed",
                "reason": "test fixture was not played",
            }
        }
        fake = FakeHttp({"errors": [], "response": [match]})

        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        self.assertEqual(
            "administrative_result_unplayed",
            self.warehouse.connection.execute(
                "SELECT status FROM fixture"
            ).fetchone()[0],
        )
        self.assertEqual(1, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture_result_observation"
        ).fetchone()[0])
        for table in (
            "lineup_snapshot", "match_event", "team_match_stat_observation",
            "player_match_stat_observation",
        ):
            self.assertEqual(0, self.warehouse.connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0])
        issue = self.warehouse.connection.execute(
            """SELECT severity, status FROM data_quality_issue
               WHERE rule_code='api_administrative_result_unplayed'"""
        ).fetchone()
        self.assertEqual(("warning", "open"), issue)

    def test_unplayed_shape_without_explicit_exception_is_rejected(self):
        match = detailed_match()
        match["events"] = []
        match["players"] = []
        match["lineups"] = [
            {"team": {"id": 10, "name": "Home FC"}, "formation": None},
            {"team": {"id": 20, "name": "Away FC"}, "formation": None},
        ]
        match["statistics"] = []
        fake = FakeHttp({"errors": [], "response": [match]})

        with self.assertRaisesRegex(
            BackfillValidationError, "Raw fixture validation failed"
        ):
            self.executor(fake).run(maximum_batches=1, execute=True)

    def test_starter_repeated_as_substitute_keeps_starter_and_warns(self):
        match = detailed_match()
        duplicate = json.loads(json.dumps(match["lineups"][1]["startXI"][0]))
        match["lineups"][1]["substitutes"].append(duplicate)
        fake = FakeHttp({"errors": [], "response": [match]})

        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        starter_counts = self.warehouse.connection.execute(
            """SELECT count(*) FILTER (WHERE lp.selection_role='starter')
               FROM lineup_snapshot ls JOIN lineup_player lp USING(lineup_snapshot_id)
               GROUP BY ls.team_id ORDER BY 1"""
        ).fetchall()
        self.assertEqual([(11,), (11,)], starter_counts)
        issue = self.warehouse.connection.execute(
            """SELECT severity, status FROM data_quality_issue
               WHERE rule_code='api_lineup_duplicate_entry'"""
        ).fetchone()
        self.assertEqual(("warning", "open"), issue)
        validation = self.warehouse.connection.execute(
            "SELECT validation FROM historical_backfill_batch_checkpoint"
        ).fetchone()[0]
        validation = json.loads(validation) if isinstance(validation, str) else validation
        duplicates = validation["raw"]["fixtures"][0]["lineup_duplicate_entries"]
        self.assertEqual(1, len(duplicates))
        self.assertEqual("starter", duplicates[0]["first_role"])
        self.assertEqual("substitute", duplicates[0]["duplicate_role"])

    def test_duplicate_inside_starting_eleven_is_rejected(self):
        match = detailed_match()
        match["lineups"][1]["startXI"][-1] = json.loads(json.dumps(
            match["lineups"][1]["startXI"][0]
        ))
        fake = FakeHttp({"errors": [], "response": [match]})

        with self.assertRaisesRegex(
            BackfillValidationError, "Raw fixture validation failed"
        ):
            self.executor(fake).run(maximum_batches=1, execute=True)

    def test_placeholder_zero_minute_player_blocks_are_not_imported(self):
        match = detailed_match()
        fields = {
            "goals": ("total", "assists"),
            "shots": ("total", "on"),
            "passes": ("total", "accuracy", "key"),
            "tackles": ("total", "interceptions"),
            "duels": ("total", "won"),
            "dribbles": ("attempts", "success"),
        }
        for block in match["players"]:
            for record in block["players"]:
                statistics = record["statistics"][0]
                statistics["games"]["minutes"] = 0
                statistics["games"]["rating"] = None
                for section, names in fields.items():
                    for name in names:
                        statistics.setdefault(section, {})[name] = None

        fake = FakeHttp({"errors": [], "response": [match]})
        summary = self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(1, summary["completed_batches"])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM player_match_stat_observation"
        ).fetchone()[0])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM appearance"
        ).fetchone()[0])
        issue = self.warehouse.connection.execute(
            """SELECT severity, status FROM data_quality_issue
               WHERE rule_code='api_player_stats_unavailable'"""
        ).fetchone()
        self.assertEqual(("warning", "open"), issue)
        validation = self.warehouse.connection.execute(
            "SELECT validation FROM historical_backfill_batch_checkpoint"
        ).fetchone()[0]
        validation = json.loads(validation) if isinstance(validation, str) else validation
        raw_state = validation["raw"]["fixtures"][0]
        self.assertTrue(raw_state["accepted_partial"])
        self.assertEqual(
            "placeholder_zero_minutes",
            raw_state["player_stats_unavailable_reason"],
        )

    def test_inconsistent_zero_minutes_with_performance_values_is_rejected(self):
        match = detailed_match()
        for block in match["players"]:
            for record in block["players"]:
                record["statistics"][0]["games"]["minutes"] = 0
        fake = FakeHttp({"errors": [], "response": [match]})

        with self.assertRaisesRegex(
            BackfillValidationError, "Raw fixture validation failed"
        ):
            self.executor(fake).run(maximum_batches=1, execute=True)

        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM fixture"
        ).fetchone()[0])

    def test_transliterated_lineup_and_event_names_link_to_stat_identity(self):
        match = detailed_match()
        match["lineups"][0]["startXI"][0]["player"] = {
            "id": 544512, "name": "A. Saether", "number": 1,
            "pos": "G", "grid": "1:1",
        }
        match["players"][0]["players"][0]["player"] = {
            "id": 313648, "name": "Adrian Sæther",
        }
        match["players"][0]["players"][0]["statistics"][0]["games"]["number"] = 1
        match["events"] = [{
            "time": {"elapsed": 10, "extra": None},
            "team": {"id": 10, "name": "Home FC"},
            "player": {"id": 1002, "name": "Home 2"},
            "assist": {"id": 544512, "name": "A. Saether"},
            "type": "Goal", "detail": "Normal Goal", "comments": None,
        }]
        self.executor(FakeHttp({"errors": [], "response": [match]})).run(
            maximum_batches=1, execute=True
        )

        stat_player = self.warehouse.connection.execute(
            """SELECT internal_entity_id FROM source_entity_map
               WHERE source_code='api_football' AND entity_type='player'
                 AND source_entity_id=?""",
            [api_player_identity_key(313648, "Adrian Sæther")],
        ).fetchone()[0]
        lineup_player = self.warehouse.connection.execute(
            """SELECT lp.player_id FROM lineup_snapshot ls
               JOIN lineup_player lp USING (lineup_snapshot_id)
               JOIN player p ON p.player_id=lp.player_id
               WHERE ls.source_code='api_football'
                 AND lp.selection_role='starter' AND lp.formation_grid='1:1'
                 AND ls.team_id=(SELECT internal_entity_id FROM source_entity_map
                     WHERE source_code='api_football' AND entity_type='team'
                       AND source_entity_id='10')"""
        ).fetchone()[0]
        event_assist = self.warehouse.connection.execute(
            "SELECT secondary_player_id FROM match_event WHERE source_code='api_football'"
        ).fetchone()[0]
        self.assertEqual(stat_player, lineup_player)
        self.assertEqual(stat_player, event_assist)

    def test_compound_surname_links_with_matching_shirt_number(self):
        match = detailed_match()
        match["lineups"][0]["startXI"][0]["player"] = {
            "id": 319368, "name": "M. Spiten-Nysaeter", "number": 39,
            "pos": "F", "grid": "1:1",
        }
        match["players"][0]["players"][0]["player"] = {
            "id": 519924, "name": "Mats Spiten",
        }
        match["players"][0]["players"][0]["statistics"][0]["games"]["number"] = 39
        match["events"] = [{
            "time": {"elapsed": 10, "extra": None},
            "team": {"id": 10, "name": "Home FC"},
            "player": {"id": 1002, "name": "Home 2"},
            "assist": {"id": 319368, "name": "M. Spiten-Nysaeter"},
            "type": "Goal", "detail": "Normal Goal", "comments": None,
        }]
        self.executor(FakeHttp({"errors": [], "response": [match]})).run(
            maximum_batches=1, execute=True
        )

        stat_player = self.warehouse.connection.execute(
            """SELECT internal_entity_id FROM source_entity_map
               WHERE source_code='api_football' AND entity_type='player'
                 AND source_entity_id=?""",
            [api_player_identity_key(519924, "Mats Spiten")],
        ).fetchone()[0]
        lineup_player = self.warehouse.connection.execute(
            """SELECT lp.player_id FROM lineup_snapshot ls
               JOIN lineup_player lp USING (lineup_snapshot_id)
               WHERE lp.formation_grid='1:1'
                 AND ls.team_id=(SELECT internal_entity_id FROM source_entity_map
                     WHERE source_code='api_football' AND entity_type='team'
                       AND source_entity_id='10')"""
        ).fetchone()[0]
        event_assist = self.warehouse.connection.execute(
            "SELECT secondary_player_id FROM match_event WHERE source_code='api_football'"
        ).fetchone()[0]
        self.assertEqual(stat_player, lineup_player)
        self.assertEqual(stat_player, event_assist)

    def test_response_identity_mismatch_is_rejected_before_import(self):
        match = detailed_match()
        match["teams"]["home"]["id"] = 999
        fake = FakeHttp({"errors": [], "response": [match]})
        with self.assertRaisesRegex(BackfillValidationError, "home_team"):
            self.executor(fake).run(maximum_batches=1, execute=True)
        self.assertEqual(0, self.warehouse.connection.execute("SELECT count(*) FROM fixture").fetchone()[0])

    def test_relational_validation_failure_rolls_back_import(self):
        fake = FakeHttp({"errors": [], "response": [detailed_match()]})
        executor = self.executor(fake)
        executor._validate_loaded_batch = lambda *args: (_ for _ in ()).throw(
            BackfillValidationError("forced post-load failure")
        )
        with self.assertRaisesRegex(BackfillValidationError, "forced post-load failure"):
            executor.run(maximum_batches=1, execute=True)
        self.assertEqual(0, self.warehouse.connection.execute("SELECT count(*) FROM fixture").fetchone()[0])
        self.assertEqual(0, self.warehouse.connection.execute(
            "SELECT count(*) FROM player_match_stat_observation"
        ).fetchone()[0])
        self.assertEqual(1, self.warehouse.connection.execute("SELECT count(*) FROM raw_artifact").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
