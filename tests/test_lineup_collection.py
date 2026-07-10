from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collection_planner import lineup_stage_plans  # noqa: E402
from soccer_bot.collection_state import validate_lineups  # noqa: E402
from soccer_bot.collector import Collector, DetailJob, FixtureRecord  # noqa: E402
from soccer_bot.database import Warehouse  # noqa: E402
from soccer_bot.http import HttpResponse  # noqa: E402
from soccer_bot.loaders import RawCatalog, WarehouseLoader  # noqa: E402
from soccer_bot.raw_store import RawArtifactStore  # noqa: E402


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("Network access is not expected in this test")

    def post_json(self, *args, **kwargs):
        raise AssertionError("Network access is not expected in this test")


class LineupStagePlannerTests(unittest.TestCase):
    def setUp(self):
        self.kickoff = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        self.common = {
            "fixture_source_id": "900",
            "schedule_version": "schedule-a",
            "kickoff": self.kickoff,
            "offsets": [50, 35, 20, 5],
            "lineup_complete": False,
        }

    def plans_at(self, minutes_before, attempted=()):
        return lineup_stage_plans(
            now=self.kickoff - timedelta(minutes=minutes_before),
            attempted_job_keys=set(attempted),
            **self.common,
        )

    def test_four_stages_are_due_at_exact_boundaries(self):
        first = self.plans_at(50)
        self.assertEqual(["lineup_t_minus_50"], [plan.stage for plan in first])
        attempted = {first[0].job_key}

        second = self.plans_at(35, attempted)
        self.assertEqual(["lineup_t_minus_35"], [plan.stage for plan in second])
        attempted.add(second[0].job_key)

        third = self.plans_at(20, attempted)
        self.assertEqual(["lineup_t_minus_20"], [plan.stage for plan in third])
        attempted.add(third[0].job_key)

        fourth = self.plans_at(5, attempted)
        self.assertEqual(["lineup_t_minus_5"], [plan.stage for plan in fourth])
        attempted.add(fourth[0].job_key)
        self.assertEqual([], self.plans_at(4, attempted))

    def test_missed_stages_are_recoverable_in_one_current_response(self):
        plans = self.plans_at(20)
        self.assertEqual(
            ["lineup_t_minus_50", "lineup_t_minus_35", "lineup_t_minus_20"],
            [plan.stage for plan in plans],
        )
        self.assertEqual(
            [self.kickoff - timedelta(minutes=offset) for offset in (50, 35, 20)],
            [plan.stage_time for plan in plans],
        )

    def test_complete_lineup_suppresses_all_later_stages(self):
        plans = lineup_stage_plans(
            now=self.kickoff - timedelta(minutes=5),
            attempted_job_keys=set(),
            lineup_complete=True,
            fixture_source_id="900",
            schedule_version="schedule-a",
            kickoff=self.kickoff,
            offsets=[50, 35, 20, 5],
        )
        self.assertEqual([], plans)

    def test_schedule_version_changes_job_keys(self):
        old = self.plans_at(50)[0].job_key
        new = [
            plan.job_key
            for plan in lineup_stage_plans(
                now=self.kickoff - timedelta(minutes=50),
                attempted_job_keys=set(),
                lineup_complete=False,
                fixture_source_id="900",
                schedule_version="schedule-b",
                kickoff=self.kickoff,
                offsets=[50, 35, 20, 5],
            )
        ][0]
        self.assertNotEqual(old, new)
        self.assertIn(":schedule-b:", new)


