#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import write_corner_feature_artifact
from soccer_bot.datasets.corner_features import (
    ChronologicalCornerFeatureBuilder,
    load_corner_feature_config,
)
from soccer_bot.datasets.corners import build_corner_targets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze the leakage-safe joint-corner modeling dataset."
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "models" / "joint_corners_v1.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "features" / "corner_team_state_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        target_build = build_corner_targets(connection)
    finally:
        connection.close()
    config = load_corner_feature_config(args.config)
    rows = ChronologicalCornerFeatureBuilder(config).build(list(target_build.targets))
    manifest = write_corner_feature_artifact(
        rows,
        output_dir=args.output_dir,
        warehouse_path=args.warehouse,
        source_files={"corner_model_configuration": args.config},
        target_conflicts=len(target_build.conflicts),
    )
    print(
        json.dumps(
            {
                "dataset": manifest["dataset"]["path"],
                "safe_target_fixtures": len(target_build.targets),
                "conflicting_targets_excluded": len(target_build.conflicts),
                "feature_fixtures": manifest["dataset"]["fixtures"],
                "horizon_rows": manifest["dataset"]["horizon_rows"],
                "logical_rows_sha256": manifest["dataset"]["logical_rows_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
