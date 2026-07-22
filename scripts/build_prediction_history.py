#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prediction_history import build_prediction_history


def main() -> None:
    parser = argparse.ArgumentParser(description="Build verified published prediction history.")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--settlement-config", type=Path, required=True)
    parser.add_argument("--platform-snapshot-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()
    generated_at = datetime.fromisoformat(args.generated_at.replace("Z", "+00:00")) if args.generated_at else datetime.now(timezone.utc)
    artifact = build_prediction_history(
        evidence_directory=args.evidence_dir,
        ledger_path=args.ledger,
        settlement_config_path=args.settlement_config,
        generated_at=generated_at,
        platform_snapshot_directory=args.platform_snapshot_dir,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / "latest.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    print(json.dumps({"status": "written", "fixture_count": artifact["fixture_count"], "prediction_group_count": artifact["prediction_group_count"], "history_rows_sha256": artifact["history_rows_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
