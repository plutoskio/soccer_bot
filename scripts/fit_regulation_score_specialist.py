#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.score_grid import read_rich_rate_predictions
from soccer_bot.modeling.score_specialist import (
    dump_score_specialist_model,
    fit_regulation_score_specialist,
    load_score_specialist_config,
    score_specialist_sha256,
)


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    rich_root = feature_root / "regulation_walk_forward_v1" / "rich_rate_v1"
    parser = argparse.ArgumentParser(
        description=(
            "Fit the frozen score-family specialist without claiming "
            "retrospective promotion."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_score_specialist_v1.json",
    )
    parser.add_argument(
        "--features", type=Path, default=feature_root / "features.parquet"
    )
    parser.add_argument(
        "--development-predictions",
        type=Path,
        default=rich_root / "validation_predictions.parquet",
    )
    parser.add_argument(
        "--later-predictions",
        type=Path,
        default=rich_root / "promoted_evaluation" / "predictions.parquet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "models" / "regulation_score_specialist_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_score_specialist_config(args.config)
    sources = (args.development_predictions, args.later_predictions)
    rows = []
    for path in sources:
        rows.extend(
            read_rich_rate_predictions(
                path,
                feature_path=args.features,
                kickoff_start=datetime(1970, 1, 1, tzinfo=timezone.utc),
                kickoff_end=config.training_kickoff_end_exclusive,
            )
        )
    unique = {}
    for row in rows:
        key = (row.fixture_id, row.information_state)
        if key in unique:
            raise RuntimeError(f"Historical specialist sources overlap at {key}")
        unique[key] = row
    model = fit_regulation_score_specialist(list(unique.values()), config)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / "model.json"
    temporary = args.output_dir / ".model.json.tmp"
    created_at = datetime.now(timezone.utc)
    dump_score_specialist_model(model, temporary, created_at=created_at)
    os.replace(temporary, model_path)
    manifest = {
        "artifact_version": "regulation_score_specialist_manifest_v1",
        "created_at": created_at.isoformat(),
        "status": config.status,
        "retrospective_promotion_claim": False,
        "moneyline_disagreement_policy": "diagnostic_only",
        "prospective_holdout_start": config.prospective_holdout_start.isoformat(),
        "logical_model_sha256": score_specialist_sha256(model),
        "sources": {
            "config": _source(args.config),
            "features": _source(args.features),
            "development_predictions": _source(args.development_predictions),
            "later_predictions": _source(args.later_predictions),
        },
        "model": _source(model_path),
        "horizons": [
            {
                "information_state": item.information_state,
                "training_fixtures": item.training_fixtures,
                "training_kickoff_start": item.training_kickoff_start.isoformat(),
                "training_kickoff_end_exclusive": (
                    item.training_kickoff_end_exclusive.isoformat()
                ),
                "converged": item.converged,
                "iterations": item.iterations,
            }
            for item in model.horizons
        ],
    }
    manifest_path = args.output_dir / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "model": str(model_path.resolve()),
                "manifest": str(manifest_path.resolve()),
                "logical_model_sha256": manifest["logical_model_sha256"],
                "prospective_holdout_start": manifest[
                    "prospective_holdout_start"
                ],
                "retrospective_promotion_claim": False,
                "horizons": manifest["horizons"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _source(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": _file_sha256(path)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
