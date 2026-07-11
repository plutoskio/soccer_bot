from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collection_planner import (  # noqa: E402
    discovery_date_window,
    discovery_job_for_date,
    effective_recovery_days,
)
from soccer_bot.collector import Collector, FixtureRecord  # noqa: E402
from soccer_bot.database import Warehouse  # noqa: E402
from soccer_bot.http import HttpResponse  # noqa: E402
from soccer_bot.raw_store import RawArtifactStore  # noqa: E402


class DateDiscoveryHttp:
    def __init__(self, responses_by_date=None):
        self.responses_by_date = responses_by_date or {}
        self.calls = []

    def get(self, base_url, path, *, params=None, headers=None, timeout=30.0):
        self.calls.append((base_url, path, dict(params or {})))
        if "date" in (params or {}):
            body = {
                "response": self.responses_by_date.get(params["date"], []),
                "errors": [],
            }
            return HttpResponse(
                f"{base_url}{path}", 200, {"content-type": "application/json"},
                json.dumps(body).encode(),
            )
        raise AssertionError(f"Unexpected request: {base_url} {path} {params}")

    def post_json(self, *args, **kwargs):
        raise AssertionError("Polymarket order books are not expected in this test")


class PlannerUnitTests(unittest.TestCase):
    def test_window_and_cadence_slots_are_deterministic(self):
        today = date(2026, 7, 10)
        now = datetime(2026, 7, 10, 10, 15, tzinfo=timezone.utc)
        start, end, dates = discovery_date_window(
            today, recovery_days=14, planning_days=7
        )
        self.assertEqual(date(2026, 6, 26), start)
        self.assertEqual(date(2026, 7, 17), end)
        self.assertEqual(22, len(dates))

        past = discovery_job_for_date(
            date(2026, 7, 9), today=today, now=now,
            zone=ZoneInfo("Europe/Luxembourg"),
        )
        current = discovery_job_for_date(
            today, today=today, now=now,
            zone=ZoneInfo("Europe/Luxembourg"),
        )
        future = discovery_job_for_date(
            date(2026, 7, 12), today=today, now=now,
            zone=ZoneInfo("Europe/Luxembourg"),
        )
        self.assertEqual("recovery", past.cadence)
        self.assertIn(":recovery:2026-07-09", past.job_key)
        self.assertEqual("six_hour", current.cadence)
        self.assertIn(":six_hour:2026-07-10T12:00", current.job_key)
        self.assertEqual("daily", future.cadence)
        self.assertIn(":daily:2026-07-10", future.job_key)

    def test_catch_up_cannot_shrink_configured_recovery(self):
        self.assertEqual(14, effective_recovery_days(14, 0))
        self.assertEqual(30, effective_recovery_days(14, 30))
        self.assertEqual(21, effective_recovery_days(14, completed_frontier_days=21))


