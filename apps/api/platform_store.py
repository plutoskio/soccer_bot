from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from typing import Any

from soccer_bot.platform_snapshot import (
    PlatformSnapshotValidationError as CorePlatformSnapshotValidationError,
    validate_platform_snapshot as validate_platform_contract,
)


DEFAULT_PLATFORM_SNAPSHOT_PATH = Path(
    "data/predictions/specialized_platform_v1/latest.json"
)
PLATFORM_SNAPSHOT_VERSION = "specialized_bet_platform_snapshot_v1"
PLATFORM_STATUSES = {"validated", "experimental", "unavailable", "unsupported"}


class PlatformSnapshotUnavailableError(RuntimeError):
    """Raised when the specialized platform snapshot cannot be loaded."""


class PlatformSnapshotValidationError(ValueError):
    """Raised when the specialized platform snapshot is unsafe to serve."""


class PlatformSnapshotStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cached_mtime_ns: int | None = None
        self._cached: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as error:
            raise PlatformSnapshotUnavailableError(
                f"Platform snapshot is unavailable at {self.path}"
            ) from error
        with self._lock:
            if self._cached is not None and self._cached_mtime_ns == mtime:
                return deepcopy(self._cached)
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise PlatformSnapshotUnavailableError(
                    "Platform snapshot could not be read as JSON"
                ) from error
            validate_platform_snapshot(value)
            self._cached = public_platform_snapshot(value)
            self._cached_mtime_ns = mtime
            return deepcopy(self._cached)


class S3PlatformSnapshotStore:
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
        self._cached: dict[str, Any] | None = None
        self._cached_etag: str | None = None
        self._refresh_after = 0.0

    def load(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now < self._refresh_after:
                return deepcopy(self._cached)
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.key)
                body = response["Body"].read()
                etag = str(response.get("ETag", ""))
            except Exception as error:
                if self._cached is not None:
                    self._refresh_after = now + min(5.0, self.cache_seconds)
                    return deepcopy(self._cached)
                raise PlatformSnapshotUnavailableError(
                    "Platform snapshot could not be read from object storage"
                ) from error
            if self._cached is not None and etag and etag == self._cached_etag:
                self._refresh_after = now + self.cache_seconds
                return deepcopy(self._cached)
            try:
                value = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PlatformSnapshotUnavailableError(
                    "Object-storage platform snapshot is invalid JSON"
                ) from error
            validate_platform_snapshot(value)
            self._cached = public_platform_snapshot(value)
            self._cached_etag = etag
            self._refresh_after = now + self.cache_seconds
            return deepcopy(self._cached)


def validate_platform_snapshot(value: object) -> None:
    try:
        validate_platform_contract(value)
    except CorePlatformSnapshotValidationError as error:
        raise PlatformSnapshotValidationError(str(error)) from error


def public_platform_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    states = sorted(
        deepcopy(value["states"]),
        key=lambda row: (row["kickoff"], row["fixture_id"], row["information_state"]),
    )
    as_of = _timestamp(value["as_of"], "as_of")
    age = max(0.0, (datetime.now(timezone.utc) - as_of).total_seconds())
    return {
        "snapshot_version": value["snapshot_version"],
        "created_at": value["created_at"],
        "as_of": value["as_of"],
        "snapshot_age_seconds": round(age),
        "is_stale": age > 21600,
        "family_registry_version": value["family_registry_version"],
        "market_comparison_status": value.get("market_comparison_status"),
        "ranking_policy": value["ranking_policy"],
        "models": deepcopy(value["models"]),
        "target_audit": deepcopy(value.get("target_audit", {})),
        "fixture_count": len({row["fixture_id"] for row in states}),
        "state_count": len(states),
        "available_information_states": sorted(
            {row["information_state"] for row in states}
        ),
        "state_rows_sha256": value["state_rows_sha256"],
        "states": states,
    }


def _validate_state(value: object, index: int, created_at: datetime) -> None:
    if not isinstance(value, dict):
        raise PlatformSnapshotValidationError(f"State {index} must be an object")
    for key in (
        "fixture_id",
        "fixture",
        "kickoff",
        "prediction_at",
        "issued_at",
        "information_state",
        "families",
    ):
        if key not in value:
            raise PlatformSnapshotValidationError(f"State {index} is missing {key}")
    kickoff = _timestamp(value["kickoff"], "kickoff")
    prediction_at = _timestamp(value["prediction_at"], "prediction_at")
    issued_at = _timestamp(value["issued_at"], "issued_at")
    if prediction_at >= kickoff or issued_at >= kickoff:
        raise PlatformSnapshotValidationError(f"State {index} is not pre-match")
    if issued_at > created_at:
        raise PlatformSnapshotValidationError(f"State {index} issued after snapshot")
    fixture = value["fixture"]
    if not isinstance(fixture, dict) or fixture.get("fixture_id") != value["fixture_id"]:
        raise PlatformSnapshotValidationError(f"State {index} fixture mismatch")
    families = value["families"]
    if not isinstance(families, list) or not families:
        raise PlatformSnapshotValidationError(f"State {index} has no families")
    family_keys = set()
    for family in families:
        _validate_family(family, index)
        if family["family_key"] in family_keys:
            raise PlatformSnapshotValidationError(f"State {index} repeats a family")
        family_keys.add(family["family_key"])


def _validate_family(family: object, state_index: int) -> None:
    if not isinstance(family, dict):
        raise PlatformSnapshotValidationError("Family must be an object")
    status = family.get("status")
    if status not in PLATFORM_STATUSES:
        raise PlatformSnapshotValidationError("Unknown platform family status")
    ranking = family.get("eligible_for_ranking")
    if ranking is not (status == "validated"):
        raise PlatformSnapshotValidationError("Only validated families may rank")
    markets = family.get("markets")
    if not isinstance(markets, list):
        raise PlatformSnapshotValidationError("Family markets must be a list")
    if status in {"unavailable", "unsupported"} and markets:
        raise PlatformSnapshotValidationError("Unavailable family cannot publish markets")
    market_ids = set()
    for market in markets:
        if not isinstance(market, dict):
            raise PlatformSnapshotValidationError("Market must be an object")
        market_id = market.get("market_id")
        if not isinstance(market_id, str) or not market_id:
            raise PlatformSnapshotValidationError("Market id is invalid")
        if market_id in market_ids:
            raise PlatformSnapshotValidationError("Duplicate market id")
        market_ids.add(market_id)
        probability = market.get("probability")
        if probability is not None and (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise PlatformSnapshotValidationError("Market probability is invalid")
        fair = market.get("fair_decimal_multiplier")
        if fair is not None and (
            isinstance(fair, bool)
            or not isinstance(fair, (int, float))
            or not math.isfinite(fair)
            or fair < 1
        ):
            raise PlatformSnapshotValidationError("Fair multiplier is invalid")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise PlatformSnapshotValidationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlatformSnapshotValidationError(f"{field} is invalid") from error
    if parsed.tzinfo is None:
        raise PlatformSnapshotValidationError(f"{field} must include a timezone")
    return parsed


def _logical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
