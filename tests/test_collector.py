from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collector import (
    Collector,
    DetailJob,
    FixtureRecord,
    chunks,
    lineup_stage,
    postmatch_stage,
)
from soccer_bot.database import Warehouse
from soccer_bot.http import HttpClient, HttpResponse
from soccer_bot.loaders import RawCatalog, WarehouseLoader
from soccer_bot.raw_store import RawArtifactStore


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc)

    def test_lineup_is_requested_once_at_50_then_conditionally_at_35(self):
        common = {
            "kickoff": self.kickoff,
            "lineup_complete": False,
            "first_check_minutes": 50,
            "retry_minutes": 35,
        }
        self.assertIsNone(lineup_stage(
            now=self.kickoff - timedelta(minutes=51),
            primary_attempted=False, retry_attempted=False, **common,
        ))
        self.assertEqual("lineup_primary", lineup_stage(
            now=self.kickoff - timedelta(minutes=50),
            primary_attempted=False, retry_attempted=False, **common,
        ))
        self.assertIsNone(lineup_stage(
            now=self.kickoff - timedelta(minutes=40),
            primary_attempted=True, retry_attempted=False, **common,
        ))
        self.assertEqual("lineup_retry", lineup_stage(
            now=self.kickoff - timedelta(minutes=35),
            primary_attempted=True, retry_attempted=False, **common,
        ))
        self.assertIsNone(lineup_stage(
            now=self.kickoff - timedelta(minutes=20),
            lineup_complete=True, primary_attempted=True, retry_attempted=False,
            kickoff=self.kickoff, first_check_minutes=50, retry_minutes=35,
        ))

    def test_postmatch_has_one_late_retry(self):
        common = {
            "kickoff": self.kickoff,
            "data_complete": False,
            "first_check_minutes": 150,
            "retry_minutes": 1590,
        }
        self.assertIsNone(postmatch_stage(
            now=self.kickoff + timedelta(minutes=149),
            primary_attempted=False, retry_attempted=False, **common,
        ))
        self.assertEqual("postmatch_primary", postmatch_stage(
            now=self.kickoff + timedelta(minutes=150),
            primary_attempted=False, retry_attempted=False, **common,
        ))
        self.assertEqual("postmatch_retry", postmatch_stage(
            now=self.kickoff + timedelta(minutes=1590),
            primary_attempted=True, retry_attempted=False, **common,
        ))

    def test_batches_never_exceed_twenty(self):
        batches = list(chunks(list(range(45)), 20))
        self.assertEqual([20, 20, 5], [len(batch) for batch in batches])


class CollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.warehouse = Warehouse(
            self.root / "test.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
        )
        self.warehouse.migrate()
        self.warehouse.register_sources()
        self.config = json.loads((ROOT / "config" / "collector.json").read_text())

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    def test_competition_filter_does_not_accept_unrelated_premier_leagues(self):
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(), api_key="test", config=self.config,
        )
        england = {"league": {"id": 39, "country": "England", "name": "Premier League"}}
        ethiopia = {"league": {"id": 363, "country": "Ethiopia", "name": "Premier League"}}
        self.assertTrue(collector._is_monitored_match(england))
        self.assertFalse(collector._is_monitored_match(ethiopia))

    def test_fixture_planning_keeps_previous_day_for_postmatch_collection(self):
        competition_id = self.warehouse.resolve_competition(
            "api_football", 39, "Premier League", country_code="England"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "39|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team("api_football", 1, "Alpha", team_type="club")
        away_id = self.warehouse.resolve_team("api_football", 2, "Beta", team_type="club")
        self.warehouse.resolve_fixture(
            "api_football", 777, home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=datetime(2026, 7, 2, 20, 30, tzinfo=timezone.utc),
            competition_id=competition_id, season_id=season_id, status="completed",
        )
        collector = Collector(
            warehouse=self.warehouse, raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(), api_key="test", config=self.config,
        )
        current_date = datetime(2026, 7, 3, tzinfo=timezone.utc).astimezone(collector.zone).date()
        self.assertEqual([], collector._monitored_fixtures(current_date, lookback_days=0))
        self.assertEqual(1, len(collector._monitored_fixtures(current_date, lookback_days=2)))

    def test_live_cycle_uses_paid_ids_batch_and_second_run_is_idempotent(self):
        self.config["api_football"]["minimum_interval_seconds"] = 0
        self.config["discovery"]["recovery_days"] = 0
        self.config["discovery"]["planning_days"] = 0
        now = datetime(2026, 7, 3, 20, 0, tzinfo=timezone.utc)
        base_match = {
            "fixture": {
                "id": 777, "date": "2026-07-03T17:00:00+00:00",
                "status": {"short": "FT"}, "venue": {"name": "Ground"},
            },
            "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
            "teams": {
                "home": {"id": 1, "name": "Alpha FC"},
                "away": {"id": 2, "name": "Beta FC"},
            },
            "score": {"fulltime": {"home": 2, "away": 1}, "halftime": {"home": 1, "away": 0}},
        }
        detail_match = dict(base_match)
        detail_match.update({
            "lineups": [], "events": [],
            "players": [
                {"team": {"id": 1, "name": "Alpha FC"}, "players": [{
                    "player": {"id": 11, "name": "Alpha Player"},
                    "statistics": [{"games": {"minutes": 90, "substitute": False}, "goals": {"total": 1}}],
                }]},
                {"team": {"id": 2, "name": "Beta FC"}, "players": [{
                    "player": {"id": 22, "name": "Beta Player"},
                    "statistics": [{"games": {"minutes": 90, "substitute": False}, "goals": {"total": 0}}],
                }]},
            ],
            "statistics": [
                {"team": {"id": 1, "name": "Alpha FC"}, "statistics": [{"type": "Total Shots", "value": 10}]},
                {"team": {"id": 2, "name": "Beta FC"}, "statistics": [{"type": "Total Shots", "value": 8}]},
            ],
        })

        class FakeHttp:
            def __init__(self):
                self.calls = []

            def get(self, base_url, path, *, params=None, headers=None, timeout=30.0):
                self.calls.append((base_url, path, params))
                if "gamma-api" in base_url:
                    body = {"events": [], "next_cursor": None}
                elif params and "date" in params:
                    body = {"response": [base_match], "errors": []}
                elif params and params.get("ids") == "777":
                    body = {"response": [detail_match], "errors": []}
                else:
                    raise AssertionError(f"Unexpected request: {base_url} {path} {params}")
                return HttpResponse(
                    f"{base_url}{path}", 200, {"content-type": "application/json"},
                    json.dumps(body).encode(),
                )

            def post_json(self, *args, **kwargs):
                raise AssertionError("No order books should be requested without linked markets")

        fake = FakeHttp()
        collector = Collector(
            warehouse=self.warehouse, raw_store=RawArtifactStore(self.root / "raw"),
            http_client=fake, api_key="test", config=self.config,
        )
        first = collector.run(now=now)
        self.assertEqual(2, first["api_football_calls"])
        detail_calls = [params for base, path, params in fake.calls if params and ("id" in params or "ids" in params)]
        self.assertEqual([{"ids": "777"}], detail_calls)
        attempts = self.warehouse.connection.execute(
            """
            SELECT source_code, job_type, status
            FROM collection_attempt
            ORDER BY source_code, job_type
            """
        ).fetchall()
        self.assertEqual(
            [
                ("api_football", "fixture_discovery", "succeeded"),
                ("api_football", "postmatch_primary", "incomplete"),
                ("polymarket_gamma", "event_discovery", "succeeded"),
            ],
            attempts,
        )
        checkpoint = self.warehouse.connection.execute(
            """
            SELECT fixture_id, component_code, status, completed_at,
                   next_attempt_at, last_run_id
            FROM collection_checkpoint
            WHERE job_type='postmatch_primary'
            """
        ).fetchone()
        self.assertEqual("incomplete", checkpoint[2])
        self.assertIsNone(checkpoint[3])
        self.assertIsNotNone(checkpoint[4])
        self.assertIsNotNone(checkpoint[5])

        self.warehouse.connection.execute(
            """
            UPDATE collection_checkpoint
            SET status='succeeded', completed_at=?
            WHERE job_type='postmatch_primary'
            """,
            [now],
        )
        collector._reconcile_fixture_components(
            collector._monitored_fixtures(now.date(), lookback_days=2), now
        )
        reopened = self.warehouse.connection.execute(
            """
            SELECT status, completed_at, metadata
            FROM collection_checkpoint
            WHERE job_type='postmatch_primary'
            """
        ).fetchone()
        self.assertEqual("incomplete", reopened[0])
        self.assertIsNone(reopened[1])
        self.assertTrue(json.loads(reopened[2])["checkpoint_fact_mismatch"])

        fake.calls.clear()
        second = Collector(
            warehouse=self.warehouse, raw_store=RawArtifactStore(self.root / "raw"),
            http_client=fake, api_key="test", config=self.config,
        ).run(now=now)
        self.assertEqual(0, second["api_football_calls"])
        self.assertEqual([], fake.calls)

    def test_one_embedded_response_loads_all_match_detail_tables(self):
        loader = WarehouseLoader(self.warehouse, RawCatalog.__new__(RawCatalog))
        starters_home = [
            {"player": {"id": 1000 + index, "name": f"Home {index}", "pos": "F", "number": index}}
            for index in range(1, 12)
        ]
        starters_away = [
            {"player": {"id": 2000 + index, "name": f"Away {index}", "pos": "F", "number": index}}
            for index in range(1, 12)
        ]
        player_stat = lambda player_id, name: {
            "player": {"id": player_id, "name": name},
            "statistics": [{
                "games": {"minutes": 90, "number": 9, "position": "Attacker", "rating": "7.1", "captain": False, "substitute": False},
                "goals": {"total": 1, "assists": 0, "conceded": 0, "saves": None},
                "shots": {"total": 3, "on": 2},
                "passes": {"key": 1, "total": 20, "accuracy": "16"},
                "tackles": {"total": 2, "blocks": 1, "interceptions": 1},
                "duels": {"total": 5, "won": 3},
                "dribbles": {"attempts": 2, "success": 1, "past": 1},
                "fouls": {"drawn": 1, "committed": 2},
                "cards": {"yellow": 0, "yellowred": 0, "red": 0},
                "penalty": {"won": 1, "commited": 0, "scored": 0, "missed": 0, "saved": 0},
            }],
        }
        team_stats = lambda team_id, name: {
            "team": {"id": team_id, "name": name},
            "statistics": [
                {"type": "Total Shots", "value": 10},
                {"type": "Shots on Goal", "value": 4},
                {"type": "Corner Kicks", "value": 5},
            ],
        }
        payload = {"response": [{
            "fixture": {
                "id": 9001, "date": "2026-07-03T18:00:00+00:00",
                "status": {"short": "FT"}, "venue": {"name": "Test Ground"},
            },
            "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
            "teams": {
                "home": {"id": 10, "name": "Home FC"},
                "away": {"id": 20, "name": "Away FC"},
            },
            "score": {"fulltime": {"home": 1, "away": 0}, "halftime": {"home": 0, "away": 0}},
            "lineups": [
                {"team": {"id": 10, "name": "Home FC"}, "formation": "4-3-3", "startXI": starters_home, "substitutes": []},
                {"team": {"id": 20, "name": "Away FC"}, "formation": "4-4-2", "startXI": starters_away, "substitutes": []},
            ],
            "events": [{
                "time": {"elapsed": 70}, "team": {"id": 10, "name": "Home FC"},
                "player": {"id": 1001, "name": "Home 1"}, "assist": {"id": None},
                "type": "Goal", "detail": "Normal Goal",
            }, {
                "time": {"elapsed": 75}, "team": {"id": 20, "name": "Away FC"},
                "player": {"id": 2001, "name": "Away 1"}, "assist": {"id": None},
                "type": "Card", "detail": "Yellow Card",
            }],
            "players": [
                {"team": {"id": 10, "name": "Home FC"}, "players": [player_stat(1001, "Home 1")]},
                {"team": {"id": 20, "name": "Away FC"}, "players": [player_stat(2001, "Away 1")]},
            ],
            "statistics": [team_stats(10, "Home FC"), team_stats(20, "Away FC")],
        }]}
        item = {
            "retrieved_at": "2026-07-03T20:30:00+00:00",
            "content_sha256": "test-content",
            "_raw_artifact_id": "test-artifact",
            "request_parameters": {"ids": "9001"},
        }
        loader.load_api_football_payload(payload, item, "fixture_details_batch")
        connection = self.warehouse.connection
        self.assertEqual(1, connection.execute("SELECT count(*) FROM fixture").fetchone()[0])
        self.assertEqual(2, connection.execute("SELECT count(*) FROM lineup_snapshot").fetchone()[0])
        self.assertEqual(22, connection.execute("SELECT count(*) FROM lineup_player").fetchone()[0])
        self.assertEqual(2, connection.execute("SELECT count(*) FROM player_match_stat_observation").fetchone()[0])
        expanded = connection.execute(
            """
            SELECT passes, accurate_passes, pass_accuracy_pct, rating, tackles,
                   interceptions, duels, duels_won, dribbles_attempted,
                   dribbles_successful, fouls_drawn, fouls_committed, penalties_won
            FROM player_match_stat_observation ORDER BY player_id LIMIT 1
            """
        ).fetchone()
        self.assertEqual((20, 16, 80.0, 7.1, 2, 1, 5, 3, 2, 1, 1, 2, 1), expanded)
        self.assertEqual(2, connection.execute("SELECT count(*) FROM team_match_stat_observation").fetchone()[0])
        self.assertEqual(2, connection.execute("SELECT count(*) FROM match_event").fetchone()[0])
        reordered = json.loads(json.dumps(payload))
        reordered["response"][0]["events"].reverse()
        reordered_item = dict(item, content_sha256="corrected-content")
        loader.load_api_football_payload(reordered, reordered_item, "fixture_details_batch")
        self.assertEqual(2, connection.execute("SELECT count(*) FROM match_event").fetchone()[0])
        team_types = connection.execute("SELECT DISTINCT team_type FROM team").fetchall()
        self.assertEqual([("club",)], team_types)

    def test_polymarket_jobs_share_one_batched_orderbook_request(self):
        collector = Collector(
            warehouse=self.warehouse, raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(), api_key="test", config=self.config,
        )
        kickoff = datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc)
        first = FixtureRecord("fixture-1", "101", kickoff, "scheduled", "Alpha", "Beta")
        second = FixtureRecord("fixture-2", "202", kickoff, "scheduled", "Gamma", "Delta")
        jobs = [
            DetailJob("market:a", "lineup_snapshot", first, kickoff),
            DetailJob("market:b", "prekick_snapshot", first, kickoff),
            DetailJob("market:c", "lineup_snapshot", second, kickoff),
        ]
        tokens = {"fixture-1": ["a", "b"], "fixture-2": ["c"]}
        calls = []
        collector._market_tokens = lambda fixture_id: tokens[fixture_id]

        def fake_post(resource, path, payload):
            calls.append(payload)
            return ([{"asset_id": item["token_id"]} for item in payload], {}, 200)

        collector._polymarket_post = fake_post
        collector.loader = type("NoOpLoader", (), {"load_polymarket_payload": lambda *args: None})()
        collector._execute_market_jobs(jobs, kickoff)
        self.assertEqual(1, len(calls))
        self.assertEqual({"a", "b", "c"}, {item["token_id"] for item in calls[0]})
        self.assertEqual(3, self.warehouse.connection.execute(
            "SELECT count(*) FROM collection_checkpoint WHERE status = 'succeeded'"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
