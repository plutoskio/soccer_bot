from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_env
from soccer_bot.collector import Collector
from soccer_bot.collection_planner import validate_collector_config
from soccer_bot.database import Warehouse, stable_id
from soccer_bot.http import HttpClient, HttpResponse
from soccer_bot.raw_store import RawArtifactStore


class ConfigTests(unittest.TestCase):
    def test_load_env_handles_comments_and_quoted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# secret\nAPI_FOOTBALL_KEY='abc123'\n", encoding="utf-8")
            self.assertEqual(load_env(path)["API_FOOTBALL_KEY"], "abc123")


class RawArtifactStoreTests(unittest.TestCase):
    def test_stores_compressed_body_and_redacts_unlisted_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RawArtifactStore(root)
            response = HttpResponse(
                url="https://example.test/fixtures?date=2026-07-02",
                status=200,
                headers={"content-type": "application/json", "authorization": "secret"},
                body=b'{"response": []}',
            )
            artifact = store.store(
                source="test",
                resource="fixtures",
                response=response,
                request_params={"date": "2026-07-02"},
            )
            with gzip.open(artifact.data_path, "rb") as handle:
                self.assertEqual(handle.read(), response.body)
            metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
            self.assertNotIn("authorization", metadata["response_headers"])
            self.assertEqual(metadata["http_status"], 200)

    def test_identical_bodies_are_physically_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RawArtifactStore(Path(directory))
            response = HttpResponse("https://example.test", 200, {}, b"{}")
            first = store.store(source="test", resource="one", response=response)
            second = store.store(source="test", resource="one", response=response)
            self.assertFalse(first.duplicate)
            self.assertTrue(second.duplicate)
            self.assertEqual(first.data_path, second.data_path)


class WarehouseTests(unittest.TestCase):
    def test_stable_ids_are_deterministic(self):
        self.assertEqual(stable_id("team", "Spain"), stable_id("team", "Spain"))
        self.assertNotEqual(stable_id("team", "Spain"), stable_id("team", "Austria"))

    def test_configured_team_aliases_share_an_internal_id(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Warehouse(
                Path(directory) / "test.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                abbreviated = warehouse.resolve_team(
                    "football_data_uk", "Man City", "Man City", team_type="club"
                )
                full = warehouse.resolve_team(
                    "understat", "88", "Manchester City", team_type="club"
                )
                self.assertEqual(abbreviated, full)
            finally:
                warehouse.close()

    def test_same_player_display_name_does_not_merge_provider_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Warehouse(
                Path(directory) / "test.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                first = warehouse.resolve_player("api_football", 101, "M. Sylla")
                second = warehouse.resolve_player("api_football", 202, "M. Sylla")
                repeated = warehouse.resolve_player("api_football", 101, "Mamadou Sylla")
                self.assertNotEqual(first, second)
                self.assertEqual(first, repeated)
            finally:
                warehouse.close()

    def test_collector_dry_run_is_read_only_and_needs_no_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "test.duckdb"
            writable = Warehouse(
                path, ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
            )
            writable.migrate()
            writable.register_sources()
            writable.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()

            warehouse = Warehouse(
                path,
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
                read_only=True,
            )
            try:
                self.assertEqual([], warehouse.pending_migrations())
                with self.assertRaises(RuntimeError):
                    warehouse.migrate()
                config = json.loads(
                    (ROOT / "config" / "collector.json").read_text(encoding="utf-8")
                )
                collector = Collector(
                    warehouse=warehouse,
                    raw_store=RawArtifactStore(root / "raw"),
                    http_client=HttpClient(),
                    api_key="",
                    config=config,
                )
                summary = collector.run(
                    now=datetime(2026, 7, 10, 10, tzinfo=timezone.utc),
                    dry_run=True,
                )
                self.assertGreater(len(summary["planned_jobs"]), 0)
                self.assertFalse((root / "raw").exists())
            finally:
                warehouse.close()
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_invalid_collector_config_is_rejected_before_use(self):
        config = json.loads(
            (ROOT / "config" / "collector.json").read_text(encoding="utf-8")
        )
        config["api_football"]["fixture_batch_size"] = 21
        with self.assertRaisesRegex(ValueError, "must not exceed 20"):
            validate_collector_config(config)


if __name__ == "__main__":
    unittest.main()
