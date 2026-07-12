#!/usr/bin/env python3
"""One-time repair for seven proven API-Football player-block swaps.

This script is intentionally fixture-specific. It does not alter collector,
loader, or backfill behavior. Original raw artifacts remain immutable.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.database import Warehouse, json_text, stable_id
from soccer_bot.loaders import RawCatalog, WarehouseLoader
from scripts.build_database import run_quality_checks


# The fingerprint includes immutable raw content plus exact provider-ID overlap
# counts. A changed response or a different anomaly shape aborts the repair.
KNOWN_SWAPS = {
    "1400804": {
        "sha256": "5847192c6f3d35f2dc65ef86d6e32247c99647be73dc817a87b0b30c3b5cd4bf",
        "teams": ["949", "953"],
        "cross_overlaps": {"949": 23, "953": 19},
    },
    "1400807": {
        "sha256": "11641bbfcb726fd5f5b7b2c5d735ce18b41e6bc399e52a55e4b6101de7c7dd92",
        "teams": ["5050", "553"],
        "cross_overlaps": {"5050": 23, "553": 17},
    },
    "1400806": {
        "sha256": "11641bbfcb726fd5f5b7b2c5d735ce18b41e6bc399e52a55e4b6101de7c7dd92",
        "teams": ["957", "2099"],
        "cross_overlaps": {"957": 23, "2099": 21},
    },
    "1400809": {
        "sha256": "11641bbfcb726fd5f5b7b2c5d735ce18b41e6bc399e52a55e4b6101de7c7dd92",
        "teams": ["1124", "575"],
        "cross_overlaps": {"1124": 22, "575": 23},
    },
    "1400805": {
        "sha256": "11641bbfcb726fd5f5b7b2c5d735ce18b41e6bc399e52a55e4b6101de7c7dd92",
        "teams": ["1123", "955"],
        "cross_overlaps": {"1123": 19, "955": 21},
    },
    "1400803": {
        "sha256": "11641bbfcb726fd5f5b7b2c5d735ce18b41e6bc399e52a55e4b6101de7c7dd92",
        "teams": ["12260", "2110"],
        "cross_overlaps": {"12260": 18, "2110": 18},
    },
    "1488554": {
        "sha256": "d97dbbf5c6cf84b694fb5488626a4c38b67429a622888eacea8a16c39691d687",
        "teams": ["757", "2142"],
        "cross_overlaps": {"757": 16, "2142": 15},
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair the seven known swapped API-Football player blocks"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Only for an expendable test copy",
    )
    return parser.parse_args()


def read_raw(path: Path, expected_sha256: str) -> dict:
    body = gzip.open(path, "rb").read() if path.suffix == ".gz" else path.read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            f"Raw artifact hash mismatch: expected {expected_sha256}, found {actual}"
        )
    return json.loads(body)


def player_ids_from_lineup(lineup: dict) -> set[str]:
    return {
        str(player["player"]["id"])
        for field in ("startXI", "substitutes")
        for player in (lineup.get(field) or [])
        if (player.get("player") or {}).get("id") is not None
    }


def player_ids_from_block(block: dict) -> set[str]:
    return {
        str(player["player"]["id"])
        for player in (block.get("players") or [])
        if (player.get("player") or {}).get("id") is not None
    }


def validate_swap_evidence(match: dict, expected: dict) -> dict:
    fixture_id = str((match.get("fixture") or {}).get("id"))
    fixture_teams = {
        str(team["id"]): team
        for team in (match.get("teams") or {}).values()
        if team and team.get("id") is not None
    }
    lineups = {
        str(lineup["team"]["id"]): lineup
        for lineup in (match.get("lineups") or [])
    }
    blocks = {
        str(block["team"]["id"]): block
        for block in (match.get("players") or [])
    }
    expected_teams = set(expected["teams"])
    if set(fixture_teams) != expected_teams:
        raise RuntimeError(f"Fixture {fixture_id}: unexpected fixture teams")
    if set(lineups) != expected_teams or set(blocks) != expected_teams:
        raise RuntimeError(f"Fixture {fixture_id}: expected two lineup and player blocks")
    if any(len(lineup.get("startXI") or []) != 11 for lineup in lineups.values()):
        raise RuntimeError(f"Fixture {fixture_id}: incomplete starting lineup")

    evidence = {}
    teams = expected["teams"]
    for team_id in teams:
        other_id = teams[1] if team_id == teams[0] else teams[0]
        lineup_ids = player_ids_from_lineup(lineups[team_id])
        same_ids = player_ids_from_block(blocks[team_id])
        other_ids = player_ids_from_block(blocks[other_id])
        same_overlap = len(lineup_ids & same_ids)
        cross_overlap = len(lineup_ids & other_ids)
        coverage = cross_overlap / min(len(lineup_ids), len(other_ids))
        if same_overlap != 0:
            raise RuntimeError(
                f"Fixture {fixture_id}: team {team_id} has {same_overlap} same-block matches"
            )
        if cross_overlap != expected["cross_overlaps"][team_id]:
            raise RuntimeError(
                f"Fixture {fixture_id}: team {team_id} cross-overlap changed "
                f"from {expected['cross_overlaps'][team_id]} to {cross_overlap}"
            )
        if cross_overlap < 11 or coverage < 0.70:
            raise RuntimeError(f"Fixture {fixture_id}: cross-team evidence is insufficient")
        evidence[team_id] = {
            "same_exact_id_overlap": same_overlap,
            "cross_exact_id_overlap": cross_overlap,
            "cross_coverage_of_smaller_roster": coverage,
        }

    first_ids = player_ids_from_block(blocks[teams[0]])
    second_ids = player_ids_from_block(blocks[teams[1]])
    if first_ids & second_ids:
        raise RuntimeError(f"Fixture {fixture_id}: player blocks overlap")
    return evidence


def corrected_player_blocks(match: dict, expected: dict) -> list[dict]:
    blocks = {
        str(block["team"]["id"]): deepcopy(block)
        for block in (match.get("players") or [])
    }
    lineups = {
        str(lineup["team"]["id"]): lineup
        for lineup in (match.get("lineups") or [])
    }
    first, second = expected["teams"]
    blocks[first]["team"] = deepcopy(lineups[second]["team"])
    blocks[second]["team"] = deepcopy(lineups[first]["team"])
    return [blocks[first], blocks[second]]


def rows(connection, query: str, parameters: list[str]) -> list[tuple]:
    return connection.execute(query, parameters).fetchall()


def snapshot_fixture(connection, fixture_id: str) -> dict:
    return {
        "player_stats": rows(
            connection,
            "SELECT * EXCLUDE(team_id) FROM player_match_stat_observation "
            "WHERE fixture_id=? ORDER BY observation_id",
            [fixture_id],
        ),
        "player_stat_teams": dict(rows(
            connection,
            "SELECT observation_id, team_id FROM player_match_stat_observation "
            "WHERE fixture_id=? ORDER BY observation_id",
            [fixture_id],
        )),
        "appearances": rows(
            connection,
            "SELECT * EXCLUDE(team_id) FROM appearance WHERE fixture_id=? "
            "ORDER BY appearance_id",
            [fixture_id],
        ),
        "appearance_teams": dict(rows(
            connection,
            "SELECT appearance_id, team_id FROM appearance WHERE fixture_id=? "
            "ORDER BY appearance_id",
            [fixture_id],
        )),
        "lineup_facts": rows(
            connection,
            """SELECT ls.team_id, lp.selection_role, lp.position_code,
                      lp.formation_grid, lp.shirt_number, lp.captain, lp.goalkeeper
               FROM lineup_snapshot ls JOIN lineup_player lp USING(lineup_snapshot_id)
               WHERE ls.fixture_id=? ORDER BY 1,2,3,4,5""",
            [fixture_id],
        ),
        "events_without_players": rows(
            connection,
            "SELECT * EXCLUDE(player_id, secondary_player_id) FROM match_event "
            "WHERE fixture_id=? ORDER BY match_event_id",
            [fixture_id],
        ),
        "results": rows(
            connection,
            "SELECT * FROM fixture_result_observation WHERE fixture_id=? "
            "ORDER BY observation_id",
            [fixture_id],
        ),
        "team_stats": rows(
            connection,
            "SELECT * FROM team_match_stat_observation WHERE fixture_id=? "
            "ORDER BY observation_id",
            [fixture_id],
        ),
    }


def unmatched_participants(connection, fixture_id: str, raw_artifact_id: str) -> int:
    return connection.execute(
        """
        SELECT count(*)
        FROM player_match_stat_observation pm
        WHERE pm.fixture_id=? AND pm.source_code='api_football'
          AND pm.raw_artifact_id=? AND pm.minutes_played>0
          AND NOT EXISTS (
              SELECT 1 FROM lineup_snapshot ls
              JOIN lineup_player lp USING(lineup_snapshot_id)
              WHERE ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
                AND ls.source_code=pm.source_code
                AND ls.raw_artifact_id=pm.raw_artifact_id
                AND lp.player_id=pm.player_id
          )
        """,
        [fixture_id, raw_artifact_id],
    ).fetchone()[0]


def verify_after(
    connection,
    fixture_id: str,
    raw_artifact_id: str,
    before: dict,
    team_ids: list[str],
) -> dict:
    after = snapshot_fixture(connection, fixture_id)
    for key in (
        "player_stats", "appearances", "lineup_facts", "events_without_players",
        "results", "team_stats",
    ):
        if before[key] != after[key]:
            raise RuntimeError(f"Fixture {fixture_id}: invariant changed: {key}")

    internal_teams = []
    for source_team_id in team_ids:
        row = connection.execute(
            """SELECT internal_entity_id FROM source_entity_map
               WHERE source_code='api_football' AND entity_type='team'
                 AND source_entity_id=?""",
            [source_team_id],
        ).fetchone()
        if not row:
            raise RuntimeError(f"Missing team mapping {source_team_id}")
        internal_teams.append(row[0])
    opposite = {internal_teams[0]: internal_teams[1], internal_teams[1]: internal_teams[0]}
    for key in ("player_stat_teams", "appearance_teams"):
        if set(before[key]) != set(after[key]):
            raise RuntimeError(f"Fixture {fixture_id}: row identity changed: {key}")
        for row_id, old_team in before[key].items():
            if after[key][row_id] != opposite[old_team]:
                raise RuntimeError(f"Fixture {fixture_id}: {key} was not exactly reversed")

    unmatched = unmatched_participants(connection, fixture_id, raw_artifact_id)
    if unmatched:
        raise RuntimeError(
            f"Fixture {fixture_id}: {unmatched} participating players remain unlinked"
        )
    duplicate_players = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT player_id FROM player_match_stat_observation
            WHERE fixture_id=? AND source_code='api_football'
            GROUP BY player_id HAVING count(DISTINCT team_id)>1
        )
        """,
        [fixture_id],
    ).fetchone()[0]
    if duplicate_players:
        raise RuntimeError(f"Fixture {fixture_id}: player appears for both teams")
    return {
        "player_rows": len(after["player_stats"]),
        "appearance_rows": len(after["appearances"]),
        "lineup_rows": len(after["lineup_facts"]),
        "event_rows": len(after["events_without_players"]),
        "unmatched_participants": unmatched,
    }


