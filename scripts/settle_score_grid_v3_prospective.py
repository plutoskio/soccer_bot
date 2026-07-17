#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prospective_settlement import update_prospective_settlement_ledger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Append newly final, eligible v3 forecasts to the immutable "
            "prospective settlement ledger without writing performance aggregates."
        )
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=(
            ROOT
            / "data"
            / "predictions"
            / "regulation_score_grid_v3_shadow"
            / "evidence"
        ),
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=(
            ROOT
            / "artifacts"
            / "production"
            / "regulation_score_grid_v3_shadow"
            / "model.json"
        ),
    )
    parser.add_argument(
        "--prospective-gate",
        type=Path,
        default=(
            ROOT
            / "config"
            / "models"
            / "regulation_score_grid_v3_prospective_gate.json"
        ),
    )
    parser.add_argument(
        "--settlement-config",
        type=Path,
        default=(
            ROOT
            / "config"
            / "models"
            / "regulation_score_grid_v3_settlement.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(
            ROOT
            / "data"
            / "predictions"
            / "regulation_score_grid_v3_settlement"
        ),
    )
    parser.add_argument("--settled-at", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settled_at = (
        datetime.fromisoformat(args.settled_at.replace("Z", "+00:00"))
        if args.settled_at
        else datetime.now(timezone.utc)
    )
    if settled_at.tzinfo is None:
        raise ValueError("--settled-at must include a timezone")
    result = update_prospective_settlement_ledger(
        root=ROOT,
        warehouse_path=args.warehouse,
        evidence_directory=args.evidence_dir,
        model_path=args.model,
        gate_path=args.prospective_gate,
        settlement_config_path=args.settlement_config,
        output_directory=args.output_dir,
        settled_at=settled_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
