#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import filecmp
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.database import Warehouse
from soccer_bot.loaders import RawCatalog, WarehouseLoader
from scripts.build_database import run_quality_checks


PROTECTED_TABLES = (
    "fixture",
    "fixture_result_observation",
    "team_match_stat_observation",
    "player_match_stat_observation",
    "appearance",
)

EMBEDDED_RESOURCES = {
    "fixtures_by_date",
    "fixture_by_id",
    "fixture_details_batch",
    "pro_validation_fixture_batch",
    "historical_coverage_sample",
    "historical_backfill_batch",
}


def table_fingerprint(warehouse: Warehouse, table: str) -> tuple[int, str, str]:
    columns = [row[0] for row in warehouse.connection.execute(f"DESCRIBE {table}").fetchall()]
    quoted = ", ".join(f'"{column}"' for column in columns)
    row = warehouse.connection.execute(
        f"""
        SELECT count(*),
               coalesce(sum(hash({quoted})::HUGEINT), 0)::VARCHAR,
               coalesce(bit_xor(hash({quoted})), 0)::VARCHAR
        FROM {table}
        """
    ).fetchone()
    return int(row[0]), str(row[1]), str(row[2])


def protected_fingerprints(warehouse: Warehouse) -> dict[str, tuple[int, str, str]]:
    return {table: table_fingerprint(warehouse, table) for table in PROTECTED_TABLES}


def link_metrics(warehouse: Warehouse) -> dict[str, int]:
    connection = warehouse.connection
    common = """
        FROM lineup_snapshot ls
        JOIN lineup_player lp USING (lineup_snapshot_id)
        JOIN player_match_stat_observation pm
          ON pm.fixture_id=ls.fixture_id AND pm.team_id=ls.team_id
         AND pm.player_id=lp.player_id AND pm.source_code=ls.source_code
         AND pm.raw_artifact_id=ls.raw_artifact_id
        WHERE ls.source_code='api_football'
    """
    unlisted = connection.execute(
        """
        SELECT count(*) FROM player_match_stat_observation pm
        JOIN lineup_snapshot ls
          ON ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
         AND ls.source_code=pm.source_code AND ls.raw_artifact_id=pm.raw_artifact_id
        WHERE pm.source_code='api_football' AND pm.minutes_played>0
          AND NOT EXISTS (
              SELECT 1 FROM lineup_player lp
              WHERE lp.lineup_snapshot_id=ls.lineup_snapshot_id
                AND lp.player_id=pm.player_id
          )
        """
    ).fetchone()[0]
    return {
        "lineup_snapshots": connection.execute(
            "SELECT count(*) FROM lineup_snapshot WHERE source_code='api_football'"
        ).fetchone()[0],
        "lineup_players": connection.execute(
            """SELECT count(*) FROM lineup_player WHERE lineup_snapshot_id IN (
                   SELECT lineup_snapshot_id FROM lineup_snapshot
                   WHERE source_code='api_football')"""
        ).fetchone()[0],
        "linked_rows": connection.execute("SELECT count(*) " + common).fetchone()[0],
        "shirt_conflicts": connection.execute(
            "SELECT count(*) " + common
            + " AND lp.shirt_number IS NOT NULL AND pm.shirt_number IS NOT NULL"
              " AND lp.shirt_number<>pm.shirt_number"
        ).fetchone()[0],
        "role_conflicts": connection.execute(
            "SELECT count(*) " + common
            + " AND ((lp.selection_role='starter' AND pm.started=false)"
              " OR (lp.selection_role='substitute' AND pm.started=true))"
        ).fetchone()[0],
        "unlisted_participants": int(unlisted),
        "pending_aliases": connection.execute(
            """SELECT count(*) FROM source_entity_map
               WHERE source_code='api_football_lineup' AND entity_type='player'
                 AND review_status='pending'"""
        ).fetchone()[0],
        "bad_starter_snapshots": connection.execute(
            """SELECT count(*) FROM (
                   SELECT ls.lineup_snapshot_id
                   FROM lineup_snapshot ls JOIN lineup_player lp USING(lineup_snapshot_id)
                   WHERE ls.source_code='api_football' AND ls.lineup_type='confirmed'
                   GROUP BY ls.lineup_snapshot_id
                   HAVING count(*) FILTER(WHERE lp.selection_role='starter')<>11)"""
        ).fetchone()[0],
    }


def allowed_raw_artifact_ids(warehouse: Warehouse) -> set[str]:
    return {
        row[0]
        for row in warehouse.connection.execute(
            """
            SELECT raw_artifact_id FROM raw_artifact
            WHERE source_code='api_football'
              AND (
                resource_name<>'historical_backfill_batch'
                OR raw_artifact_id IN (
                    SELECT raw_artifact_id
                    FROM historical_backfill_batch_checkpoint
                    WHERE status='succeeded'
                )
              )
            """
        ).fetchall()
    }


