from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Mapping


REPRODUCIBILITY_VERSION = "regulation_champion_reproducibility_v1"
REPRODUCIBILITY_FILENAME = "reproducibility.json"

SERVING_BUNDLE_FILES = (
    "config/contracts/regulation_v1.json",
    "config/features/regulation_rich_rate_v1.json",
    "config/features/regulation_team_state_v1.json",
    "config/models/regulation_champion_v1.json",
    "config/models/regulation_score_exclusions_v1.json",
    "config/models/regulation_walk_forward_v1.json",
    "scripts/predict_upcoming_regulation.py",
    "src/soccer_bot/champion_evidence.py",
    "src/soccer_bot/datasets/features.py",
    "src/soccer_bot/datasets/targets.py",
    "src/soccer_bot/datasets/upcoming.py",
    "src/soccer_bot/modeling/production.py",
    "src/soccer_bot/modeling/reproducibility.py",
    "src/soccer_bot/modeling/rich_rates.py",
    "src/soccer_bot/prediction_integrity.py",
    "src/soccer_bot/prediction_publication.py",
)

TRAINING_IMPLEMENTATION_FILES = (
    "scripts/fit_regulation_champion.py",
    "src/soccer_bot/datasets/features.py",
    "src/soccer_bot/datasets/targets.py",
    "src/soccer_bot/modeling/production.py",
    "src/soccer_bot/modeling/rich_rates.py",
)


class ChampionReproducibilityError(RuntimeError):
    """Raised when a champion artifact cannot prove its recorded identity."""


def champion_training_recipe_sha256(specification: Mapping[str, object]) -> str:
    """Hash only model-fitting policy, excluding mutable serving policy."""

    required = (
        "model_version",
        "contract",
        "model_class",
        "feature_version",
        "rich_feature_version",
        "selection_evidence",
        "production_refit",
        "parameter_status",
    )
    missing = [key for key in required if key not in specification]
    if missing:
        raise ChampionReproducibilityError(
            "Champion specification is missing training fields: "
            + ", ".join(missing)
        )
    return logical_sha256({key: specification[key] for key in required})


def build_champion_reproducibility_manifest(
    *,
    repository_root: Path,
    model_path: Path,
    specification: Mapping[str, object],
    training_identity: Mapping[str, object],
    warehouse_path: Path | None,
    legacy_warehouse_evidence: Mapping[str, object] | None = None,
    legacy_training_implementation: bool = False,
) -> dict[str, object]:
    """Build a compact, self-verifying identity record without copying data."""

    model_artifact = _read_object(model_path)
    model_version = _required_string(model_artifact.get("model"), "model_version")
    logical_model_hash = _required_sha256(
        model_artifact.get("logical_model_sha256"), "logical_model_sha256"
    )
    if warehouse_path is not None:
        stat = warehouse_path.stat()
        warehouse: dict[str, object] = {
            "status": "verified_at_fit",
            "sha256": file_sha256(warehouse_path),
            "size_bytes": stat.st_size,
        }
    else:
        if legacy_warehouse_evidence is None:
            raise ChampionReproducibilityError(
                "Legacy warehouse evidence must be explicit"
            )
        warehouse = {
            "status": "unavailable_for_legacy_artifact",
            "reason": "whole_file_sha256_was_not_recorded_at_fit_time",
            **dict(legacy_warehouse_evidence),
        }
    value: dict[str, object] = {
        "reproducibility_version": REPRODUCIBILITY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_version": model_version,
        "logical_model_sha256": logical_model_hash,
        "model_artifact": {
            "filename": model_path.name,
            "sha256": file_sha256(model_path),
        },
        "training_recipe_sha256": champion_training_recipe_sha256(specification),
        "training_identity": dict(training_identity),
        "training_warehouse": warehouse,
        "training_implementation": (
            {
                "status": "unavailable_for_legacy_artifact",
                "reason": "source_file_hashes_were_not_recorded_at_fit_time",
            }
            if legacy_training_implementation
            else {
                "status": "verified_at_fit",
                "files": _file_fingerprints(
                    repository_root, TRAINING_IMPLEMENTATION_FILES
                ),
            }
        ),
        "source_revision": _source_revision(repository_root),
        "serving_bundle": {
            "files": _file_fingerprints(repository_root, SERVING_BUNDLE_FILES),
        },
    }
    value["serving_bundle"]["logical_sha256"] = logical_sha256(
        value["serving_bundle"]["files"]
    )
    value["manifest_payload_sha256"] = logical_sha256(value)
    validate_champion_reproducibility_value(value)
    return value


