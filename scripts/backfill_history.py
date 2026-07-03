#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_json
from soccer_bot.http import HttpClient
from soccer_bot.raw_store import RawArtifactStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download curated historical soccer data")
    parser.add_argument(
        "source",
        choices=("football-data", "understat", "all"),
        nargs="?",
        default="all",
    )
    parser.add_argument("--refresh", action="store_true", help="Refetch requests already captured")
    return parser.parse_args()


def existing_requests(raw_root: Path) -> set[tuple[str, str, str]]:
    existing: set[tuple[str, str, str]] = set()
    for path in raw_root.rglob("*.meta.json"):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata.get("http_status") != 200:
            continue
        params = json.dumps(metadata.get("request_parameters", {}), sort_keys=True)
        existing.add((metadata.get("source", ""), metadata.get("resource", ""), params))
    return existing


def request_key(source: str, resource: str, params: dict) -> tuple[str, str, str]:
    return source, resource, json.dumps(params, sort_keys=True)


def backfill_football_data(config, client, store, existing, refresh: bool) -> tuple[int, int]:
    fetched = skipped = 0
    interval = float(config["minimum_interval_seconds"])
    last_request = 0.0
    for season in config["seasons"]:
        for division in config["divisions"]:
            path = f"/mmz4281/{season}/{division}.csv"
            params = {"path": path}
            key = request_key("football_data_uk", "league_csv", params)
            if not refresh and key in existing:
                skipped += 1
                continue
            wait = interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            response = client.get("https://www.football-data.co.uk", path)
            last_request = time.monotonic()
            store.store(
                source="football_data_uk",
                resource="league_csv",
                response=response,
                request_params=params,
            )
            print(f"football_data_uk season={season} division={division} HTTP={response.status}")
            fetched += 1
    return fetched, skipped


def backfill_understat(config, client, store, existing, refresh: bool) -> tuple[int, int]:
    fetched = skipped = 0
    interval = float(config["minimum_interval_seconds"])
    last_request = 0.0
    for season in config["seasons"]:
        for league in config["leagues"]:
            params = {"league": league, "season": season}
            key = request_key("understat", "league_data", params)
            if not refresh and key in existing:
                skipped += 1
                continue
            wait = interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            encoded_league = quote(league, safe="")
            response = client.get(
                "https://understat.com",
                f"/getLeagueData/{encoded_league}/{season}",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"https://understat.com/league/{encoded_league}/{season}",
                },
            )
            last_request = time.monotonic()
            store.store(
                source="understat",
                resource="league_data",
                response=response,
                request_params=params,
            )
            print(f"understat season={season} league={league} HTTP={response.status}")
            fetched += 1
    return fetched, skipped


def main() -> int:
    args = parse_args()
    config = load_json(ROOT / "config" / "backfill.json")
    raw_root = ROOT / "data" / "raw"
    existing = existing_requests(raw_root)
    client = HttpClient()
    store = RawArtifactStore(raw_root)
    total_fetched = total_skipped = 0
    if args.source in {"football-data", "all"}:
        fetched, skipped = backfill_football_data(
            config["football_data_uk"], client, store, existing, args.refresh
        )
        total_fetched += fetched
        total_skipped += skipped
    if args.source in {"understat", "all"}:
        fetched, skipped = backfill_understat(
            config["understat"], client, store, existing, args.refresh
        )
        total_fetched += fetched
        total_skipped += skipped
    print(f"fetched={total_fetched} skipped={total_skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
