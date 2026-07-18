#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.polymarket_contracts import load_polymarket_contract_policy
from soccer_bot.polymarket_evidence import capture_polymarket_market_evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create immutable, outcome-blind Polymarket evidence for a prediction snapshot"
    )
    parser.add_argument("--warehouse", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--expected-policy-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--captured-at")
    args = parser.parse_args()
    policy, policy_hash = load_polymarket_contract_policy(args.policy)
    if policy_hash != args.expected_policy_sha256:
        raise RuntimeError("policy_sha256_mismatch")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    captured_at = (
        datetime.fromisoformat(args.captured_at.replace("Z", "+00:00"))
        if args.captured_at
        else datetime.now(timezone.utc)
    )
    if captured_at.tzinfo is None:
        raise ValueError("captured-at must be timezone-aware")
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        result = capture_polymarket_market_evidence(
            connection,
            snapshot=snapshot,
            policy=policy,
            policy_sha256=policy_hash,
            output_directory=args.output_dir,
            captured_at=captured_at,
        )
    finally:
        connection.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
