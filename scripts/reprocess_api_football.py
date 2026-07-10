#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.database import Warehouse
from soccer_bot.loaders import RawCatalog, WarehouseLoader
from scripts.build_database import run_quality_checks


COUNT_TABLES = (
    "raw_artifact",
    "fixture",
    "fixture_result_observation",
    "player_identity_state",
    "lineup_snapshot",
    "lineup_player",
    "appearance",
    "match_event",
    "team_match_stat_observation",
    "player_match_stat_observation",
)


def counts(warehouse: Warehouse) -> dict[str, int]:
    return {
        table: warehouse.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in COUNT_TABLES
    }


def main() -> int:
    warehouse = Warehouse(
        ROOT / "data" / "warehouse" / "soccer.duckdb",
        ROOT / "migrations",
        ROOT / "config" / "entity_aliases.json",
    )
    try:
        warehouse.migrate()
        warehouse.register_sources()
        before = counts(warehouse)
        catalog = RawCatalog(ROOT / "data" / "raw", warehouse)
        catalog.load_database_catalog()
        WarehouseLoader(warehouse, catalog).load_api_football()
        warehouse.reconcile_team_aliases()
        run_quality_checks(warehouse)
        after = counts(warehouse)
        for table in COUNT_TABLES:
            print(f"{table}: before={before[table]} after={after[table]} delta={after[table] - before[table]}")
        blocking_issues, warnings = warehouse.connection.execute(
            """
            SELECT count(*) FILTER (WHERE severity = 'blocking'),
                   count(*) FILTER (WHERE severity = 'warning')
            FROM data_quality_issue WHERE status = 'open'
            """
        ).fetchone()
        print(f"open_blocking_quality_issues={blocking_issues}")
        print(f"open_quality_warnings={warnings}")
        return 1 if blocking_issues else 0
    finally:
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
