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

from soccer_bot.config import load_json
from soccer_bot.datasets.features import (
    ChronologicalTeamStateBuilder,
    load_team_state_feature_config,
)
from soccer_bot.datasets.targets import (
    build_regulation_score_targets,
    load_regulation_target_exclusions,
)
from soccer_bot.datasets.upcoming import load_upcoming_inference_fixtures
from soccer_bot.modeling.production import (
    champion_model_sha256,
    load_regulation_champion,
    predict_regulation_moneyline,
    prediction_rows_sha256,
)
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    load_fixture_performance,
    load_rich_rate_config,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a read-only upcoming regulation-moneyline snapshot."
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=(
            ROOT
            / "data"
            / "models"
            / "regulation_champion_v1"
            / "model.json"
        ),
    )
    parser.add_argument(
        "--model-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_champion_v1.json",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Timezone-aware ISO timestamp; defaults to the current UTC time.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "predictions" / "regulation_champion_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = _parse_as_of(args.as_of)
    specification = load_json(args.model_config)
    inference_policy = specification["inference"]
    if inference_policy.get("market_features_allowed") is not False:
        raise RuntimeError("Independent champion inference cannot use market data")
    team_config_path = (
        ROOT / "config" / "features" / "regulation_team_state_v1.json"
    )
    rich_config_path = (
        ROOT / "config" / "features" / "regulation_rich_rate_v1.json"
    )
    walk_config_path = (
        ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
    )
    exclusions_path = (
        ROOT / "config" / "models" / "regulation_score_exclusions_v1.json"
    )
    team_config = load_team_state_feature_config(team_config_path)
    rich_config = load_rich_rate_config(rich_config_path)
    walk = load_walk_forward_config(walk_config_path)
    model = load_regulation_champion(args.model)
    if model.model_version != specification["model_version"]:
        raise RuntimeError("Model artifact and inference specification differ")

    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        historical_targets = build_regulation_score_targets(
            connection,
            reviewed_exclusions=load_regulation_target_exclusions(exclusions_path),
        )
        fixtures, metadata, audit = load_upcoming_inference_fixtures(
            connection,
            as_of=as_of,
            lookahead_days=int(inference_policy["maximum_lookahead_days"]),
            feature_config=team_config,
        )
        performance = load_fixture_performance(connection, rich_config)
    finally:
        connection.close()
    base_builder = ChronologicalTeamStateBuilder(team_config)
    historical_rows = base_builder.build(historical_targets)
    inference_rows = ChronologicalTeamStateBuilder(team_config).build_inference(
        historical_targets, fixtures, as_of=as_of
    )
    rich_rows = ChronologicalRichRateBuilder(rich_config).build_inference(
        historical_rows, inference_rows, performance
    )
    predictions = predict_regulation_moneyline(
        inference_rows,
        rich_rows,
        model,
        rich_config=rich_config,
        walk_forward_config=walk,
    )
    produced = {
        (row.fixture_id, row.information_state) for row in predictions
    }
    for item in audit["horizons"]:
        key = (item["fixture_id"], item["information_state"])
        if item["eligible_before_clean_horizon_check"] and key not in produced:
            item["eligible_before_clean_horizon_check"] = False
            item["reason"] = "intervening_team_fixture_blocks_clean_horizon"

    records = []
    for prediction in predictions:
        item = asdict(prediction)
        item["prediction_at"] = prediction.prediction_at.isoformat()
        item["kickoff"] = prediction.kickoff.isoformat()
        item["fixture"] = asdict(metadata[prediction.fixture_id])
        records.append(item)
    snapshot = {
        "snapshot_version": "upcoming_regulation_moneyline_snapshot_v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "model_version": model.model_version,
        "logical_model_sha256": champion_model_sha256(model),
        "prediction_rows_sha256": prediction_rows_sha256(predictions),
        "supported_output": inference_policy["supported_output"],
        "distribution_limitation": model.distribution_limitation,
        "training_evidence": {
            "horizon_training_fixtures": {
                item.information_state: item.training_fixtures
                for item in model.horizons
            },
            "minimum_training_fixtures": walk.minimum_training_fixtures,
            "team_cold_start_below_matches": (
                team_config.cold_start_match_threshold
            ),
            "full_signal_history_matches": (
                rich_config.full_signal_history_matches
            ),
        },
        "predictions": records,
        "audit": audit,
        "source_snapshot": {
            "warehouse": str(args.warehouse.resolve()),
            "warehouse_sha256": _file_sha256(args.warehouse),
            "model": str(args.model.resolve()),
            "model_sha256": _file_sha256(args.model),
            "model_config_sha256": _file_sha256(args.model_config),
            "team_feature_config_sha256": _file_sha256(team_config_path),
            "rich_feature_config_sha256": _file_sha256(rich_config_path),
            "walk_forward_config_sha256": _file_sha256(walk_config_path),
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    latest_path = args.output_dir / "latest.json"
    timestamp_path = args.output_dir / (
        as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    _atomic_write_json(timestamp_path, snapshot)
    _atomic_write_json(latest_path, snapshot)
    print(
        json.dumps(
            {
                "as_of": snapshot["as_of"],
                "fixtures": len({row.fixture_id for row in predictions}),
                "latest": str(latest_path.resolve()),
                "prediction_rows": len(predictions),
                "snapshot": str(timestamp_path.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_as_of(value: str | None) -> datetime:
    parsed = datetime.now(timezone.utc) if value is None else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone offset")
    return parsed


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
