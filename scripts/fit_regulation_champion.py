#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, fields
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
    RegulationInferenceFeatureRow,
    feature_rows_sha256,
    load_team_state_feature_config,
)
from soccer_bot.datasets.targets import (
    build_regulation_score_targets,
    load_regulation_target_exclusions,
)
from soccer_bot.modeling.production import (
    champion_model_sha256,
    fit_regulation_champion,
)
from soccer_bot.modeling.rich_rates import (
    ChronologicalRichRateBuilder,
    load_fixture_performance,
    load_rich_rate_config,
    rich_feature_rows_sha256,
)
from soccer_bot.modeling.reproducibility import (
    REPRODUCIBILITY_FILENAME,
    build_champion_reproducibility_manifest,
)
from soccer_bot.modeling.walk_forward import load_walk_forward_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refit the frozen regulation champion on all eligible history."
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--model-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_champion_v1.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "models" / "regulation_champion_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = {
        "team_feature_config": (
            ROOT / "config" / "features" / "regulation_team_state_v1.json"
        ),
        "rich_feature_config": (
            ROOT / "config" / "features" / "regulation_rich_rate_v1.json"
        ),
        "walk_forward_config": (
            ROOT / "config" / "models" / "regulation_walk_forward_v1.json"
        ),
        "target_exclusions": (
            ROOT / "config" / "models" / "regulation_score_exclusions_v1.json"
        ),
        "target_task": ROOT / "config" / "models" / "regulation_score_v1.json",
        "contract_registry": ROOT / "config" / "contracts" / "regulation_v1.json",
        "model_config": args.model_config,
    }
    specification = load_json(args.model_config)
    selection_report_path = ROOT / specification["selection_evidence"]["report"]
    selection_report = load_json(selection_report_path)
    if selection_report.get("evaluation_version") != specification[
        "selection_evidence"
    ]["evaluation_version"]:
        raise RuntimeError("Selection report version does not match champion spec")
    temperatures = {
        item["information_state"]: float(item["temperature"])
        for item in selection_report["moneyline_calibration"]["fits"]
        if item["model_key"] == "independent_poisson_xg_shots_correction_v1"
    }
    team_config = load_team_state_feature_config(paths["team_feature_config"])
    rich_config = load_rich_rate_config(paths["rich_feature_config"])
    walk = load_walk_forward_config(paths["walk_forward_config"])
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        targets = build_regulation_score_targets(
            connection,
            reviewed_exclusions=load_regulation_target_exclusions(
                paths["target_exclusions"]
            ),
        )
        performance = load_fixture_performance(connection, rich_config)
    finally:
        connection.close()
    feature_rows = ChronologicalTeamStateBuilder(team_config).build(targets)
    rich_rows = ChronologicalRichRateBuilder(rich_config).build(
        feature_rows, performance
    )
    model = fit_regulation_champion(
        feature_rows,
        rich_rows,
        temperatures=temperatures,
        model_specification=specification,
        rich_config=rich_config,
        walk_forward_config=walk,
    )
    logical_hash = champion_model_sha256(model)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / "model.json"
    manifest_path = args.output_dir / "manifest.json"
    reproducibility_path = args.output_dir / REPRODUCIBILITY_FILENAME
    _atomic_write_json(
        model_path,
        {
            "artifact_version": "regulation_champion_model_artifact_v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "logical_model_sha256": logical_hash,
            "model": asdict(model),
        },
    )
    training_identity = {
        "eligibility_flag": "eligible_result_models",
        "targets": len(targets),
        "feature_rows": len(feature_rows),
        "horizon_rows": dict(
            sorted(Counter(row.information_state for row in feature_rows).items())
        ),
        "kickoff_start": min(row.kickoff for row in feature_rows).isoformat(),
        "kickoff_end": max(row.kickoff for row in feature_rows).isoformat(),
        "feature_rows_sha256": feature_rows_sha256(feature_rows),
        "rich_rows_sha256": rich_feature_rows_sha256(rich_rows),
    }
    reproducibility = build_champion_reproducibility_manifest(
        repository_root=ROOT,
        model_path=model_path,
        specification=specification,
        training_identity=training_identity,
        warehouse_path=args.warehouse,
    )
    _atomic_write_json(reproducibility_path, reproducibility)
    warehouse_stat = args.warehouse.stat()
    manifest = {
        "artifact_version": "regulation_champion_manifest_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_version": model.model_version,
        "logical_model_sha256": logical_hash,
        "model_artifact": {
            "path": str(model_path.resolve()),
            "sha256": _file_sha256(model_path),
        },
        "production_refit_policy": specification["production_refit"],
        "evaluation_evidence": {
            "path": str(selection_report_path.resolve()),
            "sha256": _file_sha256(selection_report_path),
            "final_test": selection_report["final_test"],
        },
        "training": {
            **training_identity,
            "feature_schema": [field.name for field in fields(type(feature_rows[0]))],
            "inference_feature_schema": [
                field.name for field in fields(RegulationInferenceFeatureRow)
            ],
        },
        "warehouse_snapshot": {
            "path": str(args.warehouse.resolve()),
            "size_bytes": warehouse_stat.st_size,
            "modified_at": datetime.fromtimestamp(
                warehouse_stat.st_mtime, timezone.utc
            ).isoformat(),
            "sha256": reproducibility["training_warehouse"]["sha256"],
        },
        "reproducibility": {
            "path": str(reproducibility_path.resolve()),
            "sha256": _file_sha256(reproducibility_path),
        },
        "source_files": {
            name: {"path": str(path.resolve()), "sha256": _file_sha256(path)}
            for name, path in sorted(paths.items())
        },
        "selection_report": {
            "path": str(selection_report_path.resolve()),
            "sha256": _file_sha256(selection_report_path),
        },
    }
    _atomic_write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "horizons": [asdict(item) for item in model.horizons],
                "logical_model_sha256": logical_hash,
                "manifest": str(manifest_path.resolve()),
                "model": str(model_path.resolve()),
                "reproducibility": str(reproducibility_path.resolve()),
                "targets": len(targets),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


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
