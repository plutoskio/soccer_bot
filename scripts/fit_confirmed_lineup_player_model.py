#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import gzip
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.players import read_player_target_artifact
from soccer_bot.modeling.player_hierarchy import (
    fit_confirmed_lineup_player_model,
    load_player_hierarchy_config,
    player_model_sha256,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fit the frozen confirmed-lineup player shadow hierarchy."
    )
    parser.add_argument(
        "--targets", type=Path,
        default=ROOT / "data" / "features" / "confirmed_lineup_player_v1" / "targets.parquet",
    )
    parser.add_argument(
        "--dataset-manifest", type=Path,
        default=ROOT / "data" / "features" / "confirmed_lineup_player_v1" / "manifest.json",
    )
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "config" / "models" / "confirmed_lineup_player_v1.json",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "data" / "models" / "confirmed_lineup_player_v1",
    )
    parser.add_argument(
        "--model-filename", default="model.json",
        choices=("model.json", "model.json.gz"),
    )
    args = parser.parse_args()
    config = load_player_hierarchy_config(args.config)
    rows = read_player_target_artifact(args.targets)
    model = fit_confirmed_lineup_player_model(rows, config)
    logical_hash = player_model_sha256(model)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / args.model_filename
    value = asdict(model)
    value["fit_end_exclusive"] = model.fit_end_exclusive.isoformat()
    _atomic_write_json(
        model_path,
        {
            "artifact_version": "confirmed_lineup_player_model_artifact_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "logical_model_sha256": logical_hash,
            "model": value,
        },
    )
    manifest = {
        "artifact_version": "confirmed_lineup_player_model_manifest_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_version": model.model_version,
        "logical_model_sha256": logical_hash,
        "model": {"path": _manifest_path(model_path), "sha256": _file_sha256(model_path)},
        "training": {
            "rows": model.training_rows,
            "fixtures": model.training_fixtures,
            "players": model.training_players,
            "fit_end_exclusive": model.fit_end_exclusive.isoformat(),
        },
        "safety": {
            "historical_confirmed_lineup_evaluation": False,
            "prospective_shadow_only": True,
            "apply_to_public_champion": False,
            "market_features_used": False,
            "trading_actions": False,
        },
        "sources": {
            "targets": {"path": _manifest_path(args.targets), "sha256": _file_sha256(args.targets)},
            "dataset_manifest": {"path": _manifest_path(args.dataset_manifest), "sha256": _file_sha256(args.dataset_manifest)},
            "config": {"path": _manifest_path(args.config), "sha256": _file_sha256(args.config)},
        },
    }
    _atomic_write_json(args.output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    body = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.suffix == ".gz":
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            handle.write(body)
    else:
        temporary.write_text(body)
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT.resolve()))
    except ValueError:
        return str(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
