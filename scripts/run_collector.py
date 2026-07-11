#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.collector import Collector
from soccer_bot.collection_planner import validate_collector_config
from soccer_bot.config import load_env, load_json
from soccer_bot.database import Warehouse
from soccer_bot.http import HttpClient
from soccer_bot.locking import CollectorLock
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
    parser.add_argument(
        "--catch-up-days",
        type=int,
        default=None,
        help="Expand the discovery recovery window for this run; cannot reduce configured recovery_days",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json(ROOT / "config" / "collector.json")
    validate_collector_config(config, args.catch_up_days)
    lock = None
    if not args.dry_run:
        lock_config = config.get("lock", {})
        lock = CollectorLock(
            ROOT / "data" / "warehouse" / "collector.lock",
            stale_timeout_seconds=int(
                lock_config.get("stale_timeout_seconds", 900)
            ),
            heartbeat_interval_seconds=int(
                lock_config.get("heartbeat_interval_seconds", 30)
            ),
        )
        lock_result = lock.acquire()
        if not lock_result.acquired:
            print(json.dumps({"status": "already_running"}, indent=2))
            return 0
    warehouse = None
    try:
        env = {} if args.dry_run else load_env(ROOT / ".env")
        api_key = env.get("API_FOOTBALL_KEY", "")
        if not args.dry_run and not api_key:
            raise ValueError("API_FOOTBALL_KEY is missing")
        warehouse = Warehouse(
            ROOT / "data" / "warehouse" / "soccer.duckdb",
            ROOT / "migrations",
            ROOT / "config" / "entity_aliases.json",
            read_only=args.dry_run,
        )
        if args.dry_run:
            pending = warehouse.pending_migrations()
            if pending:
                raise RuntimeError(
                    "Dry run requires a current schema; pending migrations: "
                    + ", ".join(pending)
                )
        else:
            warehouse.migrate()
            warehouse.register_sources()
        collector = Collector(
            warehouse=warehouse,
            raw_store=RawArtifactStore(ROOT / "data" / "raw"),
            http_client=HttpClient("soccer-bot-collector/0.1"),
            api_key=api_key,
            config=config,
            report_directory=(
                None if args.dry_run
                else ROOT / config.get("health", {}).get(
                    "report_directory", "reports/collector"
                )
            ),
        )
        summary = collector.run(
            dry_run=args.dry_run,
            catch_up_days=args.catch_up_days,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 2 if summary.get("health", {}).get("severity") == "blocking" else 0
    finally:
        if warehouse is not None:
            warehouse.close()
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