def validate_champion_reproducibility(
    *,
    model_path: Path,
    model_config_path: Path,
    repository_root: Path,
) -> dict[str, object]:
    manifest_path = model_path.with_name(REPRODUCIBILITY_FILENAME)
    if not manifest_path.is_file():
        raise ChampionReproducibilityError(
            f"Champion reproducibility manifest is missing: {manifest_path}"
        )
    value = _read_object(manifest_path)
    validate_champion_reproducibility_value(value)
    if value["model_artifact"]["filename"] != model_path.name or value[
        "model_artifact"
    ]["sha256"] != file_sha256(model_path):
        raise ChampionReproducibilityError("Champion model artifact hash mismatch")
    artifact = _read_object(model_path)
    if artifact.get("logical_model_sha256") != value["logical_model_sha256"]:
        raise ChampionReproducibilityError("Champion logical model hash mismatch")
    specification = _read_object(model_config_path)
    if champion_training_recipe_sha256(specification) != value[
        "training_recipe_sha256"
    ]:
        raise ChampionReproducibilityError("Champion training recipe mismatch")
    observed_files = _file_fingerprints(repository_root, SERVING_BUNDLE_FILES)
    if observed_files != value["serving_bundle"]["files"]:
        raise ChampionReproducibilityError("Champion serving bundle mismatch")
    if logical_sha256(observed_files) != value["serving_bundle"]["logical_sha256"]:
        raise ChampionReproducibilityError("Champion serving bundle hash mismatch")
    return value


def validate_champion_reproducibility_value(value: object) -> None:
    if not isinstance(value, dict):
        raise ChampionReproducibilityError("Reproducibility manifest must be an object")
    if value.get("reproducibility_version") != REPRODUCIBILITY_VERSION:
        raise ChampionReproducibilityError("Unexpected reproducibility version")
    _required_string(value, "model_version")
    _required_sha256(value.get("logical_model_sha256"), "logical_model_sha256")
    _required_sha256(value.get("training_recipe_sha256"), "training_recipe_sha256")
    model = _required_mapping(value, "model_artifact")
    _required_string(model, "filename")
    _required_sha256(model.get("sha256"), "model artifact sha256")
    training = _required_mapping(value, "training_identity")
    for key in ("feature_rows_sha256", "rich_rows_sha256"):
        _required_sha256(training.get(key), f"training identity {key}")
    for key in ("targets", "feature_rows"):
        item = training.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ChampionReproducibilityError(
                f"Training identity {key} must be a positive integer"
            )
    warehouse = _required_mapping(value, "training_warehouse")
    status = warehouse.get("status")
    if status == "verified_at_fit":
        _required_sha256(warehouse.get("sha256"), "training warehouse sha256")
    elif status != "unavailable_for_legacy_artifact":
        raise ChampionReproducibilityError("Invalid training warehouse status")
    implementation = _required_mapping(value, "training_implementation")
    implementation_status = implementation.get("status")
    if implementation_status == "verified_at_fit":
        implementation_files = _required_mapping(implementation, "files")
        if set(implementation_files) != set(TRAINING_IMPLEMENTATION_FILES):
            raise ChampionReproducibilityError(
                "Training implementation file set mismatch"
            )
        for path, fingerprint in implementation_files.items():
            _required_sha256(fingerprint, f"training implementation {path}")
    elif implementation_status != "unavailable_for_legacy_artifact":
        raise ChampionReproducibilityError(
            "Invalid training implementation status"
        )
    serving = _required_mapping(value, "serving_bundle")
    files = _required_mapping(serving, "files")
    if set(files) != set(SERVING_BUNDLE_FILES):
        raise ChampionReproducibilityError("Serving bundle file set mismatch")
    for path, fingerprint in files.items():
        _required_sha256(fingerprint, f"serving file {path}")
    if serving.get("logical_sha256") != logical_sha256(files):
        raise ChampionReproducibilityError("Serving bundle logical hash mismatch")
    expected_payload_hash = logical_sha256(
        {key: item for key, item in value.items() if key != "manifest_payload_sha256"}
    )
    if value.get("manifest_payload_sha256") != expected_payload_hash:
        raise ChampionReproducibilityError("Reproducibility payload hash mismatch")


def reproducibility_file_sha256(model_path: Path) -> str:
    path = model_path.with_name(REPRODUCIBILITY_FILENAME)
    if not path.is_file():
        raise ChampionReproducibilityError("Champion reproducibility manifest missing")
    return file_sha256(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def logical_sha256(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _file_fingerprints(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    values = {}
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise ChampionReproducibilityError(
                f"Serving bundle file is missing: {relative}"
            )
        values[relative] = file_sha256(path)
    return values


def _source_revision(root: Path) -> dict[str, object]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", *SERVING_BUNDLE_FILES],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return {"git_commit": commit, "serving_bundle_dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": None, "serving_bundle_dirty": None}


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChampionReproducibilityError(f"Could not read JSON: {path}") from error
    if not isinstance(value, dict):
        raise ChampionReproducibilityError(f"Expected JSON object: {path}")
    return value


def _required_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ChampionReproducibilityError(f"Missing object: {key}")
    return item


def _required_string(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        raise ChampionReproducibilityError(f"Missing object containing {key}")
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ChampionReproducibilityError(f"Missing string: {key}")
    return item


def _required_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ChampionReproducibilityError(f"Invalid SHA-256: {field}")
    return value
