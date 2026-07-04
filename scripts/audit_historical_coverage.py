#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_env, load_json
from soccer_bot.coverage_audit import (
    HistoricalCoverageAuditor,
    RawRequestCache,
    write_markdown_report,
)
from soccer_bot.http import HttpClient
from soccer_bot.raw_store import RawArtifactStore


def main() -> int:
    config = load_json(ROOT / "config" / "api_football_coverage_audit.json")
    api_key = load_env(ROOT / ".env").get("API_FOOTBALL_KEY", "")
    if not api_key:
        raise RuntimeError("API_FOOTBALL_KEY is missing")
    raw_root = ROOT / "data" / "raw"
    auditor = HistoricalCoverageAuditor(
        client=HttpClient("soccer-bot-historical-coverage-audit/0.1"),
        store=RawArtifactStore(raw_root),
        cache=RawRequestCache(raw_root),
        api_key=api_key,
        config=config,
    )
    result = auditor.run()
    json_path = ROOT / "reports" / "API_FOOTBALL_HISTORICAL_COVERAGE.json"
    markdown_path = ROOT / "reports" / "API_FOOTBALL_HISTORICAL_COVERAGE.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown_report(result, markdown_path)
    print(f"network_calls={result['network_calls']}")
    print(f"cache_hits={result['cache_hits']}")
    print(f"json_report={json_path.relative_to(ROOT)}")
    print(f"markdown_report={markdown_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
