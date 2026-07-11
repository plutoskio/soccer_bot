from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse  # noqa: E402
from soccer_bot.health import generate_health_report  # noqa: E402


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.warehouse = Warehouse(
            self.root / "warehouse.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.warehouse.register_sources()
        self.config = json.loads((ROOT / "config" / "collector.json").read_text())
        self.now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
        self.warehouse.connection.execute(
            """
            INSERT INTO collection_run (
                collection_run_id,started_at,status,dry_run
            ) VALUES ('run',?,'completed',false)
            """,
            [self.now],
        )

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    def test_warning_report_is_persisted_and_contains_no_secrets(self):
        report = generate_health_report(
            self.warehouse.connection,
            config=self.config,
            collection_run_id="run",
            now=self.now,
            report_directory=self.root / "reports",
        )
        self.assertEqual("warning", report.severity)
        text = report.markdown_path.read_text()
        self.assertNotIn("API_FOOTBALL_KEY", text)
        self.assertNotIn("x-apisports-key", text)
        stored = self.warehouse.connection.execute(
            "SELECT severity,collection_run_id FROM collection_health_report"
        ).fetchone()
        self.assertEqual(("warning", "run"), stored)

    def test_invalid_required_component_is_blocking(self):
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_collection_component (
                fixture_id,source_code,component_code,state,
                required_for_fixture_terminal
            ) VALUES ('fixture','api_football','result','invalid',true)
            """
        )
        report = generate_health_report(
            self.warehouse.connection,
            config=self.config,
            collection_run_id="run",
            now=self.now,
        )
        self.assertEqual("blocking", report.severity)
        self.assertEqual("invalid_required_components", report.blocking_reason)


if __name__ == "__main__":
    unittest.main()