class RecoveryIntegrationTests(unittest.TestCase):
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
        self.config["api_football"]["minimum_interval_seconds"] = 0

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    def collector(self, http):
        return Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=http,
            api_key="test",
            config=self.config,
        )

    def test_missing_dates_are_requested_once_and_restart_is_idempotent(self):
        self.config["discovery"]["recovery_days"] = 2
        self.config["discovery"]["planning_days"] = 1
        now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        http = DateDiscoveryHttp()

        first = self.collector(http).run(now=now)
        dates = [
            params["date"] for _, path, params in http.calls
            if path == "/fixtures" and "date" in params
        ]
        self.assertEqual(
            ["2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"],
            dates,
        )
        self.assertEqual(4, first["api_football_calls"])

        http.calls.clear()
        second = self.collector(http).run(now=now)
        self.assertEqual(0, second["api_football_calls"])
        self.assertEqual([], http.calls)
        self.assertEqual(
            4,
            self.warehouse.connection.execute(
                "SELECT count(*) FROM collection_checkpoint WHERE job_type = 'fixture_discovery' AND status = 'succeeded'"
            ).fetchone()[0],
        )

    def test_catch_up_days_recovers_a_longer_outage(self):
        self.config["discovery"]["recovery_days"] = 2
        self.config["discovery"]["planning_days"] = 0
        now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        http = DateDiscoveryHttp()

        self.collector(http).run(now=now, catch_up_days=5)
        dates = [
            params["date"] for _, path, params in http.calls
            if path == "/fixtures" and "date" in params
        ]
        self.assertEqual(
            [
                "2026-07-05", "2026-07-06", "2026-07-07",
                "2026-07-08", "2026-07-09", "2026-07-10",
            ],
            dates,
        )

    def test_completed_fixture_frontier_expands_recovery_for_model_history(self):
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
            scheduled_kickoff=datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc),
            competition_id=competition_id, season_id=season_id, status="completed",
        )
        self.config["discovery"]["recovery_days"] = 2
        self.config["discovery"]["planning_days"] = 0
        collector = self.collector(DateDiscoveryHttp())
        start, end, _, past_days = collector._discovery_window(
            date(2026, 7, 10), None
        )
        self.assertEqual(date(2026, 7, 1), start)
        self.assertEqual(date(2026, 7, 10), end)
        self.assertEqual(9, past_days)

    def test_explicit_three_week_catch_up_requests_each_date_once(self):
        self.config["discovery"]["recovery_days"] = 2
        self.config["discovery"]["planning_days"] = 0
        now = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        http = DateDiscoveryHttp()

        self.collector(http).run(now=now, catch_up_days=21)
        dates = [
            params["date"] for _, path, params in http.calls
            if path == "/fixtures" and "date" in params
        ]
        self.assertEqual(22, len(dates))
        self.assertEqual("2026-06-19", dates[0])
        self.assertEqual("2026-07-10", dates[-1])
        self.assertEqual(len(dates), len(set(dates)))

    def test_discovery_filters_before_loading_canonical_fixtures(self):
        self.config["discovery"]["recovery_days"] = 0
        self.config["discovery"]["planning_days"] = 0
        match = {
            "fixture": {
                "id": 777,
                "date": "2026-07-10T23:00:00+00:00",
                "status": {"short": "NS"},
            },
            "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
            "teams": {
                "home": {"id": 1, "name": "Alpha FC"},
                "away": {"id": 2, "name": "Beta FC"},
            },
        }
        out_of_scope = json.loads(json.dumps(match))
        out_of_scope["fixture"]["id"] = 778
        out_of_scope["league"] = {
            "id": 363, "name": "Premier League", "country": "Ethiopia", "season": 2026
        }
        http = DateDiscoveryHttp({"2026-07-10": [match, out_of_scope]})
        self.collector(http).run(
            now=datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            [("777",)],
            self.warehouse.connection.execute(
                """
                SELECT source_entity_id
                FROM source_entity_map
                WHERE source_code = 'api_football' AND entity_type = 'fixture'
                ORDER BY source_entity_id
                """
            ).fetchall(),
        )

    def test_near_kickoff_fixture_refresh_uses_fixture_and_kickoff_key(self):
        competition_id = self.warehouse.resolve_competition(
            "api_football", 39, "Premier League", country_code="England"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "39|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team("api_football", 1, "Alpha", team_type="club")
        away_id = self.warehouse.resolve_team("api_football", 2, "Beta", team_type="club")
        kickoff = datetime(2026, 7, 10, 19, 0, tzinfo=timezone.utc)
        fixture_id = self.warehouse.resolve_fixture(
            "api_football", 777, home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=kickoff, competition_id=competition_id,
            season_id=season_id, status="scheduled",
        )
        fixture = FixtureRecord(
            fixture_id, "777", kickoff, "scheduled", "Alpha", "Beta"
        )
        collector = self.collector(DateDiscoveryHttp())
        jobs = collector._plan_fixture_refresh_jobs(
            [fixture], datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(1, len(jobs))
        self.assertIn("fixture_refresh:777:near_kickoff", jobs[0].job_key)
        self.assertIn(str(int(kickoff.timestamp())), jobs[0].job_key)

    def test_past_fixture_without_pregame_lineup_is_marked_missed(self):
        competition_id = self.warehouse.resolve_competition(
            "api_football", 39, "Premier League", country_code="England"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "39|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team("api_football", 1, "Alpha", team_type="club")
        away_id = self.warehouse.resolve_team("api_football", 2, "Beta", team_type="club")
        kickoff = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        fixture_id = self.warehouse.resolve_fixture(
            "api_football", 778, home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=kickoff, competition_id=competition_id,
            season_id=season_id, status="scheduled",
        )
        fixture = FixtureRecord(
            fixture_id, "778", kickoff, "scheduled", "Alpha", "Beta"
        )
        collector = self.collector(DateDiscoveryHttp())
        now = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
        collector._reconcile_fixture_components([fixture], now)
        collector._mark_expired_pregame_components([fixture], now)
        self.assertEqual(
            ("missed", "kickoff_passed_without_pregame_lineup"),
            self.warehouse.connection.execute(
                """
                SELECT state, reason_code
                FROM fixture_collection_component
                WHERE fixture_id = ? AND component_code = 'pregame_lineup_capture'
                """,
                [fixture_id],
            ).fetchone(),
        )


if __name__ == "__main__":
    unittest.main()
