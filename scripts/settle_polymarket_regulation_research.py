#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prospective_market_settlement import update_market_settlement_ledger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append outcome-linked Polymarket research settlements without trading."
    )
    parser.add_argument(
        "--coverage-universe-dir",
        type=Path,
        default=ROOT / "data/predictions/polymarket_market_evidence_v1/coverage_universe",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "data/predictions/polymarket_market_evidence_v1/evidence",
    )
    parser.add_argument(
        "--score-settlement-ledger",
        type=Path,
        default=ROOT / "data/predictions/regulation_score_grid_v3_settlement/ledger.jsonl",
    )
    parser.add_argument(
        "--score-settlement-config",
        type=Path,
        default=ROOT / "config/models/regulation_score_grid_v3_settlement.json",
    )
    parser.add_argument(
        "--market-policy",
        type=Path,
        default=ROOT / "config/contracts/polymarket_regulation_v1.json",
    )
    parser.add_argument(
        "--settlement-config",
        type=Path,
        default=ROOT / "config/models/polymarket_regulation_market_settlement_v1.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/predictions/polymarket_regulation_market_settlement_v1",
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
    result = update_market_settlement_ledger(
        coverage_universe_directory=args.coverage_universe_dir,
        evidence_directory=args.evidence_dir,
        score_settlement_ledger_path=args.score_settlement_ledger,
        score_settlement_config_path=args.score_settlement_config,
        market_policy_path=args.market_policy,
        settlement_config_path=args.settlement_config,
        output_directory=args.output_dir,
        settled_at=settled_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
