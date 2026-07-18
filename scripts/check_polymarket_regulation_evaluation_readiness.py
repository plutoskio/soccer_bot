#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prospective_market_evaluation import update_market_evaluation_readiness


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write count-only readiness for the frozen Polymarket evaluation."
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=ROOT / "data/predictions/polymarket_regulation_market_settlement_v1/ledger.jsonl",
    )
    parser.add_argument(
        "--settlement-config",
        type=Path,
        default=ROOT / "config/models/polymarket_regulation_market_settlement_v1.json",
    )
    parser.add_argument(
        "--evaluation-config",
        type=Path,
        default=ROOT / "config/models/polymarket_regulation_market_evaluation_v1.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/predictions/polymarket_regulation_market_evaluation_v1",
    )
    parser.add_argument("--as-of", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if args.as_of
        else datetime.now(timezone.utc)
    )
    if as_of.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    result = update_market_evaluation_readiness(
        ledger_path=args.ledger,
        settlement_config_path=args.settlement_config,
        evaluation_config_path=args.evaluation_config,
        output_directory=args.output_dir,
        as_of=as_of,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