def replay_items(warehouse: Warehouse, catalog: RawCatalog) -> list[dict]:
    allowed = allowed_raw_artifact_ids(warehouse)
    items = sorted(
        (
            item for item in catalog.items
            if item["source"] == "api_football"
            and item.get("http_status") == 200
            and item["_raw_artifact_id"] in allowed
        ),
        key=lambda item: (item.get("retrieved_at", ""), str(item["_metadata_path"])),
    )
    unique_items = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["resource"], item["content_sha256"])
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    return unique_items


def replay_links(
    warehouse: Warehouse, maximum_checkpoints: int | None = None
) -> tuple[int, int, int, int]:
    catalog = RawCatalog(ROOT / "data" / "raw", warehouse)
    unique_items = replay_items(warehouse, catalog)
    loader = WarehouseLoader(warehouse, catalog)
    print("Priming in-memory identity caches...", flush=True)
    loader.prime_api_link_repair_caches()
    completed = {
        (row[0], row[1], row[2])
        for row in warehouse.connection.execute(
            """SELECT phase, resource_name, content_sha256
               FROM api_player_link_repair_checkpoint"""
        ).fetchall()
    }
    work = [
        (phase, item)
        for phase in ("lineups", "events")
        for item in unique_items
        if (
            item["resource"] in EMBEDDED_RESOURCES
            or (phase == "lineups" and item["resource"] == "fixture_lineups")
            or (phase == "events" and item["resource"] == "fixture_events")
        )
    ]
    pending = [
        (phase, item) for phase, item in work
        if (phase, item["resource"], item["content_sha256"]) not in completed
    ]
    print(
        f"Replay plan: {len(work):,} phase/artifact checkpoints; "
        f"{len(pending):,} remaining.",
        flush=True,
    )
    selected = pending[:maximum_checkpoints] if maximum_checkpoints else pending
    replayed_matches = 0
    for index, (phase, item) in enumerate(selected, 1):
        payload = catalog.read_json(item)
        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, list):
            response = []
        resource = item["resource"]
        item_matches = 0
        with warehouse.transaction():
            if resource in EMBEDDED_RESOURCES:
                for match in response:
                    if not isinstance(match, dict):
                        continue
                    source_fixture_id = (match.get("fixture") or {}).get("id")
                    fixture_id = loader.api_fixture_id(source_fixture_id)
                    if not fixture_id:
                        continue
                    if phase == "lineups":
                        loader._load_api_lineups(
                            match.get("lineups") or [], source_fixture_id,
                            fixture_id, item,
                        )
                    else:
                        loader._load_api_events(
                            match.get("events") or [], source_fixture_id,
                            fixture_id, item,
                        )
                    item_matches += 1
            else:
                source_fixture_id = (item.get("request_parameters") or {}).get("fixture")
                fixture_id = loader.api_fixture_id(source_fixture_id)
                if fixture_id:
                    if phase == "lineups":
                        loader._load_api_lineups(response, source_fixture_id, fixture_id, item)
                    else:
                        loader._load_api_events(response, source_fixture_id, fixture_id, item)
                    item_matches += 1
            warehouse.connection.execute(
                """
                INSERT INTO api_player_link_repair_checkpoint
                    (phase, resource_name, content_sha256, raw_artifact_id,
                     match_operations, completed_at)
                VALUES (?, ?, ?, ?, ?, now())
                """,
                [phase, resource, item["content_sha256"],
                 item["_raw_artifact_id"], item_matches],
            )
        replayed_matches += item_matches
        if index == 1 or index % 25 == 0 or index == len(selected):
            print(
                f"Progress: {index:,}/{len(selected):,} selected checkpoints "
                f"completed in this run ({phase}: {resource}).",
                flush=True,
            )
    return (
        len(unique_items), len(selected), replayed_matches,
        len(pending) - len(selected),
    )