def backup_database(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.pre_player_block_repair_{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(backup.read_bytes()).digest():
        raise RuntimeError("Backup verification failed")
    return backup


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    if not database.exists():
        raise FileNotFoundError(database)
    if args.no_backup and database == (ROOT / "data" / "warehouse" / "soccer.duckdb").resolve():
        raise RuntimeError("--no-backup is forbidden for the live database")

    backup = None
    if args.execute and not args.no_backup:
        backup = backup_database(database)

    warehouse = Warehouse(database, ROOT / "migrations", ROOT / "config" / "entity_aliases.json")
    report = {"mode": "execute" if args.execute else "dry_run", "fixtures": []}
    try:
        connection = warehouse.connection
        loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
        loader.enable_api_backfill_identity_cache()
        prepared = []
        for api_fixture_id, expected in KNOWN_SWAPS.items():
            mapping = connection.execute(
                """SELECT internal_entity_id FROM source_entity_map
                   WHERE source_code='api_football' AND entity_type='fixture'
                     AND source_entity_id=?""",
                [api_fixture_id],
            ).fetchall()
            if len(mapping) != 1:
                raise RuntimeError(f"Fixture {api_fixture_id}: expected one mapping")
            fixture_id = mapping[0][0]
            artifact = connection.execute(
                """
                SELECT DISTINCT ra.raw_artifact_id, ra.content_sha256, ra.data_path,
                                ra.retrieved_at
                FROM player_match_stat_observation pm
                JOIN raw_artifact ra USING(raw_artifact_id)
                WHERE pm.fixture_id=? AND pm.source_code='api_football'
                  AND ra.content_sha256=?
                """,
                [fixture_id, expected["sha256"]],
            ).fetchall()
            if len(artifact) != 1:
                raise RuntimeError(f"Fixture {api_fixture_id}: expected one raw artifact")
            raw_artifact_id, content_sha256, data_path, retrieved_at = artifact[0]
            payload = read_raw(Path(data_path), content_sha256)
            matches = [
                match for match in payload.get("response", [])
                if str((match.get("fixture") or {}).get("id")) == api_fixture_id
            ]
            if len(matches) != 1:
                raise RuntimeError(f"Fixture {api_fixture_id}: raw response match count changed")
            match = matches[0]
            evidence = validate_swap_evidence(match, expected)
            prepared.append({
                "api_fixture_id": api_fixture_id,
                "fixture_id": fixture_id,
                "raw_artifact_id": raw_artifact_id,
                "match": match,
                "expected": expected,
                "item": {
                    "_raw_artifact_id": raw_artifact_id,
                    "content_sha256": content_sha256,
                    "retrieved_at": retrieved_at.isoformat(),
                },
                "before": snapshot_fixture(connection, fixture_id),
                "evidence": evidence,
            })
            report["fixtures"].append({
                "api_fixture_id": api_fixture_id,
                "evidence": evidence,
                "unmatched_before": unmatched_participants(
                    connection, fixture_id, raw_artifact_id
                ),
            })

        if not args.execute:
            print(json.dumps(report, indent=2, default=str))
            return 0

        connection.execute("BEGIN TRANSACTION")
        try:
            for entry in prepared:
                api_fixture_id = entry["api_fixture_id"]
                fixture_id = entry["fixture_id"]
                match = entry["match"]
                loader._load_api_players(
                    corrected_player_blocks(match, entry["expected"]),
                    api_fixture_id,
                    fixture_id,
                    entry["item"],
                )
                loader._load_api_lineups(
                    match.get("lineups") or [], api_fixture_id, fixture_id, entry["item"]
                )
                loader._load_api_events(
                    match.get("events") or [], api_fixture_id, fixture_id, entry["item"]
                )
                result = verify_after(
                    connection,
                    fixture_id,
                    entry["raw_artifact_id"],
                    entry["before"],
                    entry["expected"]["teams"],
                )
                report_entry = next(
                    item for item in report["fixtures"]
                    if item["api_fixture_id"] == api_fixture_id
                )
                report_entry.update(result)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO data_quality_issue (
                        issue_id, rule_code, severity, entity_type,
                        internal_entity_id, source_code, raw_artifact_id,
                        details, detected_at, status
                    ) VALUES (?, 'api_player_team_blocks_swapped_corrected',
                              'warning', 'fixture', ?, 'api_football', ?, ?, ?, 'resolved')
                    """,
                    [
                        stable_id("quality_issue", "api_player_team_blocks_swapped_corrected", fixture_id),
                        fixture_id,
                        entry["raw_artifact_id"],
                        json_text({
                            "message": "Provider player-stat team blocks were reversed using exact player-ID evidence",
                            "api_fixture_id": api_fixture_id,
                            "evidence": entry["evidence"],
                        }),
                        datetime.now(timezone.utc),
                    ],
                )
            run_quality_checks(warehouse)
            blocking = connection.execute(
                """SELECT count(*) FROM data_quality_issue
                   WHERE status='open' AND severity='blocking'"""
            ).fetchone()[0]
            if blocking:
                raise RuntimeError(f"Repair produced {blocking} blocking quality issues")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        report["backup"] = str(backup) if backup else None
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
