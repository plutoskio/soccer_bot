#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.players import (
    load_first_valid_confirmed_lineups,
    load_player_match_targets,
)
from soccer_bot.modeling.player_hierarchy import load_player_hierarchy_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit timestamp-safe confirmed-lineup/player modeling readiness."
    )
    parser.add_argument(
        "--warehouse", type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "config" / "models" / "confirmed_lineup_player_v1.json",
    )
    parser.add_argument(
        "--as-of", default=None,
        help="Timezone-aware ISO timestamp; defaults to now.",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "data" / "reports" / "players" / "readiness.json",
    )
    args = parser.parse_args()
    as_of = datetime.now(timezone.utc) if args.as_of is None else datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        raise ValueError("--as-of requires a timezone")
    config = load_player_hierarchy_config(args.config)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        targets, target_audit = load_player_match_targets(
            connection,
            result_availability_delay_minutes=config.result_availability_delay_minutes,
            source_code=config.source_code,
            supported_positions=config.supported_positions,
            kickoff_end_exclusive=config.production_fit_end_exclusive,
        )
        lineups = load_first_valid_confirmed_lineups(connection, as_of=as_of)
        lineup_counts = connection.execute(
            """
            SELECT
                count(DISTINCT fixture_id),
                count(*) FILTER (WHERE captured_before_kickoff),
                count(*) FILTER (WHERE captured_before_kickoff=false),
                count(*) FILTER (WHERE captured_before_kickoff IS NULL)
            FROM lineup_snapshot
            WHERE lineup_type='confirmed'
            """
        ).fetchone()
        substitute_mismatch = connection.execute(
            """
            WITH lineup AS (
                SELECT DISTINCT ls.fixture_id, ls.team_id, lp.player_id
                FROM lineup_snapshot ls
                JOIN lineup_player lp USING (lineup_snapshot_id)
                JOIN fixture_model_eligibility e USING (fixture_id)
                WHERE e.eligible_player_models
                  AND ls.lineup_type='confirmed'
                  AND lp.selection_role='substitute'
            ), incoming AS (
                SELECT DISTINCT fixture_id, team_id,
                       secondary_player_id AS player_id
                FROM match_event
                WHERE event_type='subst' AND secondary_player_id IS NOT NULL
            )
            SELECT
                count(*),
                count(*) FILTER (WHERE p.minutes_played IS NULL),
                count(*) FILTER (
                    WHERE p.minutes_played IS NULL AND i.player_id IS NOT NULL
                ),
                count(*) FILTER (
                    WHERE p.minutes_played > 0 AND i.player_id IS NULL
                )
            FROM lineup l
            LEFT JOIN player_match_stat_observation p
              USING (fixture_id, team_id, player_id)
            LEFT JOIN incoming i USING (fixture_id, team_id, player_id)
            """
        ).fetchone()
    finally:
        connection.close()
    report = {
        "report_version": "confirmed_lineup_player_readiness_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "warehouse": str(args.warehouse.resolve()),
        "player_targets": target_audit,
        "confirmed_lineups": {
            "all_historical_fixture_rows": lineup_counts[0],
            "team_snapshots_marked_pregame": lineup_counts[1],
            "team_snapshots_marked_postkickoff": lineup_counts[2],
            "team_snapshots_without_capture_classification": lineup_counts[3],
            "strict_two_team_timestamp_safe_fixtures": len(lineups),
            "historical_evaluation_eligible": False,
        },
        "substitute_appearance_semantics": {
            "bench_rows": substitute_mismatch[0],
            "missing_minutes_rows": substitute_mismatch[1],
            "missing_minutes_despite_incoming_event": substitute_mismatch[2],
            "positive_minutes_without_incoming_event": substitute_mismatch[3],
            "unconditional_substitute_props_enabled": False,
        },
        "decisions": {
            "starter_minutes_goal_assist_components": "research_enabled",
            "confirmed_lineup_predictions": "prospective_shadow_only",
            "historical_confirmed_lineup_backtest": "blocked_timestamp_leakage",
            "substitute_appearance_model": "blocked_provider_semantics",
            "first_scorer": "blocked_requires_event_time_model",
            "champion_team_rate_replacement": "blocked_prospective_gate",
        },
        "target_rows_loaded": len(targets),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
