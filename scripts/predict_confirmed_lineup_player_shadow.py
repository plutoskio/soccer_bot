#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.players import load_first_valid_confirmed_lineups
from soccer_bot.modeling.player_hierarchy import (
    load_confirmed_lineup_player_model,
    load_player_hierarchy_config,
    player_model_sha256,
    predict_confirmed_lineup,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write immutable confirmed-lineup player shadow predictions."
    )
    parser.add_argument(
        "--warehouse", type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--base-snapshot", type=Path,
        default=ROOT / "data" / "predictions" / "regulation_champion_v1" / "latest.json",
    )
    parser.add_argument(
        "--model", type=Path,
        default=ROOT / "data" / "models" / "confirmed_lineup_player_v1" / "model.json",
    )
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "config" / "models" / "confirmed_lineup_player_v1.json",
    )
    parser.add_argument(
        "--as-of", default=None,
        help="Timezone-aware ISO timestamp; defaults to now.",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "data" / "predictions" / "confirmed_lineup_player_v1",
    )
    args = parser.parse_args()
    as_of = datetime.now(timezone.utc) if args.as_of is None else datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        raise ValueError("--as-of requires a timezone")
    config = load_player_hierarchy_config(args.config)
    config_hash = _file_sha256(args.config)
    model = load_confirmed_lineup_player_model(args.model)
    if model.model_version != config.model_version:
        raise RuntimeError("Player model and config versions differ")
    model_hash = player_model_sha256(model)
    base = json.loads(args.base_snapshot.read_text(encoding="utf-8"))
    base_hash = str(base.get("logical_model_sha256", ""))
    if len(base_hash) != 64:
        raise RuntimeError("Base snapshot has no valid model hash")
    base_rows = {}
    fixture_ids = set()
    for row in base.get("predictions", []):
        if row.get("information_state") != "pre_lineup_24h_v1":
            continue
        key = str(row["fixture_id"])
        if key in base_rows:
            raise RuntimeError(f"Duplicate T-24 base prediction: {key}")
        base_rows[key] = row
        fixture_ids.add(key)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        lineups = load_first_valid_confirmed_lineups(
            connection, as_of=as_of, fixture_ids=fixture_ids
        )
    finally:
        connection.close()
    predictions = []
    records_added = 0
    for lineup in lineups:
        base_row = base_rows[lineup.fixture_id]
        prediction = predict_confirmed_lineup(
            lineup,
            model,
            config,
            base_prediction_at=_timestamp(base_row["prediction_at"]),
            base_model_version=str(base_row["model_version"]),
            base_model_sha256=base_hash,
            base_home_expected_goals=float(base_row["expected_home_goals"]),
            base_away_expected_goals=float(base_row["expected_away_goals"]),
        )
        record = _prediction_value(prediction)
        record["logical_model_sha256"] = model_hash
        record["record_sha256"] = _logical_hash(record)
        path = (
            args.output_dir
            / "evidence"
            / prediction.fixture_id
            / (prediction.prediction_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + ".json")
        )
        if _write_once_verified(path, record):
            records_added += 1
        predictions.append(record)
    latest = {
        "snapshot_version": "confirmed_lineup_player_shadow_snapshot_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "model_version": model.model_version,
        "logical_model_sha256": model_hash,
        "config_sha256": config_hash,
        "status": "written" if predictions else "no_eligible_confirmed_lineups",
        "safety": {
            "historical_confirmed_lineup_evaluation": False,
            "prospective_shadow_only": True,
            "champion_replacement_authorized": False,
            "substitute_unconditional_props": False,
            "first_scorer": False,
            "market_features_used": False,
            "trading_actions": False,
        },
        "base_snapshot": {
            "path": str(args.base_snapshot.resolve()),
            "sha256": _file_sha256(args.base_snapshot),
            "model_version": str(base["model_version"]),
            "logical_model_sha256": base_hash,
        },
        "prediction_records": len(predictions),
        "records_added": records_added,
        "predictions": predictions,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(args.output_dir / "latest.json", latest)
    receipt = {
        "status": latest["status"],
        "as_of": latest["as_of"],
        "model_version": model.model_version,
        "logical_model_sha256": model_hash,
        "config_sha256": config_hash,
        "prediction_records": len(predictions),
        "records_added": records_added,
    }
    _append_jsonl(args.output_dir / "receipts.jsonl", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def _prediction_value(prediction) -> dict:
    value = asdict(prediction)
    for key in ("prediction_at", "kickoff", "base_prediction_at"):
        value[key] = value[key].isoformat()
    return value


def _write_once_verified(path: Path, value: dict) -> bool:
    body = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError(f"Immutable prediction collision: {path}")
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _logical_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Snapshot timestamps require timezones")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
