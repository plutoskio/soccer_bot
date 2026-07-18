from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from soccer_bot.prediction_integrity import champion_prediction_rows_sha256


EVIDENCE_VERSION = "regulation_champion_forecast_evidence_v1"


class ChampionEvidenceError(RuntimeError):
    """Raised when a champion forecast cannot be frozen safely."""


def freeze_first_valid_predictions(
    *,
    output_directory: Path,
    predictions: Sequence[Mapping[str, object]],
    as_of: datetime,
    created_at: datetime,
    model_version: str,
    logical_model_sha256: str,
    strict_prediction_at_start: datetime,
    maximum_issue_delay: timedelta,
    issuance_policy_version: str,
    availability_policy_version: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Return immutable first-issued rows and persist their evidence records.

    Before the configured strict start, rows are retained as explicitly labelled
    legacy reconstructions. At and after it, a newly seen row must be generated
    promptly after its nominal cutoff or it is skipped instead of backdated.
    """

    as_of = _aware_utc(as_of, "as_of")
    created_at = _aware_utc(created_at, "created_at")
    strict_prediction_at_start = _aware_utc(
        strict_prediction_at_start, "strict_prediction_at_start"
    )
    if created_at < as_of:
        raise ChampionEvidenceError("created_at cannot precede as_of")
    if maximum_issue_delay <= timedelta(0):
        raise ChampionEvidenceError("maximum_issue_delay must be positive")
    if not issuance_policy_version or not availability_policy_version:
        raise ChampionEvidenceError("forecast policy versions are required")

    evidence_directory = output_directory / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    frozen: list[dict[str, object]] = []
    new_count = 0
    existing_count = 0
    skipped = []
    seen = set()

    for source in predictions:
        candidate = dict(source)
        fixture_id = _required_string(candidate, "fixture_id")
        information_state = _required_string(candidate, "information_state")
        kickoff = _timestamp(candidate.get("kickoff"), "kickoff")
        prediction_at = _timestamp(candidate.get("prediction_at"), "prediction_at")
        candidate["kickoff"] = kickoff.isoformat()
        candidate["prediction_at"] = prediction_at.isoformat()
        if prediction_at > as_of:
            raise ChampionEvidenceError("candidate horizon is not due")
        if created_at >= kickoff:
            skipped.append(_skip(candidate, "fixture_started_before_issue"))
            continue
        source_max = candidate.get("source_max_retrieved_at")
        if source_max is not None:
            source_max_timestamp = _timestamp(
                source_max, "source_max_retrieved_at"
            )
            if source_max_timestamp > prediction_at:
                raise ChampionEvidenceError(
                    "candidate uses an observation retrieved after prediction_at"
                )
            candidate["source_max_retrieved_at"] = source_max_timestamp.isoformat()

        key = _evidence_key(
            fixture_id,
            information_state,
            kickoff.isoformat(),
            logical_model_sha256,
        )
        if key in seen:
            raise ChampionEvidenceError("duplicate forecast evidence key")
        seen.add(key)
        path = evidence_directory / f"{key}.json"
        if path.exists():
            evidence = _read_object(path)
            _validate_evidence(evidence)
            if evidence["evidence_key"] != key:
                raise ChampionEvidenceError("forecast evidence key collision")
            frozen.append(dict(evidence["prediction"]))
            existing_count += 1
            continue

        strict = prediction_at >= strict_prediction_at_start
        delay = created_at - prediction_at
        if strict and delay > maximum_issue_delay:
            skipped.append(_skip(candidate, "strict_issue_window_missed"))
            continue

        candidate["issued_at"] = created_at.isoformat()
        candidate["issuance_status"] = (
            "strict_forward_frozen" if strict else "legacy_reconstructed_frozen"
        )
        candidate["issuance_policy_version"] = issuance_policy_version
        candidate["availability_policy_version"] = availability_policy_version
        candidate["immutable_prediction_sha256"] = (
            champion_prediction_rows_sha256([candidate])
        )
        evidence: dict[str, object] = {
            "evidence_version": EVIDENCE_VERSION,
            "evidence_key": key,
            "fixture_id": fixture_id,
            "information_state": information_state,
            "kickoff": kickoff.isoformat(),
            "prediction_at": prediction_at.isoformat(),
            "first_snapshot_as_of": as_of.isoformat(),
            "first_snapshot_created_at": created_at.isoformat(),
            "model_version": model_version,
            "logical_model_sha256": logical_model_sha256,
            "issuance_policy_version": issuance_policy_version,
            "availability_policy_version": availability_policy_version,
            "prediction": candidate,
        }
        evidence["evidence_record_sha256"] = _logical_sha256(evidence)
        _validate_evidence(evidence)
        _write_immutable_json(path, evidence)
        frozen.append(candidate)
        new_count += 1

    frozen.sort(
        key=lambda row: (
            _timestamp(row["kickoff"], "kickoff"),
            str(row["fixture_id"]),
            str(row["information_state"]),
        )
    )
    return frozen, {
        "new_frozen_predictions": new_count,
        "existing_frozen_predictions": existing_count,
        "skipped_predictions": skipped,
    }


def _validate_evidence(value: object) -> None:
    if not isinstance(value, dict) or value.get("evidence_version") != EVIDENCE_VERSION:
        raise ChampionEvidenceError("invalid champion forecast evidence")
    for key in (
        "evidence_key",
        "fixture_id",
        "information_state",
        "kickoff",
        "prediction_at",
        "first_snapshot_as_of",
        "first_snapshot_created_at",
        "model_version",
        "logical_model_sha256",
        "issuance_policy_version",
        "availability_policy_version",
        "evidence_record_sha256",
    ):
        _required_string(value, key)
    prediction = value.get("prediction")
    if not isinstance(prediction, dict):
        raise ChampionEvidenceError("evidence prediction must be an object")
    if prediction.get("fixture_id") != value["fixture_id"] or prediction.get(
        "information_state"
    ) != value["information_state"]:
        raise ChampionEvidenceError("evidence prediction identity mismatch")
    if prediction.get("issuance_policy_version") != value[
        "issuance_policy_version"
    ] or prediction.get("availability_policy_version") != value[
        "availability_policy_version"
    ]:
        raise ChampionEvidenceError("evidence prediction policy mismatch")
    kickoff = _timestamp(value["kickoff"], "kickoff")
    prediction_at = _timestamp(value["prediction_at"], "prediction_at")
    first_as_of = _timestamp(value["first_snapshot_as_of"], "first_snapshot_as_of")
    first_created_at = _timestamp(
        value["first_snapshot_created_at"], "first_snapshot_created_at"
    )
    issued_at = _timestamp(prediction.get("issued_at"), "issued_at")
    if _timestamp(prediction.get("kickoff"), "prediction kickoff") != kickoff or (
        _timestamp(prediction.get("prediction_at"), "prediction_at")
        != prediction_at
    ):
        raise ChampionEvidenceError("evidence prediction timestamps differ")
    if prediction_at > first_as_of or first_as_of > first_created_at:
        raise ChampionEvidenceError("forecast evidence chronology is invalid")
    if issued_at != first_created_at or issued_at >= kickoff:
        raise ChampionEvidenceError("forecast issuance chronology is invalid")
    source_max = prediction.get("source_max_retrieved_at")
    if source_max is not None and _timestamp(
        source_max, "source_max_retrieved_at"
    ) > prediction_at:
        raise ChampionEvidenceError("forecast evidence contains a late observation")
    expected_key = _evidence_key(
        str(value["fixture_id"]),
        str(value["information_state"]),
        kickoff.isoformat(),
        str(value["logical_model_sha256"]),
    )
    if value["evidence_key"] != expected_key:
        raise ChampionEvidenceError("forecast evidence key hash mismatch")
    unhashed_prediction = dict(prediction)
    unhashed_prediction.pop("immutable_prediction_sha256", None)
    if prediction.get("immutable_prediction_sha256") != (
        champion_prediction_rows_sha256([unhashed_prediction])
    ):
        raise ChampionEvidenceError("immutable prediction hash mismatch")
    expected_record_hash = _logical_sha256(
        {key: item for key, item in value.items() if key != "evidence_record_sha256"}
    )
    if value["evidence_record_sha256"] != expected_record_hash:
        raise ChampionEvidenceError("forecast evidence record hash mismatch")


def _evidence_key(
    fixture_id: str,
    information_state: str,
    kickoff: str,
    logical_model_sha256: str,
) -> str:
    return _logical_sha256(
        {
            "fixture_id": fixture_id,
            "information_state": information_state,
            "kickoff": kickoff,
            "logical_model_sha256": logical_model_sha256,
        }
    )


def _skip(candidate: Mapping[str, object], reason: str) -> dict[str, str]:
    return {
        "fixture_id": str(candidate.get("fixture_id", "")),
        "information_state": str(candidate.get("information_state", "")),
        "reason": reason,
    }


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ChampionEvidenceError(f"missing required string: {key}")
    return item


def _timestamp(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ChampionEvidenceError(f"{field} must be an ISO timestamp") from error
    else:
        raise ChampionEvidenceError(f"{field} must be an ISO timestamp")
    return _aware_utc(parsed, field)


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ChampionEvidenceError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def _logical_sha256(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ChampionEvidenceError(f"could not read evidence: {path}") from error
    if not isinstance(value, dict):
        raise ChampionEvidenceError("forecast evidence must be an object")
    return value


def _write_immutable_json(path: Path, value: Mapping[str, object]) -> None:
    body = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        stored = _read_object(path)
        _validate_evidence(stored)
        if stored != value:
            raise ChampionEvidenceError("concurrent forecast evidence differs")
    finally:
        if descriptor is not None:
            os.close(descriptor)
