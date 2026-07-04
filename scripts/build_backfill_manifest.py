#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.backfill_manifest import (
    BackfillManifestBuilder,
    write_jsonl,
    write_manifest_report,
)
from soccer_bot.config import load_json
from soccer_bot.coverage_audit import RawRequestCache
from soccer_bot.database import Warehouse


def main() -> int:
    coverage = load_json(ROOT / "reports" / "API_FOOTBALL_HISTORICAL_COVERAGE.json")
    config = load_json(ROOT / "config" / "api_football_coverage_audit.json")
    warehouse = Warehouse(
        ROOT / "data" / "warehouse" / "soccer.duckdb",
        ROOT / "migrations",
        ROOT / "config" / "entity_aliases.json",
    )
    try:
        warehouse.migrate()
        builder = BackfillManifestBuilder(
            warehouse=warehouse,
            cache=RawRequestCache(ROOT / "data" / "raw"),
            coverage_result=coverage,
            audit_config=config,
        )
        rows, batches, summary = builder.build()
    finally:
        warehouse.close()

    staged = ROOT / "data" / "staged"
    write_jsonl(staged / "api_football_backfill_manifest.jsonl", rows)
    (staged / "api_football_backfill_batches.json").write_text(
        json.dumps(batches, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (staged / "api_football_backfill_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_manifest_report(summary, ROOT / "reports" / "API_FOOTBALL_BACKFILL_MANIFEST.md")
    print(json.dumps({
        "fixtures": summary["fixtures"],
        "actions": summary["actions"],
        "batches": summary["batches"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
