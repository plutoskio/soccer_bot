#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collector import Collector
from soccer_bot.config import load_env, load_json
from soccer_bot.database import Warehouse
from soccer_bot.http import HttpClient
from soccer_bot.raw_store import RawArtifactStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one quota-aware collection cycle for due soccer fixtures"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan currently due work without making network requests or changing collector state",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json(ROOT / "config" / "collector.json")
    env = load_env(ROOT / ".env")
    warehouse = Warehouse(
        ROOT / "data" / "warehouse" / "soccer.duckdb",
        ROOT / "migrations",
        ROOT / "config" / "entity_aliases.json",
    )
    try:
        warehouse.migrate()
        warehouse.register_sources()
        collector = Collector(
            warehouse=warehouse,
            raw_store=RawArtifactStore(ROOT / "data" / "raw"),
            http_client=HttpClient("soccer-bot-collector/0.1"),
            api_key=env.get("API_FOOTBALL_KEY", ""),
            config=config,
        )
        summary = collector.run(dry_run=args.dry_run)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
