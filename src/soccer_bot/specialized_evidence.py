from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping


EVIDENCE_VERSION = "specialized_family_forecast_evidence_v1"
RECEIPT_VERSION = "specialized_family_evidence_receipt_v1"


class SpecializedEvidenceError(RuntimeError):
    """Raised when specialized-family forward evidence is unsafe or mutable."""


def materialize_specialized_evidence(
    *,
    output_directory: Path,
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Persist the first valid experimental family forecast for each state."""

    states = snapshot.get("states")
    if not isinstance(states, list):
        raise SpecializedEvidenceError("platform snapshot states must be a list")
    evidence_directory = output_directory / "forward_evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    new_items: list[dict[str, str]] = []
    existing = 0
    eligible = 0
    for state in states:
        if not isinstance(state, Mapping):
            raise SpecializedEvidenceError("platform state must be an object")
        families = state.get("families")
        if not isinstance(families, list):
            raise SpecializedEvidenceError("platform families must be a list")
        for family in families:
            if not isinstance(family, Mapping) or family.get("status") != "experimental":
                continue
            eligible += 1
            evidence = _evidence_value(snapshot, state, family)
            path = evidence_directory / f"{evidence['evidence_key']}.json"
            if path.exists():
                stored = _read_object(path)
                validate_specialized_evidence(stored)
                for key in (
                    "fixture_id",
                    "information_state",
                    "family_key",
                    "logical_model_sha256",
                ):
                    if stored[key] != evidence[key]:
                        raise SpecializedEvidenceError("forward evidence key collision")
                existing += 1
                continue
            _write_immutable_json(path, evidence)
            new_items.append(
                {
                    "evidence_key": str(evidence["evidence_key"]),
                    "fixture_id": str(evidence["fixture_id"]),
                    "information_state": str(evidence["information_state"]),
                    "family_key": str(evidence["family_key"]),
                    "evidence_file_sha256": _file_sha256(path),
                }
            )

    receipt_path = None
    if new_items:
        timestamp = _timestamp(snapshot.get("created_at"), "created_at").strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        receipt = {
            "receipt_version": RECEIPT_VERSION,
            "snapshot_created_at": snapshot["created_at"],
            "snapshot_as_of": snapshot.get("as_of"),
            "snapshot_state_rows_sha256": snapshot.get("state_rows_sha256"),
            "new_evidence": sorted(
                new_items,
                key=lambda item: (
                    item["fixture_id"],
                    item["information_state"],
                    item["family_key"],
                ),
            ),
        }
        receipt_path = output_directory / "forward_receipts" / f"{timestamp}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_immutable_json(receipt_path, receipt)
    return {
        "eligible_experimental_forecasts": eligible,
        "new_evidence": len(new_items),
        "existing_evidence": existing,
        "receipt_path": None if receipt_path is None else str(receipt_path),
    }


def validate_specialized_evidence(value: object) -> None:
    if not isinstance(value, dict):
        raise SpecializedEvidenceError("forward evidence must be an object")
    if value.get("evidence_version") != EVIDENCE_VERSION:
        raise SpecializedEvidenceError("unexpected forward evidence version")
    for key in (
        "evidence_key",
        "fixture_id",
        "information_state",
        "family_key",
        "model_version",
        "logical_model_sha256",
        "first_snapshot_created_at",
        "first_snapshot_as_of",
        "kickoff",
        "prediction_at",
        "issued_at",
        "prospective_holdout_start",
    ):
        if not isinstance(value.get(key), str) or not value[key]:
            raise SpecializedEvidenceError(f"forward evidence missing {key}")
    if len(value["logical_model_sha256"]) != 64:
        raise SpecializedEvidenceError("invalid forward model hash")
    family = value.get("family")
    if not isinstance(family, dict) or family.get("status") != "experimental":
        raise SpecializedEvidenceError("forward evidence family must be experimental")
    if family.get("eligible_for_ranking") is not False:
        raise SpecializedEvidenceError("experimental evidence cannot enter ranking")
    if not isinstance(family.get("markets"), list) or not family["markets"]:
        raise SpecializedEvidenceError("experimental evidence has no markets")
    expected_key = _evidence_key(
        value["fixture_id"],
        value["information_state"],
        value["family_key"],
        value["logical_model_sha256"],
    )
    if value["evidence_key"] != expected_key:
        raise SpecializedEvidenceError("forward evidence key hash mismatch")
    created_at = _timestamp(value["first_snapshot_created_at"], "created_at")
    as_of = _timestamp(value["first_snapshot_as_of"], "as_of")
    kickoff = _timestamp(value["kickoff"], "kickoff")
    prediction_at = _timestamp(value["prediction_at"], "prediction_at")
    issued_at = _timestamp(value["issued_at"], "issued_at")
    holdout = _timestamp(value["prospective_holdout_start"], "holdout")
    if kickoff < holdout or as_of < holdout:
        raise SpecializedEvidenceError("forward evidence predates holdout")
    if prediction_at > as_of or issued_at > created_at:
        raise SpecializedEvidenceError("forward evidence chronology is invalid")
    if created_at >= kickoff or issued_at >= kickoff or as_of >= kickoff:
        raise SpecializedEvidenceError("forward evidence is not pre-kickoff")
    unhashed = {key: item for key, item in value.items() if key != "evidence_record_sha256"}
    if value.get("evidence_record_sha256") != _logical_sha256(unhashed):
        raise SpecializedEvidenceError("forward evidence record hash mismatch")


def _evidence_value(snapshot, state, family) -> dict[str, object]:
    evidence = family.get("evidence")
    if not isinstance(evidence, Mapping):
        raise SpecializedEvidenceError("experimental family evidence is missing")
    holdout = evidence.get("prospective_holdout_start")
    if not isinstance(holdout, str) or not holdout:
        raise SpecializedEvidenceError("experimental family holdout is missing")
    value: dict[str, object] = {
        "evidence_version": EVIDENCE_VERSION,
        "evidence_key": _evidence_key(
            str(state.get("fixture_id")),
            str(state.get("information_state")),
            str(family.get("family_key")),
            str(family.get("logical_model_sha256")),
        ),
        "fixture_id": state.get("fixture_id"),
        "information_state": state.get("information_state"),
        "family_key": family.get("family_key"),
        "model_version": family.get("model_version"),
        "logical_model_sha256": family.get("logical_model_sha256"),
        "first_snapshot_created_at": snapshot.get("created_at"),
        "first_snapshot_as_of": snapshot.get("as_of"),
        "snapshot_state_rows_sha256": snapshot.get("state_rows_sha256"),
        "kickoff": state.get("kickoff"),
        "prediction_at": state.get("prediction_at"),
        "issued_at": state.get("issued_at"),
        "prospective_holdout_start": holdout,
        "fixture": state.get("fixture"),
        "family": family,
        "source_hashes": snapshot.get("source_hashes"),
    }
    value["evidence_record_sha256"] = _logical_sha256(value)
    validate_specialized_evidence(value)
    return value


def _evidence_key(
    fixture_id: str,
    information_state: str,
    family_key: str,
    logical_model_sha256: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (fixture_id, information_state, family_key, logical_model_sha256)
        ).encode("utf-8")
    ).hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise SpecializedEvidenceError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SpecializedEvidenceError(f"{field} is invalid") from error
    if parsed.tzinfo is None:
        raise SpecializedEvidenceError(f"{field} must include a timezone")
    return parsed


def _logical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SpecializedEvidenceError("stored forward evidence is unreadable") from error
    if not isinstance(value, dict):
        raise SpecializedEvidenceError("stored forward evidence must be an object")
    return value


def _write_immutable_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError as error:
        raise SpecializedEvidenceError(f"immutable file already exists: {path.name}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
