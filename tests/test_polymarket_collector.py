from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collection_planner import market_stage_plans  # noqa: E402
from soccer_bot.database import Warehouse  # noqa: E402
from soccer_bot.loaders import RawCatalog, WarehouseLoader  # noqa: E402


class MarketStagePlannerTests(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)

    def test_all_timed_stages_have_strict_local_windows(self):
        offsets = [1440, 360, 90, 15, 5]
        for offset in offsets:
            plans = market_stage_plans(
                fixture_source_id="1",
                schedule_version="kickoff-1",
                kickoff=self.kickoff,
                now=self.kickoff - timedelta(minutes=offset) + timedelta(minutes=4),
                offsets_minutes=offsets,
                stage_window_minutes=5,
                lineup_complete=False,
                attempted_job_keys=set(),
            )
            self.assertEqual([f"market_t_minus_{offset}"], [p.stage for p in plans])
        missed = market_stage_plans(
            fixture_source_id="1",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff - timedelta(minutes=1430),
            offsets_minutes=offsets,
            stage_window_minutes=5,
            lineup_complete=False,
            attempted_job_keys=set(),
        )
        self.assertNotIn("market_t_minus_1440", [p.stage for p in missed])

    def test_lineup_and_closed_stages_are_distinct(self):
        lineup = market_stage_plans(
            fixture_source_id="1", schedule_version="kickoff-1",
            kickoff=self.kickoff, now=self.kickoff - timedelta(minutes=20),
            offsets_minutes=[15, 5], stage_window_minutes=10,
            lineup_complete=True, attempted_job_keys=set(),
        )
        self.assertEqual(["market_after_lineup"], [p.stage for p in lineup])
        closed = market_stage_plans(
            fixture_source_id="1", schedule_version="kickoff-1",
            kickoff=self.kickoff, now=self.kickoff + timedelta(minutes=180),
            offsets_minutes=[15, 5], stage_window_minutes=10,
            lineup_complete=False, attempted_job_keys=set(),
        )
        self.assertTrue(closed[0].include_closed)
        self.assertEqual("market_after_closure", closed[0].stage)


class MarketObservationLoaderTests(unittest.TestCase):
    def test_metadata_history_and_cadence_are_append_only(self):
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Warehouse(
                Path(directory) / "warehouse.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
                event = {
                    "id": "event-1", "title": "Alpha vs Beta",
                    "active": True, "closed": False,
                    "startTime": "2026-07-12T18:00:00Z",
                    "markets": [{
                        "id": "market-1", "question": "Winner?",
                        "active": True, "closed": False,
                        "outcomes": json.dumps(["Alpha", "Beta"]),
                        "outcomePrices": json.dumps(["0.5", "0.5"]),
                        "clobTokenIds": json.dumps(["token-a", "token-b"]),
                    }],
                }
                for suffix, retrieved in (
                    ("one", "2026-07-11T18:00:00Z"),
                    ("two", "2026-07-11T19:00:00Z"),
                ):
                    loader.load_polymarket_payload(
                        "soccer_events", {"events": [event]},
                        {
                            "retrieved_at": retrieved,
                            "content_sha256": f"content-{suffix}",
                            "_raw_artifact_id": f"artifact-{suffix}",
                        },
                    )
                self.assertEqual(
                    (2, 2),
                    warehouse.connection.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM prediction_market_event_observation),
                          (SELECT count(*) FROM prediction_market_observation)
                        """
                    ).fetchone(),
                )
                loader.load_polymarket_payload(
                    "order_books_batch",
                    [{"asset_id": "token-a", "bids": [], "asks": []}],
                    {
                        "retrieved_at": "2026-07-12T17:55:00Z",
                        "content_sha256": "book-content",
                        "_raw_artifact_id": "book-artifact",
                        "request_parameters": {},
                        "_cadence_stage_by_token": {"token-a": "market_t_minus_5"},
                        "_kickoff_by_token": {"token-a": "2026-07-12T18:00:00Z"},
                    },
                )
                self.assertEqual(
                    ("market_t_minus_5", datetime(2026, 7, 12, 18, tzinfo=timezone.utc)),
                    warehouse.connection.execute(
                        """
                        SELECT cadence_stage,kickoff_known_at_retrieval
                        FROM orderbook_snapshot
                        """
                    ).fetchone(),
                )
            finally:
                warehouse.close()


if __name__ == "__main__":
    unittest.main()
