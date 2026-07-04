#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from soccer_bot.backfill_executor import HistoricalBackfillExecutor, files_sha256
from soccer_bot.config import load_env, load_json
from soccer_bot.database import Warehouse
from soccer_bot.http import HttpClient
from soccer_bot.raw_store import RawArtifactStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a validated, checkpointed API-Football historical backfill"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Make API requests and write data; without this flag the command is a dry run",
    )
    parser.add_argument(
        "--max-batches", type=int, default=1,
        help="Maximum batches to process in this run (default: 1)",
    )
    parser.add_argument("--batch-id", help="Process one specific batch")
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Retry failed checkpoints; failed batches are skipped by default",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    args = parse_args()
    if args.max_batches <= 0:
        raise SystemExit("--max-batches must be positive")
    batches_path = ROOT / "data" / "staged" / "api_football_backfill_batches.json"
    manifest_path = ROOT / "data" / "staged" / "api_football_backfill_manifest.jsonl"
    batches = load_json(batches_path)
    rows = load_jsonl(manifest_path)
    config = load_json(ROOT / "config" / "backfill.json")
    env = load_env(ROOT / ".env")
    warehouse = Warehouse(
        ROOT / "data" / "warehouse" / "soccer.duckdb",
        ROOT / "migrations",
        ROOT / "config" / "entity_aliases.json",
    )
    try:
        warehouse.migrate()
        warehouse.register_sources()
        executor = HistoricalBackfillExecutor(
            warehouse=warehouse,
            raw_store=RawArtifactStore(ROOT / "data" / "raw"),
            http_client=HttpClient("soccer-bot-historical-backfill/0.1"),
            api_key=env.get("API_FOOTBALL_KEY", ""),
            config=config,
            batches=batches,
            manifest_rows=rows,
            manifest_sha256=files_sha256([batches_path, manifest_path]),
        )
        summary = executor.run(
            maximum_batches=args.max_batches,
            execute=args.execute,
            batch_id=args.batch_id,
            retry_failed=args.retry_failed,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        warehouse.close()


if __name__ == "__main__":
    raise SystemExit(main())
