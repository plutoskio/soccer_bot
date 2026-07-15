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
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    load_fixture_performance,
    load_rich_rate_config,
    research_rich_rate_candidate,
    rich_feature_rows_sha256,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    evaluation_root = feature_root / "regulation_walk_forward_v1"
    parser = argparse.ArgumentParser(
        description="Research xG/shots rate corrections inside development only."
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
        "--output-dir", type=Path, default=evaluation_root / "rich_rate_v1"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_rows = read_regulation_feature_artifact(args.features)
    baseline_predictions = read_walk_forward_predictions(args.predictions)
    rich_config = load_rich_rate_config(args.rich_config)
    walk_forward = load_walk_forward_config(args.walk_forward_config)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        performance = load_fixture_performance(connection, rich_config)
    finally:
        connection.close()
    rich_rows = ChronologicalRichRateBuilder(rich_config).build(
        base_rows, performance
    )
    fits, predictions, summary = research_rich_rate_candidate(
        rich_rows,
        baseline_predictions,
        config=rich_config,
        walk_forward_config=walk_forward,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rich_path = args.output_dir / "rich_features.parquet"
    prediction_path = args.output_dir / "validation_predictions.parquet"
    _write_dataclass_parquet(
        rich_rows,
        rich_path,
        datetime_columns=("prediction_at", "kickoff"),
        order_by="kickoff, fixture_id, information_state",
    )
    _write_dataclass_parquet(
        predictions,
        prediction_path,
        datetime_columns=("prediction_at", "kickoff"),
        order_by="kickoff, fixture_id, information_state",
    )
    warehouse_stat = args.warehouse.stat()
    report = {
        **summary,
        "feature_version": rich_config.feature_version,
        "rich_feature_rows": len(rich_rows),
        "rich_feature_rows_sha256": rich_feature_rows_sha256(rich_rows),
        "performance_fixtures": len(performance),
        "fits": [
            {
                **asdict(fit),
                "fit_kickoff_end_exclusive": (
                    fit.fit_kickoff_end_exclusive.isoformat()
                ),
            }
            for fit in fits
        ],
        "validation_prediction_rows": len(predictions),
        "artifacts": {
            "rich_features": str(rich_path.resolve()),
            "rich_features_sha256": _file_sha256(rich_path),
            "validation_predictions": str(prediction_path.resolve()),
            "validation_predictions_sha256": _file_sha256(prediction_path),
        },
        "source_snapshot": {
            "warehouse_path": str(args.warehouse.resolve()),
            "warehouse_size_bytes": warehouse_stat.st_size,
            "warehouse_modified_at_epoch_seconds": warehouse_stat.st_mtime,
            "base_features_sha256": _file_sha256(args.features),
            "baseline_predictions_sha256": _file_sha256(args.predictions),
            "rich_config_sha256": _file_sha256(args.rich_config),
            "walk_forward_config_sha256": _file_sha256(
                args.walk_forward_config
            ),
        },
    }
    report_path = args.output_dir / "report.json"
    _atomic_write_json(report_path, report)
    print(
        json.dumps(
            {
                "fits": report["fits"],
                "metrics": report["metrics"],
                "report": str(report_path.resolve()),
                "test_fold_accessed": report["test_fold_accessed"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _write_dataclass_parquet(
    rows,
    path: Path,
    *,
    datetime_columns: tuple[str, ...],
    order_by: str,
) -> None:
    if not rows:
        raise RuntimeError(f"Cannot write empty artifact: {path.name}")
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix=f"{path.stem}-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(handle.name)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with handle:
            for row in rows:
                value = asdict(row)
                for column in datetime_columns:
                    item = value[column]
                    if not isinstance(item, datetime):
                        raise TypeError(f"{column} is not datetime")
                    value[column] = item.timestamp()
                handle.write(
                    json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
                )
        replacements = ",\n".join(
            f"to_timestamp({column}) AS {column}" for column in datetime_columns
        )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE ({replacements})
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY {order_by}
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
