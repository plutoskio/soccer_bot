from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Mapping

from soccer_bot.contracts import ScoreGrid


EVIDENCE_VERSION = "regulation_score_grid_v3_forecast_evidence_v1"
RECEIPT_VERSION = "regulation_score_grid_v3_evidence_receipt_v1"


class ProspectiveEvidenceError(RuntimeError):
    """Raised when prospective forecast evidence is not immutable and coherent."""


def materialize_legacy_evidence(output_directory: Path) -> dict[str, int]:
    """One-time import of legacy timestamped full snapshots, oldest first."""

    marker = output_directory / "evidence" / "legacy_import_v1.json"
    if marker.exists():
        value = _read_object(marker)
        if value.get("status") != "complete":
            raise ProspectiveEvidenceError("legacy evidence marker is invalid")
        return {"legacy_snapshots": 0, "new_evidence": 0}
    snapshots = sorted(
        path
        for path in output_directory.glob("*.json")
        if path.name != "latest.json"
    )
    new_count = 0
    for path in snapshots:
        snapshot = _read_object(path)
        result = materialize_snapshot_evidence(
            output_directory=output_directory,
            snapshot=snapshot,
        )
        new_count += result["new_evidence"]
    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_immutable_json(
        marker,
        {
            "status": "complete",
            "legacy_snapshots_imported": len(snapshots),
            "new_evidence": new_count,
        },
    )
    return {"legacy_snapshots": len(snapshots), "new_evidence": new_count}