class LineupLoaderTests(unittest.TestCase):
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
        self.loader = WarehouseLoader(self.warehouse, RawCatalog.__new__(RawCatalog))

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    @staticmethod
    def payload():
        def team_block(team_id, prefix):
            return {
                "team": {"id": team_id, "name": f"{prefix} FC"},
                "formation": "4-3-3",
                "startXI": [
                    {
                        "player": {
                            "id": team_id * 100 + index,
                            "name": f"{prefix} Player {index}",
                            "pos": "F",
                            "number": index,
                        }
                    }
                    for index in range(1, 12)
                ],
                "substitutes": [],
            }

        return {
            "response": [{
                "fixture": {
                    "id": 900,
                    "date": "2026-07-10T18:00:00+00:00",
                    "status": {"short": "NS"},
                    "venue": {"name": "Test Ground"},
                },
                "league": {
                    "id": 39, "name": "Premier League",
                    "country": "England", "season": 2026,
                },
                "teams": {
                    "home": {"id": 1, "name": "Alpha FC"},
                    "away": {"id": 2, "name": "Beta FC"},
                },
                "score": {},
                "lineups": [team_block(1, "Alpha"), team_block(2, "Beta")],
                "events": [],
                "players": [],
                "statistics": [],
            }]}

    def item(self, retrieved_at, artifact_id, content_sha, planned_schedule_id=None):
        item = {
            "retrieved_at": retrieved_at,
            "content_sha256": content_sha,
            "_raw_artifact_id": artifact_id,
            "request_parameters": {"ids": "900"},
        }
        if planned_schedule_id:
            item["_lineup_schedule_observation_ids"] = {"900": planned_schedule_id}
        return item

    def test_snapshot_provenance_and_retrieval_identity_are_preserved(self):
        payload = self.payload()
        self.loader.load_api_football_payload(
            payload,
            self.item("2026-07-10T17:00:00+00:00", "artifact-1", "same-body"),
            "fixture_details_batch",
        )
        planned_schedule_id = self.warehouse.connection.execute(
            """
            SELECT schedule_observation_id
            FROM fixture_schedule_observation
            WHERE raw_artifact_id = 'artifact-1'
            """
        ).fetchone()[0]
        self.loader.load_api_football_payload(
            payload,
            self.item(
                "2026-07-10T17:25:00+00:00", "artifact-2", "same-body",
                planned_schedule_id,
            ),
            "fixture_details_batch",
        )
        rows = self.warehouse.connection.execute(
            """
            SELECT raw_artifact_id, schedule_observation_id,
                   kickoff_known_at_retrieval, captured_before_kickoff,
                   identity_state
            FROM lineup_snapshot
            ORDER BY raw_artifact_id, team_id
            """
        ).fetchall()
        self.assertEqual(4, len(rows))
        self.assertEqual({"artifact-1", "artifact-2"}, {row[0] for row in rows})
        self.assertEqual({True}, {row[3] for row in rows})
        self.assertEqual(
            {"resolved", "partially_resolved", "unresolved"} & {row[4] for row in rows},
            {"unresolved"},
        )
        self.assertEqual({planned_schedule_id}, {row[1] for row in rows})
        self.assertEqual(
            4,
            self.warehouse.connection.execute(
                "SELECT count(*) FROM lineup_snapshot"
            ).fetchone()[0],
        )

        fixture_id = self.warehouse.connection.execute(
            "SELECT fixture_id FROM fixture_schedule_observation WHERE schedule_observation_id = ?",
            [planned_schedule_id],
        ).fetchone()[0]
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_schedule_observation (
                schedule_observation_id, fixture_id, source_code,
                fixture_source_id, provider_status, canonical_status,
                scheduled_kickoff, retrieved_at, raw_artifact_id
            )
            SELECT 'schedule-b', fixture_id, source_code, fixture_source_id,
                   'NS', 'scheduled', scheduled_kickoff + INTERVAL '1 day',
                   retrieved_at + INTERVAL '1 day', 'artifact-schedule-b'
            FROM fixture_schedule_observation
            WHERE schedule_observation_id = ?
            """,
            [planned_schedule_id],
        )
        self.assertEqual(
            "pending",
            validate_lineups(
                self.warehouse.connection,
                fixture_id,
                "api_football",
                datetime(2026, 7, 10, 17, 30, tzinfo=timezone.utc),
                schedule_observation_id="schedule-b",
            ).state,
        )

        self.loader.load_api_football_payload(
            payload,
            self.item(
                "2026-07-10T17:25:00+00:00", "artifact-2", "same-body",
                planned_schedule_id,
            ),
            "fixture_details_batch",
        )
        self.assertEqual(
            4,
            self.warehouse.connection.execute(
                "SELECT count(*) FROM lineup_snapshot"
            ).fetchone()[0],
        )

    def test_postmatch_stats_reconcile_unresolved_alias_without_global_merge(self):
        pregame = self.payload()
        self.loader.load_api_football_payload(
            pregame,
            self.item("2026-07-10T17:00:00+00:00", "artifact-pregame", "pregame"),
            "fixture_details_batch",
        )
        pending = self.warehouse.connection.execute(
            """
            SELECT m.internal_entity_id, p.is_identity_placeholder
            FROM source_entity_map m
            JOIN player_identity_state p ON p.player_id=m.internal_entity_id
            WHERE m.source_code='api_football_lineup'
              AND m.source_entity_id='900|101|alpha player 1'
              AND m.review_status='pending'
            """
        ).fetchone()
        self.assertIsNotNone(pending)
        self.assertTrue(pending[1])
        placeholder_id = pending[0]

        postmatch = self.payload()
        postmatch["response"][0]["players"] = [{
            "team": {"id": 1, "name": "Alpha FC"},
            "players": [{
                "player": {"id": 101, "name": "Alpha Player 1"},
                "statistics": [{
                    "games": {
                        "minutes": 90, "position": "F", "substitute": False,
                        "number": 1,
                    },
                    "goals": {}, "shots": {}, "passes": {}, "cards": {},
                }],
            }],
        }]
        self.loader.load_api_football_payload(
            postmatch,
            self.item("2026-07-10T20:00:00+00:00", "artifact-postmatch", "postmatch"),
            "fixture_details_batch",
        )

        canonical_id = self.warehouse.connection.execute(
            """
            SELECT internal_entity_id
            FROM source_entity_map
            WHERE source_code='api_football'
              AND source_entity_id='101|alpha player 1'
            """
        ).fetchone()[0]
        lineup_canonical_count = self.warehouse.connection.execute(
            """
            SELECT count(*)
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            WHERE ls.fixture_id=(
                SELECT internal_entity_id FROM source_entity_map
                WHERE source_code='api_football' AND entity_type='fixture'
                  AND source_entity_id='900'
            )
              AND lp.player_id=?
            """
            , [canonical_id]
        ).fetchone()[0]
        self.assertGreaterEqual(lineup_canonical_count, 1)
        self.assertNotEqual(placeholder_id, canonical_id)
        self.assertEqual(
            (canonical_id, 'automatic'),
            self.warehouse.connection.execute(
                """
                SELECT internal_entity_id, review_status
                FROM source_entity_map
                WHERE source_code='api_football_lineup'
                  AND source_entity_id='900|101|alpha player 1'
                """
            ).fetchone(),
        )
        self.assertEqual(
            0,
            self.warehouse.connection.execute(
                """
                SELECT count(*) FROM player_match_stat_observation pm
                JOIN player_identity_state s ON s.player_id=pm.player_id
                WHERE s.is_identity_placeholder
                """
            ).fetchone()[0],
        )


class ScheduleAwareCollectorTests(unittest.TestCase):
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
        self.collector = Collector(
            warehouse=self.warehouse,
            raw_store=RawArtifactStore(self.root / "raw"),
            http_client=NoNetwork(),
            api_key="test",
            config=self.config,
        )

    def tearDown(self):
        self.warehouse.close()
        self.temp.cleanup()

    def test_reschedule_supersedes_old_lineup_jobs_and_plans_new_version(self):
        competition_id = self.warehouse.resolve_competition(
            "api_football", 39, "Premier League", country_code="England"
        )
        season_id = self.warehouse.resolve_season(
            "api_football", "39|2026", competition_id, "2026"
        )
        home_id = self.warehouse.resolve_team("api_football", 1, "Alpha", team_type="club")
        away_id = self.warehouse.resolve_team("api_football", 2, "Beta", team_type="club")
        old_kickoff = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
        fixture_id = self.warehouse.resolve_fixture(
            "api_football", 900, home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=old_kickoff, competition_id=competition_id,
            season_id=season_id, status="scheduled",
        )
        source = "api_football"
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_schedule_observation (
                schedule_observation_id, fixture_id, source_code,
                fixture_source_id, provider_status, canonical_status,
                scheduled_kickoff, retrieved_at, raw_artifact_id
            ) VALUES ('schedule-a', ?, ?, '900', 'NS', 'scheduled', ?, ?, 'schedule-artifact-a')
            """,
            [fixture_id, source, old_kickoff, datetime(2026, 7, 10, 12, tzinfo=timezone.utc)],
        )
        old_job_key = "api_football:lineup_stage:900:schedule-a:50"
        self.warehouse.connection.execute(
            """
            INSERT INTO collection_checkpoint (
                job_key, source_code, job_type, fixture_source_id,
                scheduled_for, status, attempts, last_attempt_at,
                metadata, updated_at, fixture_id, component_code
            ) VALUES (?, 'api_football', 'lineup_stage', '900', ?, 'incomplete', 1, ?, '{}', ?, ?, 'lineups')
            """,
            [old_job_key, old_kickoff - timedelta(minutes=50), old_kickoff, old_kickoff, fixture_id],
        )
        new_kickoff = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
        self.warehouse.connection.execute(
            """
            UPDATE fixture SET scheduled_kickoff = ?, status = 'scheduled'
            WHERE fixture_id = ?
            """,
            [new_kickoff, fixture_id],
        )
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_schedule_observation (
                schedule_observation_id, fixture_id, source_code,
                fixture_source_id, provider_status, canonical_status,
                scheduled_kickoff, retrieved_at, raw_artifact_id
            ) VALUES ('schedule-b', ?, ?, '900', 'NS', 'scheduled', ?, ?, 'schedule-artifact-b')
            """,
            [fixture_id, source, new_kickoff, datetime(2026, 7, 10, 13, tzinfo=timezone.utc)],
        )
        fixture = FixtureRecord(
            fixture_id, "900", new_kickoff, "scheduled", "Alpha", "Beta"
        )
        jobs = self.collector._plan_detail_jobs(
            [fixture], datetime(2026, 7, 11, 19, 10, tzinfo=timezone.utc)
        )
        lineup_jobs = [job for job in jobs if job.job_type == "lineup_stage"]
        self.assertEqual(1, len(lineup_jobs))
        self.assertIn(f":kickoff-{int(new_kickoff.timestamp())}:", lineup_jobs[0].job_key)
        self.assertEqual(
            ("terminal", "schedule_superseded"),
            self.warehouse.connection.execute(
                "SELECT status, terminal_reason FROM collection_checkpoint WHERE job_key = ?",
                [old_job_key],
            ).fetchone(),
        )

    def test_lineup_detail_uses_planned_schedule_provenance(self):
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
            "api_football", 900, home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=kickoff, competition_id=competition_id,
            season_id=season_id, status="scheduled",
        )
        self.warehouse.connection.execute(
            """
            INSERT INTO fixture_schedule_observation (
                schedule_observation_id, fixture_id, source_code,
                fixture_source_id, provider_status, canonical_status,
                scheduled_kickoff, retrieved_at, raw_artifact_id
            ) VALUES ('schedule-a', ?, 'api_football', '900', 'NS', 'scheduled', ?, ?, 'schedule-artifact-a')
            """,
            [fixture_id, kickoff, datetime(2026, 7, 10, 12, tzinfo=timezone.utc)],
        )
        payload = LineupLoaderTests.payload()
        item = {
            "retrieved_at": "2026-07-10T17:10:00+00:00",
            "content_sha256": "detail-content",
            "_raw_artifact_id": "detail-artifact",
            "request_parameters": {"ids": "900"},
        }
        self.collector._api_get = lambda *args, **kwargs: (payload, item, 200)
        fixture = FixtureRecord(
            fixture_id, "900", kickoff, "scheduled", "Alpha", "Beta"
        )
        self.collector._execute_detail_jobs(
            [
                DetailJob(
                    "api_football:lineup_stage:900:schedule-a:50",
                    "lineup_stage",
                    fixture,
                    kickoff - timedelta(minutes=50),
                    schedule_version=f"kickoff-{int(kickoff.timestamp())}",
                    schedule_observation_id="schedule-a",
                )
            ],
            datetime(2026, 7, 10, 17, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(
            {("schedule-a", True)},
            set(self.warehouse.connection.execute(
                """
                SELECT schedule_observation_id, captured_before_kickoff
                FROM lineup_snapshot
                """
            ).fetchall()),
        )
        self.assertEqual(
            ("complete",),
            self.warehouse.connection.execute(
                """
                SELECT state
                FROM fixture_collection_component
                WHERE fixture_id = ? AND component_code = 'pregame_lineup_capture'
                """,
                [fixture_id],
            ).fetchone(),
        )


if __name__ == "__main__":
    unittest.main()
