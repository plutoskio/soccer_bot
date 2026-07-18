#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.players import (
    load_player_match_targets,
    write_player_target_artifact,
)
from soccer_bot.modeling.player_hierarchy import load_player_hierarchy_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze positive-exposure player targets without imputing minutes."
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
        "--output-dir", type=Path,
        default=ROOT / "data" / "features" / "confirmed_lineup_player_v1",
    )
    args = parser.parse_args()
    config = load_player_hierarchy_config(args.config)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        rows, audit = load_player_match_targets(
            connection,
            result_availability_delay_minutes=(
                config.result_availability_delay_minutes
            ),
            source_code=config.source_code,
            supported_positions=config.supported_positions,
            kickoff_end_exclusive=config.production_fit_end_exclusive,
        )
    finally:
        connection.close()
    manifest = write_player_target_artifact(
        rows,
        audit,
        output_dir=args.output_dir,
        warehouse_path=args.warehouse,
        source_files={"model_config": args.config},
    )
    print(json.dumps(manifest["dataset"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