def materialize_snapshot_evidence(
    *,
    output_directory: Path,
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Persist the first valid forecast for each frozen fixture/horizon pair."""

    _validate_shadow_snapshot(snapshot)
    evidence_directory = output_directory / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    new_items = []
    existing = 0
    for prediction in snapshot["predictions"]:
        evidence = _evidence_value(snapshot, prediction)
        key = str(evidence["evidence_key"])
        path = evidence_directory / f"{key}.json"
        if path.exists():
            stored = _read_object(path)
            validate_forecast_evidence(stored)
            if (
                stored["fixture_id"] != evidence["fixture_id"]
                or stored["information_state"] != evidence["information_state"]
                or stored["logical_model_sha256"]
                != evidence["logical_model_sha256"]
            ):
                raise ProspectiveEvidenceError("forecast evidence key collision")
            existing += 1
            continue
        _write_immutable_json(path, evidence)
        new_items.append(
            {
                "evidence_key": key,
                "fixture_id": evidence["fixture_id"],
                "information_state": evidence["information_state"],
                "evidence_file_sha256": _file_sha256(path),
            }
        )
    receipt_path = None
    if new_items:
        timestamp = _timestamp(snapshot["as_of"]).strftime("%Y%m%dT%H%M%S%fZ")
        receipt = {
            "receipt_version": RECEIPT_VERSION,
            "snapshot_as_of": snapshot["as_of"],
            "snapshot_created_at": snapshot["created_at"],
            "snapshot_logical_sha256": _logical_sha256(snapshot),
            "new_evidence": sorted(new_items, key=lambda item: item["evidence_key"]),
        }
        receipt_path = output_directory / "receipts" / f"{timestamp}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_immutable_json(receipt_path, receipt)
    return {
        "new_evidence": len(new_items),
        "existing_evidence": existing,
        "receipt_path": str(receipt_path) if receipt_path else None,
    }


def load_forecast_evidence(directory: Path) -> list[dict[str, object]]:
    values = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "legacy_import_v1.json":
            continue
        value = _read_object(path)
        validate_forecast_evidence(value)
        value["evidence_file_path"] = str(path.resolve())
        value["evidence_file_sha256"] = _file_sha256(path)
        values.append(value)
    return values


def validate_forecast_evidence(value: object) -> None:
    if not isinstance(value, dict):
        raise ProspectiveEvidenceError("forecast evidence must be an object")
    if value.get("evidence_version") != EVIDENCE_VERSION:
        raise ProspectiveEvidenceError("unexpected forecast evidence version")
    required_strings = (
        "evidence_key",
        "fixture_id",
        "information_state",
        "first_snapshot_as_of",
        "first_snapshot_created_at",
        "model_version",
        "logical_model_sha256",
        "prospective_gate_version",
        "prospective_holdout_start",
    )
    for key in required_strings:
        if not isinstance(value.get(key), str) or not value[key]:
            raise ProspectiveEvidenceError(f"forecast evidence missing {key}")
    prediction = value.get("prediction")
    if not isinstance(prediction, dict):
        raise ProspectiveEvidenceError("forecast evidence prediction must be an object")
    if prediction.get("fixture_id") != value["fixture_id"] or prediction.get(
        "information_state"
    ) != value["information_state"]:
        raise ProspectiveEvidenceError("forecast evidence identity mismatch")
    _validate_prediction(prediction)
    _validate_sources(value.get("sources"))
    expected_key = _evidence_key(
        str(value["fixture_id"]),
        str(value["information_state"]),
        str(value["logical_model_sha256"]),
    )
    if value["evidence_key"] != expected_key:
        raise ProspectiveEvidenceError("forecast evidence key hash mismatch")
    as_of = _timestamp(value["first_snapshot_as_of"])
    created_at = _timestamp(value["first_snapshot_created_at"])
    prediction_at = _timestamp(prediction.get("prediction_at"))
    kickoff = _timestamp(prediction.get("kickoff"))
    holdout = _timestamp(value["prospective_holdout_start"])
    if as_of < holdout or kickoff < holdout:
        raise ProspectiveEvidenceError("forecast evidence predates holdout")
    if prediction_at > as_of:
        raise ProspectiveEvidenceError("forecast evidence predates prediction horizon")
    if created_at < as_of:
        raise ProspectiveEvidenceError("forecast creation predates snapshot as-of")
    if created_at >= kickoff or as_of >= kickoff:
        raise ProspectiveEvidenceError("forecast evidence is not pre-kickoff")
    expected_hash = _logical_sha256(
        {key: item for key, item in value.items() if key != "evidence_record_sha256"}
    )
    if value.get("evidence_record_sha256") != expected_hash:
        raise ProspectiveEvidenceError("forecast evidence record hash mismatch")


def _evidence_value(
    snapshot: Mapping[str, object], prediction: Mapping[str, object]
) -> dict[str, object]:
    fixture_id = _required_string(prediction, "fixture_id")
    information_state = _required_string(prediction, "information_state")
    model_hash = _required_string(snapshot, "logical_model_sha256")
    value: dict[str, object] = {
        "evidence_version": EVIDENCE_VERSION,
        "evidence_key": _evidence_key(fixture_id, information_state, model_hash),
        "fixture_id": fixture_id,
        "information_state": information_state,
        "first_snapshot_as_of": snapshot["as_of"],
        "first_snapshot_created_at": snapshot["created_at"],
        "snapshot_logical_sha256": _logical_sha256(snapshot),
        "model_version": snapshot["model_version"],
        "logical_model_sha256": model_hash,
        "parent_model_version": snapshot["parent_model_version"],
        "prospective_gate_version": snapshot["prospective_gate_version"],
        "prospective_holdout_start": snapshot["prospective_holdout_start"],
        "sources": snapshot.get("sources"),
        "prediction": prediction,
    }
    value["evidence_record_sha256"] = _logical_sha256(value)
    validate_forecast_evidence(value)
    return value


def _validate_shadow_snapshot(value: Mapping[str, object]) -> None:
    if value.get("snapshot_version") != "regulation_score_grid_v3_shadow_snapshot_v1":
        raise ProspectiveEvidenceError("unexpected shadow snapshot version")
    for key in (
        "created_at",
        "as_of",
        "model_version",
        "logical_model_sha256",
        "parent_model_version",
        "prospective_gate_version",
        "prospective_holdout_start",
    ):
        _required_string(value, key)
    if not isinstance(value.get("predictions"), list):
        raise ProspectiveEvidenceError("shadow predictions must be a list")
    seen = set()
    for prediction in value["predictions"]:
        if not isinstance(prediction, dict):
            raise ProspectiveEvidenceError("shadow prediction must be an object")
        key = (
            _required_string(prediction, "fixture_id"),
            _required_string(prediction, "information_state"),
        )
        if key in seen:
            raise ProspectiveEvidenceError("duplicate shadow prediction")
        seen.add(key)


def _validate_prediction(prediction: Mapping[str, object]) -> None:
    for key in ("expected_home_goals", "expected_away_goals"):
        try:
            value = float(prediction[key])
        except (KeyError, TypeError, ValueError) as error:
            raise ProspectiveEvidenceError(f"forecast evidence has invalid {key}") from error
        if not math.isfinite(value) or value <= 0:
            raise ProspectiveEvidenceError(f"forecast evidence has invalid {key}")
    cells = prediction.get("score_grid")
    if not isinstance(cells, list) or not cells:
        raise ProspectiveEvidenceError("forecast evidence score grid is missing")
    probabilities = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise ProspectiveEvidenceError("forecast evidence score cell is invalid")
        home = cell.get("home_goals")
        away = cell.get("away_goals")
        probability = cell.get("probability")
        if (
            isinstance(home, bool)
            or not isinstance(home, int)
            or isinstance(away, bool)
            or not isinstance(away, int)
            or home < 0
            or away < 0
        ):
            raise ProspectiveEvidenceError("forecast evidence score is invalid")
        try:
            probability = float(probability)
        except (TypeError, ValueError) as error:
            raise ProspectiveEvidenceError(
                "forecast evidence score probability is invalid"
            ) from error
        if not math.isfinite(probability) or probability <= 0:
            raise ProspectiveEvidenceError(
                "forecast evidence score probability is invalid"
            )
        if (home, away) in probabilities:
            raise ProspectiveEvidenceError("forecast evidence score is duplicated")
        probabilities[(home, away)] = probability
    try:
        grid = ScoreGrid(probabilities, tolerance=1e-10)
    except (TypeError, ValueError) as error:
        raise ProspectiveEvidenceError("forecast evidence score grid is invalid") from error
    if prediction.get("score_grid_sha256") != _grid_sha256(probabilities):
        raise ProspectiveEvidenceError("forecast evidence score-grid hash mismatch")
    parent = _probability_triplet(prediction.get("parent_moneyline"), "parent")
    implied = _probability_triplet(prediction.get("implied_moneyline"), "implied")
    grid_moneyline = grid.moneyline()
    if any(abs(grid_moneyline[key] - parent[key]) > 1e-10 for key in parent):
        raise ProspectiveEvidenceError("forecast evidence changed parent moneyline")
    if any(abs(grid_moneyline[key] - implied[key]) > 1e-10 for key in implied):
        raise ProspectiveEvidenceError("forecast evidence implied moneyline is invalid")


def _probability_triplet(value: object, label: str) -> dict[str, float]:
    expected = {"home_win", "draw", "away_win"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProspectiveEvidenceError(
            f"forecast evidence {label} moneyline is invalid"
        )
    try:
        probabilities = {key: float(value[key]) for key in expected}
    except (TypeError, ValueError) as error:
        raise ProspectiveEvidenceError(
            f"forecast evidence {label} moneyline is invalid"
        ) from error
    if any(not math.isfinite(item) or item <= 0 for item in probabilities.values()) or not math.isclose(
        math.fsum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-10
    ):
        raise ProspectiveEvidenceError(
            f"forecast evidence {label} moneyline is invalid"
        )
    return probabilities


def _validate_sources(value: object) -> None:
    required = {"parent_snapshot", "shadow_model", "prospective_gate"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ProspectiveEvidenceError("forecast evidence sources are invalid")
    for name in required:
        source = value[name]
        digest = source.get("sha256") if isinstance(source, Mapping) else None
        if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ProspectiveEvidenceError(
                f"forecast evidence {name} source hash is invalid"
            )


def _evidence_key(fixture_id: str, information_state: str, model_hash: str) -> str:
    body = json.dumps(
        [fixture_id, information_state, model_hash],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ProspectiveEvidenceError(f"missing non-empty {key}")
    return item


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProspectiveEvidenceError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProspectiveEvidenceError("invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ProspectiveEvidenceError("timestamp must have timezone")
    return parsed.astimezone(timezone.utc)


def _logical_sha256(value: Mapping[str, object]) -> str:
    body = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _grid_sha256(probabilities: Mapping[tuple[int, int], float]) -> str:
    body = json.dumps(
        [
            [score[0], score[1], probability]
            for score, probability in sorted(probabilities.items())
        ],
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProspectiveEvidenceError(f"could not read evidence file {path.name}") from error
    if not isinstance(value, dict):
        raise ProspectiveEvidenceError(f"evidence file {path.name} is not an object")
    return value


def _write_immutable_json(path: Path, value: Mapping[str, object]) -> None:
    body = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise ProspectiveEvidenceError(
                f"immutable evidence already exists with different bytes: {path.name}"
            )
        return
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
