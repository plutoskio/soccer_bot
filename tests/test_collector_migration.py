from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse  # noqa: E402


class CollectorMigrationTests(unittest.TestCase):
    def test_populated_006_database_migrates_without_fact_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_migrations = root / "old_migrations"
            old_migrations.mkdir()
            for version in range(1, 7):
                source = next((ROOT / "migrations").glob(f"{version:03d}_*.sql"))
                shutil.copy2(source, old_migrations / source.name)

            path = root / "warehouse.duckdb"
            old = Warehouse(path, old_migrations)
            old.migrate()
            now = datetime(2026, 7, 10, tzinfo=timezone.utc)
            old.connection.execute(
                "INSERT INTO competition (competition_id,name) VALUES ('c','League')"
            )
            old.connection.execute(
                "INSERT INTO season (season_id,competition_id,name) VALUES ('s','c','2026')"
            )
            for team in ("home", "away"):
                old.connection.execute(
                    "INSERT INTO team (team_id,name,normalized_name,team_type) VALUES (?,?,?,'club')",
                    [team, team.title(), team],
                )
            old.connection.execute(
                """
                INSERT INTO fixture (
                    fixture_id,competition_id,season_id,home_team_id,away_team_id,
                    scheduled_kickoff,status
                ) VALUES ('f','c','s','home','away',?,'scheduled')
                """,
                [now],
            )
            old.connection.execute(
                """
                INSERT INTO fixture_result_observation (
                    observation_id,fixture_id,source_code,retrieved_at,
                    home_score_regulation,away_score_regulation,result_status
                ) VALUES ('r','f','api_football',?,1,0,'final')
                """,
                [now],
            )
            old.connection.execute(
                """
                INSERT INTO collection_checkpoint (
                    job_key,source_code,job_type,status,attempts,updated_at
                ) VALUES ('legacy','api_football','fixture_discovery','incomplete',1,?)
                """,
                [now],
            )
            before = {
                table: old.connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "fixture", "fixture_result_observation", "team", "player",
                    "source_entity_map", "data_quality_issue",
                )
            }
            eligibility_before = old.connection.execute(
                "SELECT * FROM fixture_model_eligibility WHERE fixture_id='f'"
            ).fetchone()
            old.close()

            upgraded = Warehouse(path, ROOT / "migrations")
            upgraded.migrate()
            try:
                after = {
                    table: upgraded.connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0]
                    for table in before
                }
                self.assertEqual(before, after)
                self.assertEqual(
                    eligibility_before,
                    upgraded.connection.execute(
                        "SELECT * FROM fixture_model_eligibility WHERE fixture_id='f'"
                    ).fetchone(),
                )
                self.assertEqual(
                    ("incomplete", 1, 2),
                    upgraded.connection.execute(
                        """
                        SELECT status,maximum_attempts,priority
                        FROM collection_checkpoint WHERE job_key='legacy'
                        """
                    ).fetchone(),
                )
                self.assertEqual(
                    "013_correction_refresh_chronology",
                    upgraded.connection.execute(
                        "SELECT max(version) FROM schema_migration"
                    ).fetchone()[0],
                )
            finally:
                upgraded.close()

    def test_correction_chronology_migration_only_marks_reversed_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_migrations = root / "old_migrations"
            old_migrations.mkdir()
            for version in range(1, 13):
                source = next((ROOT / "migrations").glob(f"{version:03d}_*.sql"))
                shutil.copy2(source, old_migrations / source.name)
            path = root / "warehouse.duckdb"
            old = Warehouse(path, old_migrations)
            old.migrate()
            now = datetime(2026, 7, 11, tzinfo=timezone.utc)
            for fixture_id in ("reversed", "ordered"):
                old.connection.execute(
                    """
                    INSERT INTO fixture_collection_component (
                        fixture_id,source_code,component_code,state,
                        required_for_fixture_terminal
                    ) VALUES (?,'api_football','correction_refresh_24h',
                              'complete',true)
                    """,
                    [fixture_id],
                )
            for fixture_id, early_time, later_time in (
                ("reversed", now, now - timedelta(hours=1)),
                ("ordered", now - timedelta(hours=1), now),
            ):
                for stage, attempted_at in (
                    ("correction_refresh_24h", early_time),
                    ("correction_refresh_72h", later_time),
                ):
                    old.connection.execute(
                        """
                        INSERT INTO collection_checkpoint (
                            job_key,source_code,job_type,status,attempts,
                            last_attempt_at,completed_at,updated_at,fixture_id,
                            maximum_attempts,priority
                        ) VALUES (?, 'api_football', ?, 'succeeded', 1,
                                  ?, ?, ?, ?, 1, 2)
                        """,
                        [f"{fixture_id}:{stage}", stage, attempted_at,
                         attempted_at, attempted_at, fixture_id],
                    )
            old.close()

            upgraded = Warehouse(path, ROOT / "migrations")
            upgraded.migrate()
            try:
                self.assertEqual(
                    [
                        ("ordered", "complete"),
                        ("reversed", "missed"),
                    ],
                    upgraded.connection.execute(
                        """
                        SELECT fixture_id,state
                        FROM fixture_collection_component ORDER BY fixture_id
                        """
                    ).fetchall(),
                )
                self.assertEqual(
                    ("terminal", "correction_stage_superseded_by_72h"),
                    upgraded.connection.execute(
                        """
                        SELECT status,terminal_reason FROM collection_checkpoint
                        WHERE job_key='reversed:correction_refresh_24h'
                        """
                    ).fetchone(),
                )
                self.assertEqual(
                    ("succeeded", None),
                    upgraded.connection.execute(
                        """
                        SELECT status,terminal_reason FROM collection_checkpoint
                        WHERE job_key='ordered:correction_refresh_24h'
                        """
                    ).fetchone(),
                )
            finally:
                upgraded.close()


if __name__ == "__main__":
    unittest.main()
