#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import os
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.database import Warehouse
from soccer_bot.loaders import RawCatalog, WarehouseLoader
from scripts.build_database import run_quality_checks


def collision_counts(warehouse: Warehouse) -> tuple[int, int]:
    row = warehouse.connection.execute(
        """
        SELECT count(*), coalesce(sum(source_ids), 0) FROM (
            SELECT internal_entity_id, count(DISTINCT source_entity_id) AS source_ids
            FROM source_entity_map
            WHERE source_code='api_football' AND entity_type='player'
            GROUP BY internal_entity_id
            HAVING source_ids > 1
        )
        """
    ).fetchone()
    return int(row[0]), int(row[1])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split incorrectly name-merged API-Football players and replay retained raw data"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply the transactional repair; otherwise only report collision counts",
    )
    parser.add_argument(
        "--reuse-backup", action="store_true",
        help="Reuse the existing pre-repair backup after a rolled-back repair attempt",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = ROOT / "data" / "warehouse" / "soccer.duckdb"
    backup_path = ROOT / "data" / "warehouse" / "soccer.pre_player_identity_fix.duckdb"
    working_path = ROOT / "data" / "warehouse" / "soccer.player_identity_repair.working.duckdb"
    if args.apply:
        if backup_path.exists():
            if not args.reuse_backup:
                raise RuntimeError(f"Refusing to overwrite existing backup: {backup_path}")
        else:
            shutil.copy2(database_path, backup_path)

        # Rebuild a disposable copy. The active database is replaced only after
        # the copy passes every validation, so interruption cannot leave it
        # half-repaired.
        working_path.unlink(missing_ok=True)
        shutil.copy2(database_path, working_path)

    warehouse = Warehouse(
        working_path if args.apply else database_path,
        ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
    )
    succeeded = False
    try:
        warehouse.migrate()
        before = collision_counts(warehouse)
        print(f"colliding_internal_players_before={before[0]}")
        print(f"source_ids_in_collisions_before={before[1]}")
        if not args.apply:
            return 0

        with warehouse.transaction():
            connection = warehouse.connection
            connection.execute(
                """DELETE FROM lineup_player WHERE lineup_snapshot_id IN (
                       SELECT lineup_snapshot_id FROM lineup_snapshot
                       WHERE source_code='api_football'
                   )"""
            )
            for table in (
                "lineup_snapshot", "appearance", "match_event",
                "player_match_stat_observation", "player_season_stat",
            ):
                connection.execute(f"DELETE FROM {table} WHERE source_code='api_football'")
            connection.execute(
                """DELETE FROM source_entity_map
                   WHERE source_code IN (
                       'api_football', 'api_football_lineup', 'api_football_event'
                   )
                     AND entity_type='player'"""
            )

        # Replay outside the delete transaction to avoid retaining gigabytes of
        # old and new row versions in memory. This database file is disposable.
        catalog = RawCatalog(ROOT / "data" / "raw", warehouse)
        catalog.load_database_catalog()
        WarehouseLoader(warehouse, catalog).load_api_football()
        run_quality_checks(warehouse)

        after = collision_counts(warehouse)
        blocking = warehouse.connection.execute(
            """SELECT count(*) FROM data_quality_issue
               WHERE status='open' AND severity='blocking'"""
        ).fetchone()[0]
        if after != (0, 0):
            raise RuntimeError(f"Player identity collisions remain after repair: {after}")
        if blocking:
            raise RuntimeError(f"Repair produced {blocking} blocking quality issues")

        print(f"colliding_internal_players_after={after[0]}")
        print(f"source_ids_in_collisions_after={after[1]}")
        print(f"backup={backup_path.relative_to(ROOT)}")
        succeeded = True
        return 0
    finally:
        warehouse.close()
        if args.apply:
            if succeeded:
                os.replace(working_path, database_path)
            else:
                working_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
