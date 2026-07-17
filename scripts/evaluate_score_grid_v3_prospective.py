#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prospective_evaluation import run_one_shot_evaluation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen v3 prospective gate once. Before deterministic "
            "readiness this command returns counts only and writes no decision."
        )
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=(
            ROOT
            / "data/predictions/regulation_score_grid_v3_settlement/ledger.jsonl"
        ),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=(
            ROOT / "artifacts/production/regulation_score_grid_v3_shadow/model.json"
        ),
    )
    parser.add_argument(
        "--prospective-gate",
        type=Path,
        default=ROOT / "config/models/regulation_score_grid_v3_prospective_gate.json",
    )
    parser.add_argument(
        "--settlement-config",
        type=Path,
        default=ROOT / "config/models/regulation_score_grid_v3_settlement.json",
    )
    parser.add_argument(
        "--evaluation-config",
        type=Path,
        default=ROOT / "config/models/regulation_score_grid_v3_evaluation.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/predictions/regulation_score_grid_v3_evaluation",
    )
    parser.add_argument("--evaluated-at", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluated_at = (
        datetime.fromisoformat(args.evaluated_at.replace("Z", "+00:00"))
        if args.evaluated_at
        else datetime.now(timezone.utc)
    )
    if evaluated_at.tzinfo is None:
        raise ValueError("--evaluated-at must include a timezone")
    result = run_one_shot_evaluation(
        ledger_path=args.ledger,
        model_path=args.model,
        gate_path=args.prospective_gate,
        settlement_config_path=args.settlement_config,
        evaluation_config_path=args.evaluation_config,
        output_directory=args.output_dir,
        evaluated_at=evaluated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
