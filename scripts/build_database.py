#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.database import Warehouse, json_text, stable_id
from soccer_bot.loaders import RawCatalog, WarehouseLoader


COUNT_TABLES = [
    "raw_artifact",
    "competition",
    "season",
    "team",
    "player",
    "source_entity_map",
    "fixture",
    "fixture_result_observation",
    "lineup_snapshot",
    "lineup_player",
    "appearance",
    "match_event",
    "team_match_stat_observation",
    "player_match_stat_observation",
    "player_season_stat",
    "bookmaker_quote",
    "prediction_market_event",
    "prediction_market",
    "prediction_market_outcome",
    "orderbook_snapshot",
    "orderbook_level",
    "market_price_history",
    "data_quality_issue",
]


def collect_counts(warehouse: Warehouse) -> dict[str, int]:
    return {
        table: warehouse.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in COUNT_TABLES
    }


def run_quality_checks(
    warehouse: Warehouse, *, passing_coverage_warning_threshold: float = 0.8
) -> None:
    if not 0 <= passing_coverage_warning_threshold <= 1:
        raise ValueError("passing coverage warning threshold must be between 0 and 1")
    connection = warehouse.connection
    # Quality issues describe the latest evaluated warehouse state. Resolve the
    # previous snapshot first; any issue that still exists is reopened below.
    connection.execute(
        "UPDATE data_quality_issue SET status = 'resolved' WHERE status = 'open'"
    )
    checks = [
        (
            "fixture_same_team",
            "blocking",
            "fixture",
            "SELECT fixture_id FROM fixture WHERE home_team_id = away_team_id",
            "Home and away team are identical",
        ),
        (
            "completed_without_result",
            "warning",
            "fixture",
            """
            SELECT f.fixture_id FROM fixture f
            LEFT JOIN fixture_result_observation r ON r.fixture_id = f.fixture_id
            WHERE f.status = 'completed' AND r.fixture_id IS NULL
            """,
            "Completed fixture has no result observation",
        ),
        (
            "confirmed_lineup_not_eleven",
            "warning",
            "lineup_snapshot",
            """
            SELECT s.lineup_snapshot_id
            FROM lineup_snapshot s
            LEFT JOIN lineup_player p ON p.lineup_snapshot_id = s.lineup_snapshot_id
            WHERE s.lineup_type = 'confirmed'
            GROUP BY s.lineup_snapshot_id
            HAVING count(*) FILTER (WHERE p.selection_role = 'starter') != 11
            """,
            "Confirmed lineup does not have exactly eleven starters",
        ),
        (
            "negative_match_stat",
            "blocking",
            "team_match_stat_observation",
            """
            SELECT observation_id FROM team_match_stat_observation
            WHERE shots < 0 OR shots_on_target < 0 OR corners < 0 OR fouls < 0
            """,
            "A count statistic is negative",
        ),
        (
            "invalid_player_match_stat",
            "blocking",
            "player_match_stat_observation",
            """
            SELECT observation_id FROM player_match_stat_observation
            WHERE minutes_played < 0 OR minutes_played > 130
               OR pass_accuracy_pct < 0 OR pass_accuracy_pct > 100
               OR accurate_passes > passes
               OR rating < 0 OR rating > 10
               OR tackles < 0 OR interceptions < 0
               OR duels < 0 OR duels_won < 0 OR duels_won > duels
               OR dribbles_attempted < 0 OR dribbles_successful < 0
               OR dribbles_successful > dribbles_attempted
               OR fouls_drawn < 0 OR fouls_committed < 0
            """,
            "Player match statistic is outside its valid range",
        ),
        (
            "low_player_passing_coverage",
            "warning",
            "fixture",
            f"""
            SELECT fixture_id
            FROM player_match_stat_observation
            WHERE source_code = 'api_football' AND minutes_played > 0
            GROUP BY fixture_id
            HAVING count(*) FILTER (
                       WHERE passes IS NOT NULL AND accurate_passes IS NOT NULL
                   ) < {passing_coverage_warning_threshold} * count(*)
            """,
            f"Fewer than {passing_coverage_warning_threshold:.0%} of participating "
            "players have complete passing data; "
            "the fixture remains usable for features that do not require passing data",
        ),
        (
            "api_administrative_result_unplayed",
            "warning",
            "fixture",
            """
            SELECT DISTINCT result.fixture_id
            FROM fixture_result_observation result
            JOIN fixture f ON f.fixture_id=result.fixture_id
            WHERE result.source_code='api_football'
              AND f.status='administrative_result_unplayed'
              AND NOT EXISTS (
                  SELECT 1 FROM lineup_snapshot ls
                  WHERE ls.fixture_id=result.fixture_id
                    AND ls.source_code=result.source_code
                    AND ls.raw_artifact_id=result.raw_artifact_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM match_event event
                  WHERE event.fixture_id=result.fixture_id
                    AND event.source_code=result.source_code
                    AND event.raw_artifact_id=result.raw_artifact_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM team_match_stat_observation tm
                  WHERE tm.fixture_id=result.fixture_id
                    AND tm.source_code=result.source_code
                    AND tm.raw_artifact_id=result.raw_artifact_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM player_match_stat_observation pm
                  WHERE pm.fixture_id=result.fixture_id
                    AND pm.source_code=result.source_code
                    AND pm.raw_artifact_id=result.raw_artifact_id
              )
            """,
            "Official administrative result for a match that was not played; "
            "exclude this fixture from all sporting-performance model training",
        ),
        (
            "api_team_stats_unavailable",
            "warning",
            "fixture",
            """
            SELECT DISTINCT ls.fixture_id
            FROM lineup_snapshot ls
            JOIN fixture_result_observation result
              ON result.fixture_id=ls.fixture_id
             AND result.source_code=ls.source_code
             AND result.raw_artifact_id=ls.raw_artifact_id
            WHERE ls.source_code='api_football'
              AND NOT EXISTS (
                  SELECT 1 FROM team_match_stat_observation tm
                  WHERE tm.fixture_id=ls.fixture_id
                    AND tm.source_code=ls.source_code
                    AND tm.raw_artifact_id=ls.raw_artifact_id
              )
            """,
            "API-Football supplied the result and complete lineups but no "
            "team-match statistics; exclude this fixture from team-stat and "
            "corner features",
        ),
        (
            "api_player_stats_unavailable",
            "warning",
            "fixture",
            """
            SELECT DISTINCT ls.fixture_id
            FROM lineup_snapshot ls
            JOIN fixture_result_observation result
              ON result.fixture_id=ls.fixture_id
             AND result.source_code=ls.source_code
             AND result.raw_artifact_id=ls.raw_artifact_id
            WHERE ls.source_code='api_football'
              AND NOT EXISTS (
                  SELECT 1 FROM player_match_stat_observation pm
                  WHERE pm.fixture_id=ls.fixture_id
                    AND pm.source_code=ls.source_code
                    AND pm.raw_artifact_id=ls.raw_artifact_id
              )
            """,
            "API-Football supplied the result and complete lineups but no usable "
            "player-match statistics; exclude this fixture from player-level training",
        ),
        (
            "api_player_not_linked_to_lineup",
            "warning",
            "fixture",
            """
            SELECT DISTINCT pm.fixture_id
            FROM player_match_stat_observation pm
            JOIN lineup_snapshot ls
              ON ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
             AND ls.source_code=pm.source_code
             AND ls.raw_artifact_id=pm.raw_artifact_id
            WHERE pm.source_code='api_football' AND pm.minutes_played>0
              AND NOT EXISTS (
                  SELECT 1 FROM lineup_player lp
                  WHERE lp.lineup_snapshot_id=ls.lineup_snapshot_id
                    AND lp.player_id=pm.player_id
              )
            """,
            "At least one participating API-Football player could not be linked "
            "confidently to the provider lineup; player-match statistics remain usable",
        ),
        (
            "api_lineup_shirt_conflict",
            "warning",
            "fixture",
            """
            SELECT DISTINCT ls.fixture_id
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            JOIN player_match_stat_observation pm
              ON pm.fixture_id=ls.fixture_id AND pm.team_id=ls.team_id
             AND pm.player_id=lp.player_id AND pm.source_code=ls.source_code
             AND pm.raw_artifact_id=ls.raw_artifact_id
            WHERE ls.source_code='api_football'
              AND lp.shirt_number IS NOT NULL AND pm.shirt_number IS NOT NULL
              AND lp.shirt_number<>pm.shirt_number
            """,
            "A linked API-Football lineup and player-stat record disagree on shirt number",
        ),
        (
            "api_lineup_role_conflict",
            "warning",
            "fixture",
            """
            SELECT DISTINCT ls.fixture_id
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            JOIN player_match_stat_observation pm
              ON pm.fixture_id=ls.fixture_id AND pm.team_id=ls.team_id
             AND pm.player_id=lp.player_id AND pm.source_code=ls.source_code
             AND pm.raw_artifact_id=ls.raw_artifact_id
            WHERE ls.source_code='api_football' AND (
                (lp.selection_role='starter' AND pm.started=false)
                OR (lp.selection_role='substitute' AND pm.started=true)
              )
            """,
            "A linked API-Football lineup and player-stat record disagree on starter status",
        ),
    ]
    for rule_code, severity, entity_type, sql, message in checks:
        for (entity_id,) in connection.execute(sql).fetchall():
            issue_id = stable_id("quality_issue", rule_code, entity_id)
            connection.execute(
                """
                INSERT OR REPLACE INTO data_quality_issue (
                    issue_id, rule_code, severity, entity_type, internal_entity_id,
                    details, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'open')
                """,
                [issue_id, rule_code, severity, entity_type, entity_id, json_text({"message": message})],
            )

    # Recoverable provider anomalies are recorded in the immutable batch
    # validation JSON because the normalized lineup intentionally removes the
    # duplicate. Recreate their warnings during each global quality audit.
    for (raw_validation,) in connection.execute(
        """SELECT json_extract(validation, '$.raw')
           FROM historical_backfill_batch_checkpoint
           WHERE status='succeeded' AND validation IS NOT NULL"""
    ).fetchall():
        raw_validation = (
            json.loads(raw_validation)
            if isinstance(raw_validation, str) else raw_validation
        )
        for state in (raw_validation or {}).get("fixtures", []):
            duplicates = state.get("lineup_duplicate_entries") or []
            if not duplicates:
                continue
            mapping = connection.execute(
                """SELECT internal_entity_id FROM source_entity_map
                   WHERE source_code='api_football' AND entity_type='fixture'
                     AND source_entity_id=?""",
                [str(state.get("fixture_id"))],
            ).fetchone()
            if not mapping:
                continue
            entity_id = mapping[0]
            rule_code = "api_lineup_duplicate_entry"
            connection.execute(
                """
                INSERT OR REPLACE INTO data_quality_issue (
                    issue_id, rule_code, severity, entity_type,
                    internal_entity_id, details, status
                ) VALUES (?, ?, 'warning', 'fixture', ?, ?, 'open')
                """,
                [
                    stable_id("quality_issue", rule_code, entity_id),
                    rule_code,
                    entity_id,
                    json_text({
                        "message": "API-Football repeated lineup players; "
                                   "starter status was preserved",
                        "duplicates": duplicates,
                    }),
                ],
            )


