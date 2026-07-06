#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.database import Warehouse
from soccer_bot.loaders import (
    RawCatalog,
    WarehouseLoader,
    compatible_api_player_compound_names,
    compatible_api_player_names,
)
from scripts.build_database import run_quality_checks


@dataclass(frozen=True)
class RecoverableLink:
    fixture_id: str
    raw_artifact_id: str
    team_id: str
    stat_player_id: str
    stat_name: str
    lineup_snapshot_id: str
    lineup_player_id: str
    lineup_name: str


def recoverable_links(warehouse: Warehouse) -> list[RecoverableLink]:
    rows = warehouse.connection.execute(
        """
        SELECT pm.fixture_id, pm.raw_artifact_id, pm.team_id,
               pm.player_id, stat_player.full_name, ls.lineup_snapshot_id,
               lp.player_id, lineup_player.full_name,
               pm.shirt_number, lp.shirt_number
        FROM player_match_stat_observation pm
        JOIN player stat_player ON stat_player.player_id=pm.player_id
        JOIN lineup_snapshot ls
          ON ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
         AND ls.source_code=pm.source_code
         AND ls.raw_artifact_id=pm.raw_artifact_id
        JOIN lineup_player lp ON lp.lineup_snapshot_id=ls.lineup_snapshot_id
        JOIN player lineup_player ON lineup_player.player_id=lp.player_id
        WHERE pm.source_code='api_football' AND pm.minutes_played>0
          AND NOT EXISTS (
              SELECT 1 FROM lineup_player exact
              WHERE exact.lineup_snapshot_id=ls.lineup_snapshot_id
                AND exact.player_id=pm.player_id
          )
        ORDER BY pm.fixture_id, pm.player_id, lp.player_id
        """
    ).fetchall()
    grouped: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for (
        fixture_id, raw_artifact_id, team_id, stat_player_id, stat_name,
        lineup_snapshot_id, lineup_player_id, lineup_name,
        stat_shirt_number, lineup_shirt_number,
    ) in rows:
        key = (
            fixture_id, raw_artifact_id, team_id, stat_player_id, stat_name,
            lineup_snapshot_id,
        )
        strong_name_match = compatible_api_player_names(stat_name, lineup_name)
        compound_name_match = (
            stat_shirt_number is not None
            and lineup_shirt_number is not None
            and int(stat_shirt_number) == int(lineup_shirt_number)
            and compatible_api_player_compound_names(stat_name, lineup_name)
        )
        if strong_name_match or compound_name_match:
            grouped.setdefault(key, []).append((lineup_player_id, lineup_name))

    result = []
    for key, candidates in grouped.items():
        unique = sorted(set(candidates))
        if len(unique) != 1:
            continue
        lineup_player_id, lineup_name = unique[0]
        result.append(RecoverableLink(*key, lineup_player_id, lineup_name))
    return sorted(result, key=lambda row: (row.fixture_id, row.stat_player_id))


def collision_count(warehouse: Warehouse) -> int:
    return warehouse.connection.execute(
        """
        SELECT count(*) FROM (
            SELECT internal_entity_id
            FROM source_entity_map
            WHERE source_code='api_football' AND entity_type='player'
            GROUP BY internal_entity_id
            HAVING count(DISTINCT source_entity_id)>1
        )
        """
    ).fetchone()[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair uniquely resolvable API-Football lineup/event links missed "
            "because provider sections use different transliterations or "
            "shortened compound surnames"
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="repair a disposable database copy and atomically install it",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_path = ROOT / "data" / "warehouse" / "soccer.duckdb"
    backup_path = ROOT / "data" / "warehouse" / "soccer.pre_compound_name_repair.duckdb"
    working_path = ROOT / "data" / "warehouse" / "soccer.transliteration_repair.working.duckdb"

    if not args.apply:
        warehouse = Warehouse(
            database_path, ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
        )
        try:
            links = recoverable_links(warehouse)
            print(f"recoverable_links={len(links)}")
            print(f"affected_fixtures={len({link.fixture_id for link in links})}")
            print(f"affected_raw_artifacts={len({link.raw_artifact_id for link in links})}")
            for link in links:
                print(
                    f"{link.fixture_id}: {link.lineup_name!r} -> {link.stat_name!r}"
                )
            return 0
        finally:
            warehouse.close()

    if backup_path.exists():
        raise RuntimeError(f"Refusing to overwrite existing backup: {backup_path}")
    working_path.unlink(missing_ok=True)
    shutil.copy2(database_path, backup_path)
    shutil.copy2(database_path, working_path)

    warehouse = Warehouse(
        working_path, ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
    )
    succeeded = False
    try:
        before = recoverable_links(warehouse)
        if not before:
            print("recoverable_links_before=0")
            succeeded = True
            return 0

        affected_artifacts = {link.raw_artifact_id for link in before}
        catalog = RawCatalog(ROOT / "data" / "raw", warehouse)
        items = {
            item["_raw_artifact_id"]: item
            for item in catalog.items
            if item["_raw_artifact_id"] in affected_artifacts
        }
        missing_artifacts = sorted(affected_artifacts - set(items))
        if missing_artifacts:
            raise RuntimeError(f"Missing retained raw artifacts: {missing_artifacts}")

        loader = WarehouseLoader(warehouse, catalog)
        with warehouse.transaction():
            for raw_artifact_id in sorted(affected_artifacts):
                item = items[raw_artifact_id]
                loader.load_api_football_payload(
                    catalog.read_json(item), item, item["resource"]
                )
            run_quality_checks(warehouse)

        unresolved = []
        for link in before:
            linked = warehouse.connection.execute(
                """
                SELECT count(*) FROM lineup_player
                WHERE lineup_snapshot_id=? AND player_id=?
                """,
                [link.lineup_snapshot_id, link.stat_player_id],
            ).fetchone()[0]
            if linked != 1:
                unresolved.append(link)
        remaining = recoverable_links(warehouse)
        blocking = warehouse.connection.execute(
            """SELECT count(*) FROM data_quality_issue
               WHERE status='open' AND severity='blocking'"""
        ).fetchone()[0]
        collisions = collision_count(warehouse)
        if unresolved or remaining or blocking or collisions:
            raise RuntimeError(
                "Repair validation failed: "
                f"unresolved={len(unresolved)}, remaining={len(remaining)}, "
                f"blocking={blocking}, collisions={collisions}"
            )

        print(f"recoverable_links_before={len(before)}")
        print(f"affected_fixtures={len({link.fixture_id for link in before})}")
        print(f"affected_raw_artifacts={len(affected_artifacts)}")
        print("recoverable_links_after=0")
        print("blocking_quality_issues=0")
        print("api_player_identity_collisions=0")
        print(f"backup={backup_path.relative_to(ROOT)}")
        succeeded = True
        return 0
    finally:
        warehouse.close()
        if succeeded:
            os.replace(working_path, database_path)
        else:
            working_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
