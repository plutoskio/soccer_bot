from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collector import Collector, FixtureRecord  # noqa: E402
from soccer_bot.collection_planner import market_stage_plans  # noqa: E402
from soccer_bot.database import Warehouse  # noqa: E402
from soccer_bot.http import HttpClient  # noqa: E402
from soccer_bot.loaders import RawCatalog, WarehouseLoader  # noqa: E402
from soccer_bot.raw_store import RawArtifactStore  # noqa: E402


class MarketStagePlannerTests(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2026, 7, 12, 18, tzinfo=timezone.utc)

    def test_all_timed_stages_have_strict_local_windows(self):
        offsets = [4320, 1440, 360, 90, 15, 5]
        for offset in offsets:
            plans = market_stage_plans(
                fixture_source_id="1",
                schedule_version="kickoff-1",
                kickoff=self.kickoff,
                now=self.kickoff - timedelta(minutes=offset) - timedelta(minutes=4),
                offsets_minutes=offsets,
                stage_window_minutes=5,
                lineup_complete=False,
                attempted_job_keys=set(),
            )
            self.assertEqual([f"market_t_minus_{offset}"], [p.stage for p in plans])
            self.assertEqual(
                self.kickoff - timedelta(minutes=offset),
                plans[0].capture_target_at,
            )
            self.assertLess(plans[0].stage_time, plans[0].capture_target_at)
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

    def test_exact_prediction_cutoff_is_excluded(self):
        plans = market_stage_plans(
            fixture_source_id="1",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff - timedelta(minutes=1440),
            offsets_minutes=[4320, 1440],
            stage_window_minutes=16,
            lineup_complete=False,
            attempted_job_keys=set(),
        )
        self.assertNotIn("market_t_minus_1440", [plan.stage for plan in plans])

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
                    "startDate": "2026-07-07T18:00:00Z",
                    "endDate": "2026-07-12T18:00:00Z",
                    "markets": [{
                        "id": "market-1", "question": "Winner?",
                        "gameStartTime": "2026-07-12T18:00:00Z",
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
                self.assertEqual(
                    (
                        datetime(2026, 7, 7, 18, tzinfo=timezone.utc),
                        datetime(2026, 7, 12, 18, tzinfo=timezone.utc),
                    ),
                    warehouse.connection.execute(
                        """
                        SELECT start_time,end_time
                        FROM prediction_market_event
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
                        "_capture_by_token": {
                            "token-a": {
                                "target_at": "2026-07-12T17:55:00Z",
                                "window_start_at": "2026-07-12T17:39:00Z",
                                "deadline_at": "2026-07-12T17:55:00Z",
                                "timing_valid": True,
                            }
                        },
                    },
                )
                self.assertEqual(
                    (
                        "market_t_minus_5",
                        datetime(2026, 7, 12, 18, tzinfo=timezone.utc),
                        datetime(2026, 7, 12, 17, 55, tzinfo=timezone.utc),
                        datetime(2026, 7, 12, 17, 39, tzinfo=timezone.utc),
                        datetime(2026, 7, 12, 17, 55, tzinfo=timezone.utc),
                        True,
                    ),
                    warehouse.connection.execute(
                        """
                        SELECT cadence_stage,kickoff_known_at_retrieval,
                               capture_target_at,capture_window_start_at,
                               capture_deadline_at,capture_timing_valid
                        FROM orderbook_snapshot
                        """
                    ).fetchone(),
                )
            finally:
                warehouse.close()

    def test_linker_uses_game_start_and_controlled_team_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            warehouse = Warehouse(
                root / "warehouse.duckdb",
                ROOT / "migrations",
                ROOT / "config" / "entity_aliases.json",
            )
            try:
                warehouse.migrate()
                warehouse.register_sources()
                loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
                loader.load_polymarket_payload(
                    "soccer_events",
                    {
                        "events": [{
                            "id": "thun-dinamo",
                            "title": "FC Thun vs GNK Dinamo Zagreb",
                            "startDate": "2026-07-16T18:16:09Z",
                            "endDate": "2026-07-21T18:00:00Z",
                            "active": True,
                            "closed": False,
                            "markets": [{
                                "id": "thun-home",
                                "gameStartTime": "2026-07-21T18:00:00Z",
                                "outcomes": json.dumps(["Yes", "No"]),
                                "clobTokenIds": json.dumps(["yes", "no"]),
                            }],
                        }]
                    },
                    {
                        "retrieved_at": "2026-07-20T21:00:00Z",
                        "content_sha256": "event-content",
                        "_raw_artifact_id": "event-artifact",
                    },
                )
                config = json.loads((ROOT / "config" / "collector.json").read_text())
                collector = Collector(
                    warehouse=warehouse,
                    raw_store=RawArtifactStore(root / "raw"),
                    http_client=HttpClient(),
                    api_key="test",
                    config=config,
                )
                fixture = FixtureRecord(
                    "fixture-thun",
                    "123",
                    datetime(2026, 7, 21, 18, tzinfo=timezone.utc),
                    "scheduled",
                    "FC Thun",
                    "Dinamo Zagreb",
                )

                self.assertEqual(1, collector._link_polymarket_events([fixture]))
                self.assertEqual(
                    ("fixture-thun", "team_names_and_kickoff"),
                    warehouse.connection.execute(
                        """
                        SELECT fixture_id,fixture_link_method
                        FROM prediction_market_event
                        """
                    ).fetchone(),
                )
                collector._lineup_complete = lambda *args, **kwargs: False
                jobs = collector._plan_market_jobs(
                    [fixture],
                    datetime(2026, 7, 20, 21, 3, tzinfo=timezone.utc),
                )
                live = [job for job in jobs if job.job_type == "market_live"]
                self.assertEqual(1, len(live))
                self.assertEqual(
                    datetime(2026, 7, 20, 21, 0, tzinfo=timezone.utc),
                    live[0].scheduled_for,
                )
            finally:
                warehouse.close()


if __name__ == "__main__":
    unittest.main()
