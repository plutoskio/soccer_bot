#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import read_regulation_feature_artifact
from soccer_bot.modeling.artifacts import read_walk_forward_predictions
from soccer_bot.modeling.calibration import (
    fit_and_apply_temperature_calibration,
    read_calibrated_predictions_parquet,
    summarize_calibration,
    write_calibrated_predictions_parquet,
)
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    evaluate_promoted_rich_rate_candidate,
    load_fixture_performance,
    load_rich_rate_config,
    summarize_promoted_rich_rate,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    evaluation_root = feature_root / "regulation_walk_forward_v1"
    rich_root = evaluation_root / "rich_rate_v1"
    parser = argparse.ArgumentParser(
        description=(
            "Refit the development-selected rich-rate recipe, calibrate on the "
            "calibration fold, and evaluate the frozen final test once."
        )
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--features", type=Path, default=feature_root / "features.parquet"
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        default=evaluation_root / "predictions.parquet",
    )
    parser.add_argument(
        "--baseline-calibrated-predictions",
        type=Path,
        default=evaluation_root / "calibrated_predictions.parquet",
    )
    parser.add_argument(
        "--selection-report", type=Path, default=rich_root / "report.json"
    )
    parser.add_argument(
        "--rich-config",
        type=Path,
        default=ROOT / "config" / "features" / "regulation_rich_rate_v1.json",
    )
    parser.add_argument(
        "--walk-forward-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_walk_forward_v1.json",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=rich_root / "promoted_evaluation"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_rows = read_regulation_feature_artifact(args.features)
    baseline_predictions = read_walk_forward_predictions(args.predictions)
    calibrated_baseline = read_calibrated_predictions_parquet(
        args.baseline_calibrated_predictions
    )
    rich_config = load_rich_rate_config(args.rich_config)
    walk = load_walk_forward_config(args.walk_forward_config)
    selection_evidence = json.loads(args.selection_report.read_text())
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        performance = load_fixture_performance(connection, rich_config)
    finally:
        connection.close()
    rich_rows = ChronologicalRichRateBuilder(rich_config).build(
        base_rows, performance
    )
    fits, rich_predictions = evaluate_promoted_rich_rate_candidate(
        rich_rows,
        baseline_predictions,
        config=rich_config,
        walk_forward_config=walk,
        selection_evidence=selection_evidence,
    )
    calibration_fits, calibrated_rich = fit_and_apply_temperature_calibration(
        rich_predictions, walk
    )
    final_summary = summarize_promoted_rich_rate(
        rich_predictions,
        calibrated_rich,
        baseline_predictions,
        calibrated_baseline,
        walk,
    )
    calibration_summary = summarize_calibration(
        calibration_fits, calibrated_rich, rich_predictions, walk
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = args.output_dir / "predictions.parquet"
    calibrated_path = args.output_dir / "calibrated_predictions.parquet"
    _write_dataclass_parquet(rich_predictions, raw_path)
    write_calibrated_predictions_parquet(calibrated_rich, calibrated_path)
    report = {
        "evaluation_version": "regulation_rich_rate_promoted_evaluation_v1",
        "policy": {
            "selection_report": str(args.selection_report.resolve()),
            "selection_used_development_only": True,
            "hyperparameters_changed_after_selection": False,
            "coefficients_refit_on_all_development": True,
            "temperature_fit_on_calibration_only": True,
            "final_test_scored_after_recipe_freeze": True,
        },
        "coefficient_fits": [
            {
                **asdict(fit),
                "fit_kickoff_end_exclusive": (
                    fit.fit_kickoff_end_exclusive.isoformat()
                ),
            }
            for fit in fits
        ],
        "moneyline_calibration": calibration_summary,
        "final_test": final_summary,
        "artifacts": {
            "predictions": str(raw_path.resolve()),
            "predictions_sha256": _file_sha256(raw_path),
            "calibrated_predictions": str(calibrated_path.resolve()),
            "calibrated_predictions_sha256": _file_sha256(calibrated_path),
        },
        "source_hashes": {
            "features": _file_sha256(args.features),
            "baseline_predictions": _file_sha256(args.predictions),
            "baseline_calibrated_predictions": _file_sha256(
                args.baseline_calibrated_predictions
            ),
            "selection_report": _file_sha256(args.selection_report),
            "rich_config": _file_sha256(args.rich_config),
            "walk_forward_config": _file_sha256(args.walk_forward_config),
        },
    }
    report_path = args.output_dir / "report.json"
    _atomic_write_json(report_path, report)
    print(
        json.dumps(
            {
                "coefficient_fits": report["coefficient_fits"],
                "final_test": report["final_test"],
                "report": str(report_path.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _write_dataclass_parquet(rows, path: Path) -> None:
    if not rows:
        raise RuntimeError("Cannot write empty rich-rate predictions")
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="rich-predictions-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(handle.name)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with handle:
            for row in rows:
                value = asdict(row)
                for column in ("prediction_at", "kickoff"):
                    item = value[column]
                    if not isinstance(item, datetime):
                        raise TypeError(f"{column} is not datetime")
                    value[column] = item.timestamp()
                handle.write(
                    json.dumps(value, separators=(",", ":"), allow_nan=False)
                    + "\n"
                )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (
                        to_timestamp(prediction_at) AS prediction_at,
                        to_timestamp(kickoff) AS kickoff
                    )
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY prediction_at, fixture_id, information_state
                ) TO {_sql_literal(temporary)} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary, path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
