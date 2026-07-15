#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import read_regulation_feature_artifact
from soccer_bot.modeling.calibration import read_calibrated_predictions_parquet
from soccer_bot.modeling.markets import (
    build_market_benchmarks,
    load_market_benchmark_config,
    summarize_market_benchmarks,
    write_market_rows_parquet,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    evaluation_root = feature_root / "regulation_walk_forward_v1"
    parser = argparse.ArgumentParser(
        description="Audit and evaluate regulation moneyline market benchmarks."
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
        "--calibrated-predictions",
        type=Path,
        default=(
            evaluation_root
            / "rich_rate_v1"
            / "promoted_evaluation"
            / "calibrated_predictions.parquet"
        ),
    )
    parser.add_argument(
        "--walk-forward-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_walk_forward_v1.json",
    )
    parser.add_argument(
        "--market-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_market_benchmark_v1.json",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=evaluation_root / "market_benchmark_v1"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    features = read_regulation_feature_artifact(args.features)
    predictions = read_calibrated_predictions_parquet(args.calibrated_predictions)
    walk_forward = load_walk_forward_config(args.walk_forward_config)
    market_config = load_market_benchmark_config(args.market_config)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        market_rows, audit = build_market_benchmarks(
            connection,
            features,
            config=market_config,
            folds=walk_forward.folds,
        )
    finally:
        connection.close()
    summary = summarize_market_benchmarks(
        market_rows,
        predictions,
        audit=audit,
        config=market_config,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = args.output_dir / "market_predictions.parquet"
    write_market_rows_parquet(market_rows, rows_path)
    warehouse_stat = args.warehouse.stat()
    report = {
        **summary,
        "source_snapshot": {
            "warehouse_path": str(args.warehouse.resolve()),
            "warehouse_size_bytes": warehouse_stat.st_size,
            "warehouse_modified_at_epoch_seconds": warehouse_stat.st_mtime,
            "features_sha256": _file_sha256(args.features),
            "baseline_predictions_sha256": _file_sha256(
                args.calibrated_predictions
            ),
            "walk_forward_config_sha256": _file_sha256(args.walk_forward_config),
            "market_config_sha256": _file_sha256(args.market_config),
        },
        "market_rows_path": str(rows_path.resolve()) if market_rows else None,
        "market_rows_sha256": _file_sha256(rows_path) if market_rows else None,
    }
    report_path = args.output_dir / "report.json"
    _atomic_write_json(report_path, report)
    print(
        json.dumps(
            {
                "report": str(report_path.resolve()),
                "timestamped_polymarket": audit["timestamped_polymarket"],
                "retrospective_bookmaker": audit["retrospective_bookmaker"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
