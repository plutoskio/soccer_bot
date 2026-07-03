#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_env, load_json
from soccer_bot.http import HttpClient
from soccer_bot.probes import (
    ApiFootballProbe,
    FootballDataUkProbe,
    PolymarketProbe,
    StatsBombProbe,
    UnderstatProbe,
)
from soccer_bot.raw_store import RawArtifactStore
from soccer_bot.report import build_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded soccer data source probes")
    parser.add_argument(
        "source",
        choices=(
            "api-football",
            "polymarket",
            "statsbomb",
            "football-data",
            "understat",
            "player-data",
            "bootstrap",
            "all",
            "report",
        ),
        help="Source to probe or report-only mode",
    )
    return parser.parse_args()


def summarize(results) -> None:
    for result in results:
        count = "unknown" if result.result_count is None else result.result_count
        print(
            f"{result.source}:{result.resource} HTTP={result.status} "
            f"records={count} duplicate={result.artifact.duplicate}"
        )


def main() -> int:
    args = parse_args()
    settings = load_json(ROOT / "config" / "probe_cases.json")
    env = load_env(ROOT / ".env")
    client = HttpClient()
    store = RawArtifactStore(ROOT / "data" / "raw")

    if args.source in {"api-football", "all"}:
        config = settings["api_football"]
        probe = ApiFootballProbe(
            client,
            store,
            env.get("API_FOOTBALL_KEY", ""),
            max_calls=config["max_calls"],
            minimum_interval_seconds=config["minimum_interval_seconds"],
        )
        summarize(
            probe.run(
                config["dates"],
                config["timezone"],
                config["max_detail_fixtures"],
                config.get("fixture_ids"),
            )
        )

    if args.source in {"polymarket", "all"}:
        config = settings["polymarket"]
        probe = PolymarketProbe(client, store, max_calls=config["max_calls"])
        summarize(
            probe.run(
                config["events_per_tag"],
                config["event_tag_ids"],
                config["search_queries"],
            )
        )

    if args.source in {"statsbomb", "bootstrap", "all"}:
        config = settings["statsbomb"]
        probe = StatsBombProbe(client, store)
        summarize(probe.run(config["competition_name"], config["season_name"]))

    if args.source in {"football-data", "bootstrap", "all"}:
        config = settings["football_data_uk"]
        probe = FootballDataUkProbe(client, store)
        summarize(probe.run(config["csv_paths"]))

    if args.source in {"understat", "player-data", "all"}:
        config = settings["understat"]
        probe = UnderstatProbe(client, store)
        summarize(probe.run(config["league"], config["season"]))

    build_report(
        ROOT / "data" / "raw",
        ROOT / "reports" / "SOURCE_VALIDATION_REPORT.md",
    )
    print("report=reports/SOURCE_VALIDATION_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
