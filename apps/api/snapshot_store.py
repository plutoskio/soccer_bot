from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from soccer_bot.prediction_integrity import champion_prediction_rows_sha256


DEFAULT_SNAPSHOT_PATH = Path(
    "data/predictions/regulation_champion_v1/latest.json"
)
SUPPORTED_OUTPUT = "regulation_moneyline"
SUPPORTED_INFORMATION_STATES = {
    "pre_lineup_72h_clean_v1",
    "pre_lineup_24h_v1",
}
SUPPORTED_SNAPSHOT_VERSIONS = {
    "upcoming_regulation_moneyline_snapshot_v2",
    "upcoming_regulation_moneyline_snapshot_v3",
}


class SnapshotUnavailableError(RuntimeError):
    """Raised when the immutable prediction snapshot cannot be served."""


class SnapshotValidationError(ValueError):
    """Raised when a snapshot violates the serving contract."""


class SnapshotStore:
    """Load and cache a validated prediction snapshot from a local read-only path.

    The API service never opens DuckDB. A producer publishes an immutable JSON
    snapshot and atomically updates the configured ``latest.json`` path.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cached_mtime_ns: int | None = None
        self._cached_snapshot: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as exc:
            raise SnapshotUnavailableError(
                f"Prediction snapshot is unavailable at {self.path}"
            ) from exc

        with self._lock:
            if self._cached_snapshot is not None and self._cached_mtime_ns == mtime_ns:
                return deepcopy(self._cached_snapshot)
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotUnavailableError(
                    "Prediction snapshot could not be read as JSON"
                ) from exc
            validate_snapshot(value)
            public_value = public_snapshot(value)
            self._cached_snapshot = public_value
            self._cached_mtime_ns = mtime_ns
            return deepcopy(public_value)


class S3SnapshotStore:
    """Read a latest snapshot object from S3-compatible immutable storage."""

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        key: str,
        cache_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.key = key
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_snapshot: dict[str, Any] | None = None
        self._cached_etag: str | None = None
        self._refresh_after = 0.0

    def load(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cached_snapshot is not None and now < self._refresh_after:
                return deepcopy(self._cached_snapshot)
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.key)
                body = response["Body"].read()
                etag = str(response.get("ETag", ""))
            except Exception as exc:
                if self._cached_snapshot is not None:
                    self._refresh_after = now + min(self.cache_seconds, 5.0)
                    return deepcopy(self._cached_snapshot)
                raise SnapshotUnavailableError(
                    "Prediction snapshot could not be read from object storage"
                ) from exc
            if self._cached_snapshot is not None and etag and etag == self._cached_etag:
                self._refresh_after = now + self.cache_seconds
                return deepcopy(self._cached_snapshot)
            try:
                value = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SnapshotUnavailableError(
                    "Object-storage prediction snapshot is not valid JSON"
                ) from exc
            validate_snapshot(value)
            self._cached_snapshot = public_snapshot(value)
            self._cached_etag = etag
            self._refresh_after = now + self.cache_seconds
            return deepcopy(self._cached_snapshot)


def validate_snapshot(value: object) -> None:
    if not isinstance(value, dict):
        raise SnapshotValidationError("Snapshot must be a JSON object")
    for key in (
        "snapshot_version",
        "model_version",
        "logical_model_sha256",
        "prediction_rows_sha256",
        "as_of",
        "created_at",
        "supported_output",
        "training_evidence",
        "predictions",
    ):
        if key not in value:
            raise SnapshotValidationError(f"Snapshot is missing {key}")
    if value["supported_output"] != SUPPORTED_OUTPUT:
        raise SnapshotValidationError(
            f"Unsupported snapshot output: {value['supported_output']!r}"
        )
    snapshot_version = value["snapshot_version"]
    if snapshot_version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise SnapshotValidationError(
            f"Unsupported snapshot version: {snapshot_version!r}"
        )
    strict_v3 = snapshot_version == "upcoming_regulation_moneyline_snapshot_v3"
    if strict_v3:
        if not _is_sha256(value.get("model_reproducibility_sha256")):
            raise SnapshotValidationError("Invalid model reproducibility SHA-256")
        for key in ("availability_policy", "issuance_policy"):
            if not isinstance(value.get(key), dict):
                raise SnapshotValidationError(f"Snapshot is missing {key}")
    as_of = _parse_timestamp(value["as_of"], "as_of")
    created_at = _parse_timestamp(value["created_at"], "created_at")
    if created_at < as_of:
        raise SnapshotValidationError("created_at cannot precede as_of")
    _validate_training_evidence(value["training_evidence"])
    predictions = value["predictions"]
    if not isinstance(predictions, list):
        raise SnapshotValidationError("predictions must be a list")
    keys: set[tuple[str, str]] = set()
    for index, prediction in enumerate(predictions):
        _validate_prediction(
            prediction,
            index,
            strict_v3=strict_v3,
            availability_policy=value.get("availability_policy"),
            issuance_policy=value.get("issuance_policy"),
            snapshot_created_at=created_at,
        )
        key = (prediction["fixture_id"], prediction["information_state"])
        if key in keys:
            raise SnapshotValidationError(
                f"Duplicate fixture/information-state prediction: {key}"
            )
        keys.add(key)
    try:
        observed_hash = champion_prediction_rows_sha256(predictions)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            "Prediction rows could not be canonically hashed"
        ) from exc
    if value["prediction_rows_sha256"] != observed_hash:
        raise SnapshotValidationError("Prediction rows SHA-256 mismatch")


def public_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Return the stable public payload, excluding local provenance paths."""

    predictions = sorted(
        deepcopy(value["predictions"]),
        key=lambda row: (
            _parse_timestamp(row["kickoff"], "kickoff"),
            row["fixture_id"],
            row["information_state"],
        ),
    )
    fixture_count = len({row["fixture_id"] for row in predictions})
    available_states = sorted({row["information_state"] for row in predictions})
    return {
        "snapshot_version": value["snapshot_version"],
        "model_version": value["model_version"],
        "logical_model_sha256": value["logical_model_sha256"],
        "model_reproducibility_sha256": value.get(
            "model_reproducibility_sha256"
        ),
        "prediction_rows_sha256": value["prediction_rows_sha256"],
        "created_at": value["created_at"],
        "as_of": value["as_of"],
        "supported_output": value["supported_output"],
        "distribution_limitation": value.get("distribution_limitation"),
        "availability_policy": deepcopy(value.get("availability_policy")),
        "issuance_policy": deepcopy(value.get("issuance_policy")),
        "training_evidence": deepcopy(value["training_evidence"]),
        "fixture_count": fixture_count,
        "prediction_count": len(predictions),
        "available_information_states": available_states,
        "predictions": predictions,
    }


