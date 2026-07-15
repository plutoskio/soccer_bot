#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import write_regulation_feature_artifact
from soccer_bot.datasets.features import (
    ChronologicalTeamStateBuilder,
    load_team_state_feature_config,
)
from soccer_bot.datasets.targets import (
    build_regulation_score_targets,
    load_regulation_target_exclusions,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze the leakage-safe regulation modeling dataset."
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "features" / "regulation_team_state_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    feature_config_path = (
        ROOT / "config" / "features" / "regulation_team_state_v1.json"
    )
    model_config_path = ROOT / "config" / "models" / "regulation_score_v1.json"
    exclusions_path = (
        ROOT / "config" / "models" / "regulation_score_exclusions_v1.json"
    )
    contract_path = ROOT / "config" / "contracts" / "regulation_v1.json"

    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        targets = build_regulation_score_targets(
            connection,
            reviewed_exclusions=load_regulation_target_exclusions(exclusions_path),
        )
    finally:
        connection.close()
    config = load_team_state_feature_config(feature_config_path)
    rows = ChronologicalTeamStateBuilder(config).build(targets)
    manifest = write_regulation_feature_artifact(
        rows,
        output_dir=args.output_dir,
        warehouse_path=args.warehouse,
        source_files={
            "contract_registry": contract_path,
            "feature_configuration": feature_config_path,
            "target_exclusions": exclusions_path,
            "target_task": model_config_path,
        },
    )
    print(
        json.dumps(
            {
                "dataset": manifest["dataset"]["path"],
                "fixtures": manifest["dataset"]["fixtures"],
                "horizon_rows": manifest["dataset"]["horizon_rows"],
                "logical_rows_sha256": manifest["dataset"][
                    "logical_rows_sha256"
                ],
                "rows": manifest["dataset"]["rows"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
