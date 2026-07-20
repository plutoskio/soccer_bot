#!/usr/bin/env python3
"""Independently verify the public prediction heartbeat and frozen identity."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = (
    "https://soccer-bot-web-production.up.railway.app/api/prediction-health"
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class PublicPredictionHealthError(RuntimeError):
    """Raised when the independent public heartbeat check fails closed."""


def extract_public_snapshot(payload: str) -> dict[str, object]:
    """Validate the stable, singular public heartbeat JSON contract."""

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise PublicPredictionHealthError(
            "public_heartbeat_not_json"
        ) from error
    if not isinstance(value, dict):
        raise PublicPredictionHealthError("public_heartbeat_not_object")
    if value.get("heartbeat_version") != "public_prediction_heartbeat_v1":
        raise PublicPredictionHealthError("public_heartbeat_version_invalid")
    for field in ("model_version", "logical_model_sha256", "as_of"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise PublicPredictionHealthError(f"public_snapshot_{field}_invalid")
    if len(str(value["logical_model_sha256"])) != 64:
        raise PublicPredictionHealthError("public_snapshot_logical_model_sha256_invalid")
    for field in ("prediction_count", "fixture_count"):
        field_value = value.get(field)
        if isinstance(field_value, bool) or not isinstance(field_value, int):
            raise PublicPredictionHealthError(f"public_snapshot_{field}_invalid")
    return value


def evaluate_public_snapshot(
    snapshot: dict[str, object],
    *,
    expected_model_version: str,
    expected_logical_hash: str,
    stale_after_seconds: int,
    now: datetime,
) -> dict[str, object]:
    if now.tzinfo is None:
        raise PublicPredictionHealthError("monitor_time_must_have_timezone")
    try:
        as_of = datetime.fromisoformat(str(snapshot["as_of"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as error:
        raise PublicPredictionHealthError("public_snapshot_as_of_invalid") from error
    if as_of.tzinfo is None:
        raise PublicPredictionHealthError("public_snapshot_as_of_missing_timezone")
    age_seconds = max(
        0.0,
        (now.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)).total_seconds(),
    )
    failures = []
    if snapshot.get("model_version") != expected_model_version:
        failures.append("public_model_version_mismatch")
    if snapshot.get("logical_model_sha256") != expected_logical_hash:
        failures.append("public_logical_model_hash_mismatch")
    if age_seconds > stale_after_seconds:
        failures.append("public_champion_snapshot_stale")
    if not isinstance(snapshot.get("prediction_count"), int) or int(
        snapshot["prediction_count"]
    ) <= 0:
        failures.append("public_prediction_rows_zero")
    if not isinstance(snapshot.get("fixture_count"), int) or int(
        snapshot["fixture_count"]
    ) <= 0:
        failures.append("public_fixture_count_zero")
    return {
        "status": "failed" if failures else "ok",
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "snapshot_as_of": as_of.astimezone(timezone.utc).isoformat(),
        "snapshot_age_seconds": round(age_seconds, 3),
        "stale_after_seconds": stale_after_seconds,
        "model_version": snapshot.get("model_version"),
        "logical_model_sha256": snapshot.get("logical_model_sha256"),
        "prediction_count": snapshot.get("prediction_count"),
        "fixture_count": snapshot.get("fixture_count"),
        "failures": failures,
    }


def fetch_heartbeat(url: str, *, timeout_seconds: float) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "soccer-bot-independent-watchdog/1.0",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise PublicPredictionHealthError(
                f"public_endpoint_http_{response.status}"
            )
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PublicPredictionHealthError("public_endpoint_response_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicPredictionHealthError(
            "public_endpoint_response_not_utf8"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed if the public champion heartbeat is stale or unexpected."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--collector-config", type=Path, default=ROOT / "config" / "collector.json")
    parser.add_argument("--stale-after-seconds", type=int, default=1200)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    args = parser.parse_args()
    try:
        if args.stale_after_seconds <= 0 or args.timeout_seconds <= 0:
            raise PublicPredictionHealthError("monitor_thresholds_must_be_positive")
        config = json.loads(args.collector_config.read_text(encoding="utf-8"))
        publication = config["prediction_publication"]
        snapshot = extract_public_snapshot(
            fetch_heartbeat(args.url, timeout_seconds=args.timeout_seconds)
        )
        result = evaluate_public_snapshot(
            snapshot,
            expected_model_version=str(publication["model_version"]),
            expected_logical_hash=str(publication["logical_model_sha256"]),
            stale_after_seconds=args.stale_after_seconds,
            now=datetime.now(timezone.utc),
        )
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, PublicPredictionHealthError)
            else "unexpected_public_watchdog_failure"
        )
        result = {
            "status": "failed",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "failures": [message[:240]],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
