#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import read_regulation_feature_artifact
from soccer_bot.modeling.calibration import (
    fit_and_apply_temperature_calibration,
    summarize_calibration,
    write_calibrated_predictions_parquet,
)
from soccer_bot.modeling.artifacts import write_walk_forward_artifacts
from soccer_bot.modeling.walk_forward import (
    evaluate_walk_forward,
    load_walk_forward_config,
    summarize_predictions,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run expanding-window regulation score baselines."
    )
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    parser.add_argument(
        "--features",
        type=Path,
        default=feature_root / "features.parquet",
    )
    parser.add_argument(
        "--feature-manifest",
        type=Path,
        default=feature_root / "manifest.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=(
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=feature_root / "regulation_walk_forward_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = read_regulation_feature_artifact(args.features)
    config = load_walk_forward_config(args.config)
    predictions = evaluate_walk_forward(rows, config)
    calibration_fits, calibrated_predictions = (
        fit_and_apply_temperature_calibration(predictions, config)
    )
    calibrated_path = args.output_dir / "calibrated_predictions.parquet"
    write_calibrated_predictions_parquet(calibrated_predictions, calibrated_path)
    summary = {
        "evaluation_version": config.evaluation_version,
        "walk_forward_policy": {
            "expanding_window": True,
            "hyperparameters_frozen_across_folds": True,
            "prediction_events_before_same_timestamp_results": True,
            "available_results_update_all_later_predictions": True,
            "simultaneous_results_batched": True,
            "result_availability_delay_minutes": (
                config.result_availability_delay_minutes
            ),
            "minimum_training_fixtures_per_horizon": (
                config.minimum_training_fixtures
            ),
        },
        **summarize_predictions(predictions, config),
        "moneyline_calibration": summarize_calibration(
            calibration_fits,
            calibrated_predictions,
            predictions,
            config,
        ),
        "calibrated_predictions_path": str(calibrated_path.resolve()),
    }
    manifest = write_walk_forward_artifacts(
        predictions,
        summary,
        output_dir=args.output_dir,
        source_files={
            "evaluation_configuration": args.config,
            "feature_manifest": args.feature_manifest,
            "frozen_features": args.features,
        },
    )
    print(
        json.dumps(
            {
                "evaluation_version": config.evaluation_version,
                "logical_rows_sha256": manifest["predictions"][
                    "logical_rows_sha256"
                ],
                "prediction_rows": len(predictions),
                "report": manifest["report"]["path"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