def write_review_report(warehouse: Warehouse, metrics: dict[str, int]) -> Path:
    rows = warehouse.connection.execute(
        """
        SELECT sem.source_entity_id, f.scheduled_kickoff, home_team.name,
               away_team.name,
               t.name, p.full_name, pm.minutes_played, pm.shirt_number,
               string_agg(
                   DISTINCT lp_name.full_name || coalesce(' #' || lp.shirt_number::VARCHAR, ''),
                   ', ' ORDER BY lp_name.full_name || coalesce(' #' || lp.shirt_number::VARCHAR, '')
               ) AS lineup_candidates
        FROM player_match_stat_observation pm
        JOIN player p USING (player_id)
        JOIN fixture f USING (fixture_id)
        JOIN team home_team ON home_team.team_id=f.home_team_id
        JOIN team away_team ON away_team.team_id=f.away_team_id
        JOIN team t ON t.team_id=pm.team_id
        JOIN source_entity_map sem
          ON sem.internal_entity_id=f.fixture_id
         AND sem.source_code='api_football' AND sem.entity_type='fixture'
        JOIN lineup_snapshot ls
          ON ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
         AND ls.source_code=pm.source_code AND ls.raw_artifact_id=pm.raw_artifact_id
        LEFT JOIN lineup_player lp ON lp.lineup_snapshot_id=ls.lineup_snapshot_id
        LEFT JOIN player lp_name ON lp_name.player_id=lp.player_id
        WHERE pm.source_code='api_football' AND pm.minutes_played>0
          AND NOT EXISTS (
              SELECT 1 FROM lineup_player exact
              WHERE exact.lineup_snapshot_id=ls.lineup_snapshot_id
                AND exact.player_id=pm.player_id
          )
        GROUP BY sem.source_entity_id, f.scheduled_kickoff,
                 home_team.name, away_team.name,
                 t.name, p.full_name, pm.minutes_played, pm.shirt_number
        ORDER BY f.scheduled_kickoff, sem.source_entity_id, t.name, p.full_name
        """
    ).fetchall()
    path = ROOT / "reports" / "API_PLAYER_LINK_REVIEW.md"
    lines = [
        "# API-Football Player-Link Review Queue", "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
        "These player-stat records were preserved but could not be linked confidently ",
        "to a provider lineup entry. They require manual review only if lineup/event ",
        "identity is needed; their player-match statistics remain usable.", "",
        "## Summary", "",
    ]
    lines.extend(f"- {key.replace('_', ' ').title()}: **{value:,}**" for key, value in metrics.items())
    lines.extend([
        "", "## Review rows", "",
        "| API fixture | Kickoff | Match | Team | Stat player | Minutes | Shirt | Lineup entries |",
        "|---:|---|---|---|---|---:|---:|---|",
    ])
    for api_id, kickoff, home, away, team, player, minutes, shirt, candidates in rows:
        safe = lambda value: str(value or "").replace("|", "\\|")
        lines.append(
            f"| {safe(api_id)} | {safe(kickoff)} | {safe(home)}–{safe(away)} | "
            f"{safe(team)} | {safe(player)} | {safe(minutes)} | {safe(shirt)} | "
            f"{safe(candidates)} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild API-Football lineup/event player links with evidence scoring"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="repair a disposable copy and atomically replace the live database",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="require an existing checkpointed working copy (normally auto-detected)",
    )
    parser.add_argument(
        "--max-checkpoints", type=int,
        help="stop safely after this many artifact-phase checkpoints",
    )
    return parser.parse_args()


def initialize_repair(warehouse: Warehouse) -> None:
    before_fingerprints = protected_fingerprints(warehouse)
    before_metrics = link_metrics(warehouse)
    with warehouse.transaction():
        warehouse.connection.execute(
            """
            CREATE TABLE api_player_link_repair_metadata (
                key VARCHAR PRIMARY KEY,
                value JSON NOT NULL
            )
            """
        )
        warehouse.connection.execute(
            """
            CREATE TABLE api_player_link_repair_checkpoint (
                phase VARCHAR NOT NULL,
                resource_name VARCHAR NOT NULL,
                content_sha256 VARCHAR NOT NULL,
                raw_artifact_id VARCHAR NOT NULL,
                match_operations INTEGER NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (phase, resource_name, content_sha256)
            )
            """
        )
        warehouse.connection.executemany(
            "INSERT INTO api_player_link_repair_metadata VALUES (?, ?)",
            [
                ("protected_fingerprints", json.dumps(before_fingerprints)),
                ("link_metrics_before", json.dumps(before_metrics)),
            ],
        )
        warehouse.connection.execute(
            """DELETE FROM lineup_player WHERE lineup_snapshot_id IN (
                   SELECT lineup_snapshot_id FROM lineup_snapshot
                   WHERE source_code='api_football')"""
        )
        warehouse.connection.execute(
            "DELETE FROM lineup_snapshot WHERE source_code='api_football'"
        )
        warehouse.connection.execute(
            "DELETE FROM match_event WHERE source_code='api_football'"
        )
        warehouse.connection.execute(
            """DELETE FROM source_entity_map
               WHERE source_code IN ('api_football_lineup', 'api_football_event')
                 AND entity_type='player'"""
        )


