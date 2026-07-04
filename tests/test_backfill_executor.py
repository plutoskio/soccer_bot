from __future__ import annotations

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
                "minimum_passing_coverage": 0.8,
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
        fake = FakeHttp({"errors": [], "response": [match]})
        first = self.executor(fake).run(maximum_batches=1, execute=True)
        self.assertEqual(1, first["api_calls"])
        self.assertEqual(1, first["completed_batches"])
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
