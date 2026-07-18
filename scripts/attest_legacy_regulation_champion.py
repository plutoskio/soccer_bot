#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_json
from soccer_bot.modeling.reproducibility import (
    REPRODUCIBILITY_FILENAME,
    build_champion_reproducibility_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Attest the legacy champion without inventing missing evidence."
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=ROOT / "data" / "models" / "regulation_champion_v1",
    )
    parser.add_argument(
        "--model-config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_champion_v1.json",
    )
    parser.add_argument(
        "--mirror-dir",
        type=Path,
        default=ROOT / "artifacts" / "production" / "regulation_champion_v1",
    )
    args = parser.parse_args()
    legacy = load_json(args.model_dir / "manifest.json")
    training = legacy["training"]
    manifest = build_champion_reproducibility_manifest(
        repository_root=ROOT,
        model_path=args.model_dir / "model.json",
        specification=load_json(args.model_config),
        training_identity={
            "eligibility_flag": training["eligibility_flag"],
            "targets": training["targets"],
            "feature_rows": training["feature_rows"],
            "horizon_rows": training["horizon_rows"],
            "kickoff_start": training["kickoff_start"],
            "kickoff_end": training["kickoff_end"],
            "feature_rows_sha256": training["feature_rows_sha256"],
            "rich_rows_sha256": training["rich_rows_sha256"],
        },
        warehouse_path=None,
        legacy_warehouse_evidence={
            "recorded_size_bytes": legacy["warehouse_snapshot"]["size_bytes"],
            "recorded_modified_at": legacy["warehouse_snapshot"]["modified_at"],
        },
        legacy_training_implementation=True,
    )
    body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    paths = [args.model_dir / REPRODUCIBILITY_FILENAME]
    if args.mirror_dir is not None:
        mirror_model = args.mirror_dir / "model.json"
        if mirror_model.read_bytes() != (args.model_dir / "model.json").read_bytes():
            raise RuntimeError("Mirror model differs from attested model")
        paths.append(args.mirror_dir / REPRODUCIBILITY_FILENAME)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    print(
        json.dumps(
            {
                "manifests": [str(path.resolve()) for path in paths],
                "manifest_payload_sha256": manifest["manifest_payload_sha256"],
                "training_warehouse_status": manifest["training_warehouse"][
                    "status"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
