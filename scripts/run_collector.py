#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from soccer_bot.operational_alerts import run_operational_watchdog
from soccer_bot.prediction_publication import run_prediction_publication
from soccer_bot.raw_store import RawArtifactStore


def compact_runtime_summary(
    summary: dict[str, object], *, dry_run: bool
) -> dict[str, object]:
    """Return a bounded log record while durable stores retain full detail."""

    if dry_run:
        return summary
    health = summary.get("health")
    publication = summary.get("prediction_publication")
    operations = summary.get("operational_watchdog")
    compact: dict[str, object] = {
        "status": "completed",
        "api_football_calls": summary.get("api_football_calls", 0),
        "polymarket_calls": summary.get("polymarket_calls", 0),
        "planned_job_count": len(summary.get("planned_jobs", [])),
        "executed_job_count": len(summary.get("executed_jobs", [])),
        "selected_fixtures": summary.get("selected_fixtures", 0),
        "market_fixture_scope": summary.get("market_fixture_scope", 0),
        "linked_polymarket_events": summary.get(
            "linked_polymarket_events", 0
        ),
    }
    if isinstance(health, dict):
        compact["health"] = {
            key: health.get(key)
            for key in ("report_date", "severity", "blocking_reason")
        }
    if isinstance(publication, dict):
        compact["prediction_publication"] = {
            key: publication.get(key)
            for key in (
                "status",
                "as_of",
                "model_version",
                "logical_model_sha256",
                "prediction_rows",
                "fixture_count",
                "error_type",
                "error",
            )
            if key in publication
        }
    if isinstance(operations, dict):
        alerts = operations.get("alerts", [])
        compact["operational_watchdog"] = {
            "overall_status": operations.get("overall_status"),
            "should_fail_run": operations.get("should_fail_run", False),
            "alert_codes": [
                alert.get("code")
                for alert in alerts
                if isinstance(alert, dict) and alert.get("code")
            ],
        }
    return compact


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
        health_severity = str(summary.get("health", {}).get("severity", "unknown"))
        operational_exit = False
        if not args.dry_run:
            warehouse.close()
            warehouse = None
            publication_as_of = datetime.now(timezone.utc)
            summary["prediction_publication"] = run_prediction_publication(
                root=ROOT,
                warehouse_path=ROOT / "data" / "warehouse" / "soccer.duckdb",
                collector_config=config,
                environment=env,
                as_of=publication_as_of,
                health_severity=health_severity,
            )
            try:
                operations = run_operational_watchdog(
                    root=ROOT,
                    collector_config=config,
                    publication_result=summary["prediction_publication"],
                    now=publication_as_of,
                )
            except Exception as error:
                operations = {
                    "status": "failed",
                    "overall_status": "critical",
                    "should_fail_run": True,
                    "alerts": [
                        {
                            "code": "operational_watchdog_failed",
                            "severity": "critical",
                            "component": "operational_watchdog",
                            "summary": type(error).__name__,
                        }
                    ],
                }
            summary["operational_watchdog"] = operations
            operational_exit = bool(operations.get("should_fail_run", False))
        runtime_summary = compact_runtime_summary(summary, dry_run=args.dry_run)
        if args.dry_run:
            print(json.dumps(runtime_summary, indent=2, sort_keys=True))
        else:
            stream = (
                sys.stderr
                if health_severity == "blocking" or operational_exit
                else sys.stdout
            )
            print(
                json.dumps(runtime_summary, separators=(",", ":"), sort_keys=True),
                file=stream,
            )
        if health_severity == "blocking":
            return 2
        return 3 if operational_exit else 0
    finally:
        if warehouse is not None:
            warehouse.close()
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
