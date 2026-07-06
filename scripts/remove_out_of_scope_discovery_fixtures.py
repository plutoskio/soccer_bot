#!/usr/bin/env python3
"""Remove shallow API-Football fixtures outside configured competitions.

Raw artifacts are deliberately retained. The cleanup only removes relational
fixture rows introduced when unfiltered daily-discovery artifacts were replayed.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "warehouse" / "soccer.duckdb"
CONFIG = ROOT / "config" / "collector.json"


def main() -> int:
    config = json.loads(CONFIG.read_text())
    monitored_ids = [str(value) for value in config["competitions"]["league_ids"]]
    placeholders = ",".join("?" for _ in monitored_ids)
    target_sql = f"""
        SELECT DISTINCT fixture_map.internal_entity_id
        FROM source_entity_map fixture_map
        JOIN fixture f ON f.fixture_id = fixture_map.internal_entity_id
        LEFT JOIN source_entity_map competition_map
          ON competition_map.entity_type = 'competition'
         AND competition_map.source_code = 'api_football'
         AND competition_map.internal_entity_id = f.competition_id
        WHERE fixture_map.source_code = 'api_football'
          AND fixture_map.entity_type = 'fixture'
          AND (
              competition_map.source_entity_id IS NULL
              OR competition_map.source_entity_id NOT IN ({placeholders})
          )
    """

    connection = duckdb.connect(str(DATABASE))
    try:
        targets = connection.execute(target_sql, monitored_ids).fetchall()
        if not targets:
            print("No out-of-scope discovery fixtures remain; no changes made.")
            return 0
        if len(targets) != 477:
            raise RuntimeError(
                f"Expected exactly 477 out-of-scope fixtures, found {len(targets)}; "
                "database left unchanged"
            )

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "CREATE TEMP TABLE scope_cleanup_targets (fixture_id VARCHAR PRIMARY KEY)"
            )
            connection.execute(
                f"INSERT INTO scope_cleanup_targets {target_sql}", monitored_ids
            )
            connection.execute(
                """DELETE FROM fixture_result_observation
                   WHERE fixture_id IN (SELECT fixture_id FROM scope_cleanup_targets)"""
            )
            connection.execute(
                """
                DELETE FROM source_entity_map
                WHERE entity_type = 'fixture'
                  AND internal_entity_id IN (
                      SELECT fixture_id FROM scope_cleanup_targets
                  )
                """,
            )
            connection.execute(
                "DELETE FROM fixture WHERE fixture_id IN "
                "(SELECT fixture_id FROM scope_cleanup_targets)"
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        remaining = connection.execute(target_sql, monitored_ids).fetchall()
        if remaining:
            raise RuntimeError(f"Cleanup left {len(remaining)} target fixtures")
        print("Removed 477 out-of-scope fixtures and their 310 score observations.")
        print("Raw JSON artifacts were preserved.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
