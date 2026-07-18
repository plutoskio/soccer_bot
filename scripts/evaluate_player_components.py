#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.players import read_player_target_artifact
from soccer_bot.modeling.player_hierarchy import (
    evaluate_player_components,
    load_player_hierarchy_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run non-promotable chronological player component diagnostics."
    )
    parser.add_argument(
        "--targets", type=Path,
        default=ROOT / "data" / "features" / "confirmed_lineup_player_v1" / "targets.parquet",
    )
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "config" / "models" / "confirmed_lineup_player_v1.json",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "data" / "features" / "confirmed_lineup_player_v1" / "component_diagnostic_v1",
    )
    args = parser.parse_args()
    config = load_player_hierarchy_config(args.config)
    rows = read_player_target_artifact(args.targets)
    predictions, report = evaluate_player_components(rows, config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".jsonl", dir=args.output_dir, delete=False
    )
    json_path = Path(handle.name)
    output_path = args.output_dir / "predictions.parquet"
    temporary_parquet = args.output_dir / ".predictions.parquet.tmp"
    try:
        with handle:
            for row in predictions:
                value = asdict(row)
                value["kickoff"] = row.kickoff.timestamp()
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (to_timestamp(kickoff) AS kickoff)
                    FROM read_json_auto('{str(json_path).replace("'", "''")}', format='newline_delimited')
                    ORDER BY kickoff, fixture_id, player_id
                ) TO '{str(temporary_parquet).replace("'", "''")}'
                (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary_parquet, output_path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary_parquet.unlink(missing_ok=True)
    report["prediction_artifact"] = str(output_path.resolve())
    temporary = args.output_dir / ".report.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output_dir / "report.json")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