def snapshot_age_seconds(snapshot: dict[str, Any]) -> float:
    as_of = _parse_timestamp(snapshot["as_of"], "as_of")
    return max(0.0, (datetime.now(timezone.utc) - as_of).total_seconds())


def _validate_training_evidence(value: object) -> None:
    if not isinstance(value, dict):
        raise SnapshotValidationError("training_evidence must be an object")
    required = (
        "horizon_training_fixtures",
        "minimum_training_fixtures",
        "team_cold_start_below_matches",
        "full_signal_history_matches",
    )
    for key in required:
        if key not in value:
            raise SnapshotValidationError(f"training_evidence is missing {key}")
    horizons = value["horizon_training_fixtures"]
    if not isinstance(horizons, dict) or set(horizons) != SUPPORTED_INFORMATION_STATES:
        raise SnapshotValidationError(
            "training_evidence horizon counts must cover supported information states"
        )
    for state, count in horizons.items():
        _positive_integer(count, f"training_evidence horizon {state}")
    for key in required[1:]:
        _positive_integer(value[key], f"training_evidence {key}")


def _validate_prediction(
    value: object,
    index: int,
    *,
    strict_v3: bool,
    availability_policy: object,
    issuance_policy: object,
    snapshot_created_at: datetime,
) -> None:
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"Prediction {index} must be an object")
    required = (
        "fixture_id",
        "fixture",
        "kickoff",
        "prediction_at",
        "information_state",
        "home_win_probability",
        "draw_probability",
        "away_win_probability",
        "raw_home_win_probability",
        "raw_draw_probability",
        "raw_away_win_probability",
        "expected_home_goals",
        "expected_away_goals",
        "home_history_matches",
        "away_history_matches",
        "home_xg_history",
        "away_xg_history",
        "home_shots_history",
        "away_shots_history",
        "warnings",
    )
    for key in required:
        if key not in value:
            raise SnapshotValidationError(f"Prediction {index} is missing {key}")
    if strict_v3:
        for key in (
            "source_max_retrieved_at",
            "issued_at",
            "issuance_status",
            "issuance_policy_version",
            "availability_policy_version",
            "immutable_prediction_sha256",
        ):
            if key not in value:
                raise SnapshotValidationError(f"Prediction {index} is missing {key}")
    if not isinstance(value["fixture_id"], str) or not value["fixture_id"]:
        raise SnapshotValidationError(f"Prediction {index} has invalid fixture_id")
    fixture = value["fixture"]
    if not isinstance(fixture, dict):
        raise SnapshotValidationError(f"Prediction {index} fixture must be an object")
    for key in ("home_team_name", "away_team_name", "competition_name"):
        if not isinstance(fixture.get(key), str) or not fixture[key]:
            raise SnapshotValidationError(
                f"Prediction {index} fixture is missing {key}"
            )
    if fixture.get("fixture_id") != value["fixture_id"]:
        raise SnapshotValidationError(
            f"Prediction {index} nested fixture_id does not match"
        )
    state = value["information_state"]
    if state not in SUPPORTED_INFORMATION_STATES:
        raise SnapshotValidationError(
            f"Prediction {index} has unsupported information_state {state!r}"
        )
    kickoff = _parse_timestamp(value["kickoff"], "kickoff")
    prediction_at = _parse_timestamp(value["prediction_at"], "prediction_at")
    if prediction_at >= kickoff:
        raise SnapshotValidationError(
            f"Prediction {index} cutoff must precede kickoff"
        )
    if strict_v3:
        issued_at = _parse_timestamp(value["issued_at"], "issued_at")
        if issued_at < prediction_at or issued_at >= kickoff:
            raise SnapshotValidationError(
                f"Prediction {index} has an invalid issuance time"
            )
        if issued_at > snapshot_created_at:
            raise SnapshotValidationError(
                f"Prediction {index} was issued after snapshot creation"
            )
        source_max = value["source_max_retrieved_at"]
        if source_max is not None and _parse_timestamp(
            source_max, "source_max_retrieved_at"
        ) > prediction_at:
            raise SnapshotValidationError(
                f"Prediction {index} uses data retrieved after its cutoff"
            )
        if not isinstance(availability_policy, dict) or value[
            "availability_policy_version"
        ] != availability_policy.get("policy_version"):
            raise SnapshotValidationError(
                f"Prediction {index} availability policy mismatch"
            )
        if not isinstance(issuance_policy, dict) or value[
            "issuance_policy_version"
        ] != issuance_policy.get("policy_version"):
            raise SnapshotValidationError(
                f"Prediction {index} issuance policy mismatch"
            )
        unhashed = dict(value)
        immutable_hash = unhashed.pop("immutable_prediction_sha256")
        try:
            expected_immutable_hash = champion_prediction_rows_sha256([unhashed])
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"Prediction {index} immutable hash could not be verified"
            ) from exc
        if immutable_hash != expected_immutable_hash:
            raise SnapshotValidationError(
                f"Prediction {index} immutable hash mismatch"
            )
    probabilities = [
        _probability(value[key], f"Prediction {index} {key}")
        for key in (
            "home_win_probability",
            "draw_probability",
            "away_win_probability",
        )
    ]
    if not math.isclose(math.fsum(probabilities), 1.0, abs_tol=1e-9):
        raise SnapshotValidationError(
            f"Prediction {index} calibrated probabilities must sum to 1"
        )
    raw_probabilities = [
        _probability(value[key], f"Prediction {index} {key}")
        for key in (
            "raw_home_win_probability",
            "raw_draw_probability",
            "raw_away_win_probability",
        )
    ]
    if not math.isclose(math.fsum(raw_probabilities), 1.0, abs_tol=1e-9):
        raise SnapshotValidationError(
            f"Prediction {index} raw probabilities must sum to 1"
        )
    for key in ("expected_home_goals", "expected_away_goals"):
        _nonnegative_number(value[key], f"Prediction {index} {key}")
    for key in (
        "home_history_matches",
        "away_history_matches",
        "home_xg_history",
        "away_xg_history",
        "home_shots_history",
        "away_shots_history",
    ):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0:
            raise SnapshotValidationError(
                f"Prediction {index} {key} must be a nonnegative integer"
            )
    warnings = value["warnings"]
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) and warning for warning in warnings
    ):
        raise SnapshotValidationError(
            f"Prediction {index} warnings must be a list of nonempty strings"
        )


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise SnapshotValidationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise SnapshotValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _probability(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise SnapshotValidationError(f"{field} must be a probability")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{field} must be a probability") from exc
    if not math.isfinite(parsed) or not 0.0 < parsed < 1.0:
        raise SnapshotValidationError(f"{field} must be strictly between 0 and 1")
    return parsed


def _nonnegative_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise SnapshotValidationError(f"{field} must be a nonnegative number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{field} must be a nonnegative number") from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise SnapshotValidationError(f"{field} must be a nonnegative number")
    return parsed


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SnapshotValidationError(f"{field} must be a positive integer")
    return value