def repair_metadata(warehouse: Warehouse, key: str):
    row = warehouse.connection.execute(
        "SELECT value FROM api_player_link_repair_metadata WHERE key=?", [key]
    ).fetchone()
    if not row:
        raise RuntimeError(f"Missing repair metadata: {key}")
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def main() -> int:
    args = parse_args()
    database_path = ROOT / "data" / "warehouse" / "soccer.duckdb"
    backup_path = ROOT / "data" / "warehouse" / "soccer.pre_evidence_link_repair.duckdb"
    working_path = ROOT / "data" / "warehouse" / "soccer.evidence_link_repair.working.duckdb"

    if not args.apply:
        warehouse = Warehouse(
            database_path, ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
        )
        try:
            print(json.dumps({
                "mode": "dry_run",
                "protected_fingerprints": protected_fingerprints(warehouse),
                "link_metrics_before": link_metrics(warehouse),
                "eligible_raw_artifacts": len(allowed_raw_artifact_ids(warehouse)),
            }, indent=2, sort_keys=True))
            return 0
        finally:
            warehouse.close()

    resuming = working_path.exists()
    if args.resume and not resuming:
        raise RuntimeError(
            f"Cannot resume because the working database is missing: {working_path}"
        )
    if resuming:
        print(f"Resuming checkpointed repair: {working_path}", flush=True)
    else:
        if backup_path.exists():
            if not filecmp.cmp(database_path, backup_path, shallow=False):
                raise RuntimeError(
                    "The existing pre-repair backup differs from the live database. "
                    "Refusing to start without manual review."
                )
        else:
            shutil.copy2(database_path, backup_path)
        shutil.copy2(database_path, working_path)
        print(f"Created isolated working database: {working_path}", flush=True)

    warehouse = Warehouse(
        working_path, ROOT / "migrations", ROOT / "config" / "entity_aliases.json"
    )
    succeeded = False
    try:
        if not resuming:
            print("Initializing repair and protecting baseline fingerprints...", flush=True)
            initialize_repair(warehouse)
        before_fingerprints = repair_metadata(warehouse, "protected_fingerprints")
        before_metrics = repair_metadata(warehouse, "link_metrics_before")
        artifacts, checkpoints, matches, remaining = replay_links(
            warehouse, args.max_checkpoints
        )
        if remaining:
            print(
                f"Paused safely with {remaining:,} checkpoints remaining. "
                "Run the same command to resume.",
                flush=True,
            )
            return 0
        print("Replay complete. Running warehouse quality checks...", flush=True)
        run_quality_checks(warehouse)
        after_fingerprints = protected_fingerprints(warehouse)
        after_metrics = link_metrics(warehouse)
        blocking = warehouse.connection.execute(
            """SELECT count(*) FROM data_quality_issue
               WHERE status='open' AND severity='blocking'"""
        ).fetchone()[0]
        collisions = warehouse.connection.execute(
            """SELECT count(*) FROM (
                   SELECT internal_entity_id FROM source_entity_map
                   WHERE source_code='api_football' AND entity_type='player'
                   GROUP BY internal_entity_id
                   HAVING count(DISTINCT source_entity_id)>1)"""
        ).fetchone()[0]
        normalized_after = {
            table: list(value) for table, value in after_fingerprints.items()
        }
        if before_fingerprints != normalized_after:
            changed = [
                table for table in PROTECTED_TABLES
                if before_fingerprints[table] != normalized_after[table]
            ]
            raise RuntimeError(f"Protected tables changed during repair: {changed}")
        if (
            blocking or collisions
            or after_metrics["bad_starter_snapshots"]
               > before_metrics["bad_starter_snapshots"]
        ):
            raise RuntimeError(
                f"Repair validation failed: blocking={blocking}, "
                f"collisions={collisions}, "
                f"bad_starters_before={before_metrics['bad_starter_snapshots']}, "
                f"bad_starters_after={after_metrics['bad_starter_snapshots']}"
            )
        report = write_review_report(warehouse, after_metrics)
        warehouse.connection.execute("DROP TABLE api_player_link_repair_checkpoint")
        warehouse.connection.execute("DROP TABLE api_player_link_repair_metadata")
        print(json.dumps({
            "mode": "apply",
            "replayed_raw_artifacts": artifacts,
            "checkpoints_completed_this_run": checkpoints,
            "replayed_matches": matches,
            "protected_fingerprints_unchanged": True,
            "link_metrics_before": before_metrics,
            "link_metrics_after": after_metrics,
            "blocking_quality_issues": blocking,
            "api_player_identity_collisions": collisions,
            "bad_starter_snapshots": after_metrics["bad_starter_snapshots"],
            "backup": str(backup_path.relative_to(ROOT)),
            "review_report": str(report.relative_to(ROOT)),
        }, indent=2, sort_keys=True))
        succeeded = True
        return 0
    finally:
        warehouse.close()
        if succeeded:
            os.replace(working_path, database_path)
        elif args.apply:
            print(
                f"Repair did not finish; preserved resumable working copy: {working_path}",
                file=sys.stderr, flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
