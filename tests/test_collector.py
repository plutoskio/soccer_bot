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
from soccer_bot.collection_planner import postmatch_stage_plans
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

    def test_status_aware_postmatch_slots_and_corrections(self):
        plans = postmatch_stage_plans(
            fixture_source_id="100",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff + timedelta(hours=25),
            canonical_status="final",
            components_complete=False,
            attempted_job_keys=set(),
        )
        self.assertEqual(
            ["correction_refresh_24h"],
            [plan.stage for plan in plans],
        )
        attempted = {plan.job_key for plan in plans}
        later = postmatch_stage_plans(
            fixture_source_id="100",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff + timedelta(hours=73),
            canonical_status="final",
            components_complete=True,
            attempted_job_keys=attempted,
        )
        self.assertEqual(["correction_refresh_72h"], [plan.stage for plan in later])
        outage = postmatch_stage_plans(
            fixture_source_id="200",
            schedule_version="kickoff-2",
            kickoff=self.kickoff,
            now=self.kickoff + timedelta(hours=73),
            canonical_status="final",
            components_complete=True,
            attempted_job_keys=set(),
        )
        self.assertEqual(["correction_refresh_72h"], [plan.stage for plan in outage])
        self.assertEqual(
            [],
            postmatch_stage_plans(
                fixture_source_id="200",
                schedule_version="kickoff-2",
                kickoff=self.kickoff,
                now=self.kickoff + timedelta(hours=74),
                canonical_status="final",
                components_complete=True,
                attempted_job_keys={outage[0].job_key},
            ),
        )

    def test_live_status_polls_stop_at_six_hours(self):
        plans = postmatch_stage_plans(
            fixture_source_id="100",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff + timedelta(minutes=361),
            canonical_status="live",
            components_complete=False,
            attempted_job_keys=set(),
        )
        self.assertEqual(
            [360],
            [plan.offset_minutes for plan in plans],
        )
        terminal = postmatch_stage_plans(
            fixture_source_id="100",
            schedule_version="kickoff-1",
            kickoff=self.kickoff,
            now=self.kickoff + timedelta(days=4),
            canonical_status="postponed",
            components_complete=False,
            attempted_job_keys=set(),
        )
        self.assertEqual([], terminal)

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

    def test_polymarket_discovery_uses_match_date_and_targeted_fallback(self):
        now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        kickoff = datetime(2026, 7, 21, 18, tzinfo=timezone.utc)
        competition_id = self.warehouse.resolve_competition(
            "api_football", 39, "Premier League", country_code="England"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "39|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team(
            "api_football", 101, "Alpha FC", team_type="club"
        )
        away_id = self.warehouse.resolve_team(
            "api_football", 102, "Beta FC", team_type="club"
        )
        fixture_id = self.warehouse.resolve_fixture(
            "api_football", 909,
            home_team_id=home_id,
            away_team_id=away_id,
            scheduled_kickoff=kickoff,
            competition_id=competition_id,
            season_id=season_id,
            status="scheduled",
        )

        class FakePolymarketHttp:
            def __init__(self):
                self.calls = []

            def get(self, base_url, path, *, params=None, headers=None, timeout=30):
                self.calls.append((path, params))
                if path == "/events/keyset":
                    self.assert_discovery_params(params)
                    body = {"events": [], "next_cursor": None}
                elif path == "/public-search":
                    body = {
                        "events": [{
                            "id": "alpha-beta",
                            "title": "Alpha FC vs. Beta FC",
                            "startDate": "2026-07-10T10:00:00Z",
                            "endDate": kickoff.isoformat(),
                            "active": True,
                            "closed": False,
                            "markets": [{
                                "id": "alpha-beta-winner",
                                "question": "Alpha FC vs. Beta FC winner?",
                                "gameStartTime": kickoff.isoformat(),
                                "active": True,
                                "closed": False,
                                "outcomes": json.dumps(["Alpha FC", "Beta FC"]),
                                "clobTokenIds": json.dumps(["alpha-token", "beta-token"]),
                            }],
                        }]
                    }
                else:
                    raise AssertionError(f"Unexpected Polymarket path: {path}")
                return HttpResponse(
                    f"{base_url}{path}", 200,
                    {"content-type": "application/json"},
                    json.dumps(body).encode(),
                )

            @staticmethod
            def assert_discovery_params(params):
                if "start_time_min" in params or "start_time_max" in params:
                    raise AssertionError("Market creation time must not drive discovery")
                if not params.get("end_date_min") or not params.get("end_date_max"):
                    raise AssertionError("Match-date discovery bounds are required")

        fake = FakePolymarketHttp()
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=fake,
            api_key="test",
            config=self.config,
        )
        collector._discover_polymarket(
            now.astimezone(collector.zone).date(),
            [FixtureRecord(
                fixture_id, "909", kickoff, "scheduled", "Alpha FC", "Beta FC"
            )],
            now,
            False,
        )
        self.assertEqual(
            ["alpha-token", "beta-token"], collector._market_tokens(fixture_id)
        )
        self.assertEqual(
            ["/events/keyset", "/public-search"],
            [row[0] for row in fake.calls],
        )

    def test_live_market_planning_uses_current_canonical_kickoff(self):
        now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        current_kickoff = now + timedelta(hours=24)
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(),
            api_key="test",
            config=self.config,
        )
        collector._fixture_has_any_market_tokens = lambda fixture_id: True
        collector._lineup_schedule_version = lambda fixture: (
            "kickoff-old",
            None,
            now - timedelta(days=1),
        )
        collector._lineup_complete = lambda *args, **kwargs: False
        jobs = collector._plan_market_jobs(
            [FixtureRecord(
                "fixture-live",
                "919",
                current_kickoff,
                "scheduled",
                "Alpha FC",
                "Beta FC",
            )],
            now,
        )
        live_jobs = [job for job in jobs if job.job_type == "market_live"]
        self.assertEqual(1, len(live_jobs))
        self.assertEqual(current_kickoff, live_jobs[0].fixture.kickoff)

    def test_market_scope_includes_every_upcoming_publishable_fixture(self):
        now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        competition_id = self.warehouse.resolve_competition(
            "api_football", 9999, "Inference-only Competition", country_code="Test"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "9999|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team(
            "api_football", 201, "Inference Home", team_type="club"
        )
        away_id = self.warehouse.resolve_team(
            "api_football", 202, "Inference Away", team_type="club"
        )
        fixture_id = self.warehouse.resolve_fixture(
            "api_football", 929,
            home_team_id=home_id,
            away_team_id=away_id,
            scheduled_kickoff=now + timedelta(hours=24),
            competition_id=competition_id,
            season_id=season_id,
            status="scheduled",
        )
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(),
            api_key="test",
            config=self.config,
        )
        scoped = collector._market_fixture_scope([], now)
        self.assertEqual([fixture_id], [fixture.internal_id for fixture in scoped])

    def test_rate_limit_is_persisted_and_run_completes(self):
        self.config["discovery"]["recovery_days"] = 0
        self.config["discovery"]["planning_days"] = 0
        self.config["api_football"]["minimum_interval_seconds"] = 0
        self.config["retry"]["maximum_inline_retry_seconds"] = 0
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)

        class RateLimitedHttp:
            def get(self, base_url, path, *, params=None, headers=None, timeout=30):
                return HttpResponse(
                    f"{base_url}{path}", 429,
                    {"content-type": "application/json", "retry-after": "60"},
                    b'{"errors":{"rate":"limited"}}',
                )

        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=RateLimitedHttp(), api_key="test", config=self.config,
        )
        summary = collector.run(now=now)
        self.assertIn("warnings", summary)
        checkpoint = self.warehouse.connection.execute(
            """
            SELECT status,next_attempt_at,last_http_status
            FROM collection_checkpoint WHERE job_type='fixture_discovery'
            """
        ).fetchone()
        self.assertEqual("rate_limited", checkpoint[0])
        self.assertGreater(checkpoint[1], now)
        self.assertEqual(429, checkpoint[2])
        self.assertEqual(
            (429,),
            self.warehouse.connection.execute(
                "SELECT http_status FROM raw_artifact"
            ).fetchone(),
        )

    def test_one_failed_discovery_date_does_not_stop_the_next(self):
        self.config["discovery"]["recovery_days"] = 0
        self.config["discovery"]["planning_days"] = 1
        self.config["api_football"]["minimum_interval_seconds"] = 0
        self.config["retry"]["maximum_inline_attempts"] = 1
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)

        class PartiallyFailingHttp:
            def __init__(self):
                self.calls = 0

            def get(self, base_url, path, *, params=None, headers=None, timeout=30):
                self.calls += 1
                status = 503 if self.calls == 1 else 200
                body = b'{}' if status == 503 else b'{"response":[],"errors":[]}'
                return HttpResponse(
                    f"{base_url}{path}", status,
                    {"content-type": "application/json"}, body,
                )

        fake = PartiallyFailingHttp()
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=fake, api_key="test", config=self.config,
        )
        collector.run(now=now)
        self.assertEqual(2, fake.calls)
        self.assertEqual(
            [("failed", 1), ("succeeded", 1)],
            self.warehouse.connection.execute(
                """
                SELECT status,count(*) FROM collection_checkpoint
                WHERE job_type='fixture_discovery' GROUP BY status ORDER BY status
                """
            ).fetchall(),
        )

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
                ("api_football", "http_request", "succeeded"),
                ("api_football", "http_request", "succeeded"),
                ("api_football", "postmatch_status", "incomplete"),
                ("polymarket_gamma", "event_discovery", "succeeded"),
                ("polymarket_gamma", "http_request", "succeeded"),
            ],
            attempts,
        )
        checkpoint = self.warehouse.connection.execute(
            """
            SELECT fixture_id, component_code, status, completed_at,
                   next_attempt_at, last_run_id
            FROM collection_checkpoint
            WHERE job_type='postmatch_status'
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
            WHERE job_type='postmatch_status'
            """,
            [now],
        )
        self.warehouse.connection.execute(
            """
            INSERT INTO collection_checkpoint (
                job_key, source_code, job_type, fixture_source_id,
                scheduled_for, status, attempts, completed_at, terminal_reason,
                metadata, updated_at, fixture_id, component_code
            ) VALUES ('terminal-lineup-job', 'api_football', 'lineup_stage',
                      '777', ?, 'terminal', 1, ?, 'schedule_superseded',
                      '{}', ?, ?, 'lineups')
            """,
            [now, now, now, checkpoint[0]],
        )
        collector._reconcile_fixture_components(
            collector._monitored_fixtures(now.date(), lookback_days=2), now
        )
        reopened = self.warehouse.connection.execute(
            """
            SELECT status, completed_at, metadata
            FROM collection_checkpoint
            WHERE job_type='postmatch_status'
            """
        ).fetchone()
        self.assertEqual("incomplete", reopened[0])
        self.assertIsNone(reopened[1])
        self.assertTrue(json.loads(reopened[2])["checkpoint_fact_mismatch"])
        self.assertEqual(
            ("terminal", "schedule_superseded"),
            self.warehouse.connection.execute(
                """
                SELECT status, terminal_reason FROM collection_checkpoint
                WHERE job_key='terminal-lineup-job'
                """
            ).fetchone(),
        )

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
        collector._market_tokens = lambda fixture_id, **kwargs: tokens[fixture_id]

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

    def test_completed_correction_component_closes_retryable_checkpoint(self):
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(), api_key="test", config=self.config,
        )
        now = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
        fixture = FixtureRecord(
            "fixture", "100", now - timedelta(days=3), "completed", "A", "B"
        )
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_collection_component (
                fixture_id,source_code,component_code,state,
                required_for_fixture_terminal
            ) VALUES ('fixture','api_football','correction_refresh_72h',
                      'complete',true)
            """
        )
        self.warehouse.connection.execute(
            """
            INSERT INTO collection_checkpoint (
                job_key,source_code,job_type,fixture_source_id,scheduled_for,
                status,attempts,updated_at,fixture_id,component_code,
                maximum_attempts,priority
            ) VALUES ('correction','api_football','correction_refresh_72h',
                      '100',?,'incomplete',1,?,'fixture',
                      'correction_refresh_72h',1,2)
            """,
            [now, now],
        )
        collector._reopen_checkpoint_fact_mismatches(fixture, {}, now)
        self.assertEqual(
            ("succeeded", None),
            self.warehouse.connection.execute(
                "SELECT status,next_attempt_at FROM collection_checkpoint"
            ).fetchone(),
        )

    def test_closed_book_partial_tokens_retry_then_terminalize(self):
        collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=HttpClient(), api_key="test", config=self.config,
        )
        now = datetime(2026, 7, 3, 21, tzinfo=timezone.utc)
        fixture = FixtureRecord(
            "fixture-1", "101", now - timedelta(minutes=180),
            "completed", "Alpha", "Beta",
        )
        job = DetailJob("market:closed", "market_after_closure", fixture, now)
        collector._market_tokens = lambda fixture_id, **kwargs: ["a", "b"]
        collector._polymarket_post = lambda *args: ([{"asset_id": "a"}], {}, 200)
        collector.loader = type(
            "NoOpLoader", (), {"load_polymarket_payload": lambda *args: None}
        )()
        expected = [
            ("incomplete", 1, True),
            ("incomplete", 2, True),
            ("terminal", 3, False),
        ]
        for state, attempts, has_retry in expected:
            collector._execute_market_jobs([job], now)
            row = self.warehouse.connection.execute(
                    """
                    SELECT status,attempts,maximum_attempts,next_attempt_at
                    FROM collection_checkpoint WHERE job_key='market:closed'
                    """
                ).fetchone()
            self.assertEqual((state, attempts, 3), row[:3])
            self.assertEqual(has_retry, row[3] is not None)


if __name__ == "__main__":
    unittest.main()