def write_report(warehouse: Warehouse, counts: dict[str, int], output_path: Path) -> None:
    connection = warehouse.connection
    sources = connection.execute(
        """
        SELECT source_code, count(*)
        FROM raw_artifact
        GROUP BY source_code
        ORDER BY source_code
        """
    ).fetchall()
    fixtures_by_source = connection.execute(
        """
        SELECT source_code, count(*)
        FROM source_entity_map
        WHERE entity_type = 'fixture'
        GROUP BY source_code
        ORDER BY source_code
        """
    ).fetchall()
    issues = connection.execute(
        """
        SELECT severity, rule_code, count(*)
        FROM data_quality_issue
        WHERE status = 'open'
        GROUP BY severity, rule_code
        ORDER BY severity, rule_code
        """
    ).fetchall()
    readiness = [
        (
            "Fixtures with regulation scores",
            connection.execute(
                """
                SELECT count(DISTINCT fixture_id) FROM fixture_result_observation
                WHERE home_score_regulation IS NOT NULL AND away_score_regulation IS NOT NULL
                """
            ).fetchone()[0],
        ),
        (
            "Team-match rows with corners",
            connection.execute(
                "SELECT count(*) FROM team_match_stat_observation WHERE corners IS NOT NULL"
            ).fetchone()[0],
        ),
        (
            "Team-match rows with xG",
            connection.execute(
                "SELECT count(*) FROM team_match_stat_observation WHERE xg IS NOT NULL"
            ).fetchone()[0],
        ),
        (
            "Player-season rows with minutes, xG, and xA",
            connection.execute(
                """
                SELECT count(*) FROM player_season_stat
                WHERE minutes > 0 AND xg IS NOT NULL AND xa IS NOT NULL
                """
            ).fetchone()[0],
        ),
        ("Detailed match events", counts["match_event"]),
        ("Historical bookmaker quotes", counts["bookmaker_quote"]),
        (
            "Polymarket moneyline markets",
            connection.execute(
                "SELECT count(*) FROM prediction_market WHERE market_type = 'moneyline'"
            ).fetchone()[0],
        ),
        (
            "Polymarket spread markets",
            connection.execute(
                "SELECT count(*) FROM prediction_market WHERE market_type = 'spreads'"
            ).fetchone()[0],
        ),
        (
            "Polymarket exact-score markets",
            connection.execute(
                "SELECT count(*) FROM prediction_market WHERE market_type = 'soccer_exact_score'"
            ).fetchone()[0],
        ),
    ]
    lines = [
        "# Database Coverage Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Database: `data/warehouse/soccer.duckdb`",
        "",
        "## Canonical row counts",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ]
    lines.extend(f"| `{table}` | {count:,} |" for table, count in counts.items())
    lines.extend(
        [
            "",
            "## Raw artifacts by source",
            "",
            "| Source | Artifacts |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| `{source}` | {count:,} |" for source, count in sources)
    lines.extend(
        [
            "",
            "## Source fixture mappings",
            "",
            "| Source | Fixtures |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| `{source}` | {count:,} |" for source, count in fixtures_by_source)
    lines.extend(
        [
            "",
            "## Modeling-data readiness",
            "",
            "| Usable data slice | Rows |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| {label} | {count:,} |" for label, count in readiness)
    lines.extend(
        [
            "",
            "## Open quality issues",
            "",
            "| Severity | Rule | Count |",
            "|---|---|---:|",
        ]
    )
    if issues:
        lines.extend(f"| `{severity}` | `{rule}` | {count:,} |" for severity, rule, count in issues)
    else:
        lines.append("| — | — | 0 |")
    lines.extend(
        [
            "",
            "## Current interpretation",
            "",
            "This database is the initial canonical backfill from validated sample and bulk sources. Counts measure successfully normalized records, not complete global soccer coverage. Additional leagues/seasons and continuous upcoming-match collection will be appended idempotently.",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    database_path = ROOT / "data" / "warehouse" / "soccer.duckdb"
    warehouse = Warehouse(
        database_path,
        ROOT / "migrations",
        ROOT / "config" / "entity_aliases.json",
    )
    build_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    try:
        warehouse.migrate()
        warehouse.register_sources()
        warehouse.connection.execute(
            "INSERT INTO database_build (build_id, started_at, status) VALUES (?, ?, 'running')",
            [build_id, started_at],
        )
        catalog = RawCatalog(ROOT / "data" / "raw", warehouse)
        WarehouseLoader(warehouse, catalog).load_all()
        warehouse.reconcile_team_aliases()
        run_quality_checks(warehouse)
        counts = collect_counts(warehouse)
        warehouse.connection.execute(
            """
            UPDATE database_build
            SET finished_at = ?, status = 'completed', counts = ?
            WHERE build_id = ?
            """,
            [datetime.now(timezone.utc), json.dumps(counts), build_id],
        )
        write_report(
            warehouse, counts, ROOT / "reports" / "DATABASE_COVERAGE_REPORT.md"
        )
        for table, count in counts.items():
            print(f"{table}={count}")
        print("report=reports/DATABASE_COVERAGE_REPORT.md")
        return 0
    except Exception as error:
        try:
            try:
                warehouse.connection.execute("ROLLBACK")
            except Exception:
                pass
            warehouse.connection.execute(
                """
                UPDATE database_build
                SET finished_at = ?, status = 'failed', notes = ?
                WHERE build_id = ?
                """,
                [datetime.now(timezone.utc), f"{type(error).__name__}: {error}", build_id],
            )
        finally:
            warehouse.close()
        raise
    finally:
        try:
            warehouse.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
