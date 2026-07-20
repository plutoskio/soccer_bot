from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math


PLATFORM_SNAPSHOT_VERSION = "specialized_bet_platform_snapshot_v1"
PLATFORM_STATUSES = {"validated", "experimental", "unavailable", "unsupported"}


class PlatformSnapshotValidationError(ValueError):
    """Raised when a specialized platform snapshot is unsafe to publish."""


def validate_platform_snapshot(value: object) -> None:
    if not isinstance(value, dict):
        raise PlatformSnapshotValidationError("Platform snapshot must be an object")
    if value.get("snapshot_version") != PLATFORM_SNAPSHOT_VERSION:
        raise PlatformSnapshotValidationError("Unsupported platform snapshot version")
    for key in (
        "created_at",
        "as_of",
        "family_registry_version",
        "ranking_policy",
        "states",
        "models",
        "state_rows_sha256",
    ):
        if key not in value:
            raise PlatformSnapshotValidationError(f"Platform snapshot is missing {key}")
    created_at = _timestamp(value["created_at"], "created_at")
    as_of = _timestamp(value["as_of"], "as_of")
    if created_at < as_of:
        raise PlatformSnapshotValidationError("Platform created_at precedes as_of")
    if value["ranking_policy"] != "validated_families_only":
        raise PlatformSnapshotValidationError("Unsafe platform ranking policy")
    states = value["states"]
    if not isinstance(states, list):
        raise PlatformSnapshotValidationError("Platform states must be a list")
    state_keys = set()
    for index, state in enumerate(states):
        _validate_state(state, index, created_at)
        key = (state["fixture_id"], state["information_state"])
        if key in state_keys:
            raise PlatformSnapshotValidationError(f"Duplicate platform state: {key}")
        state_keys.add(key)
    if value["state_rows_sha256"] != _logical_hash(states):
        raise PlatformSnapshotValidationError("Platform state hash mismatch")


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
        _validate_family(family)
        if family["family_key"] in family_keys:
            raise PlatformSnapshotValidationError(f"State {index} repeats a family")
        family_keys.add(family["family_key"])


def _validate_family(family: object) -> None:
    if not isinstance(family, dict):
        raise PlatformSnapshotValidationError("Family must be an object")
    status = family.get("status")
    if status not in PLATFORM_STATUSES:
        raise PlatformSnapshotValidationError("Unknown platform family status")
    if family.get("eligible_for_ranking") is not (status == "validated"):
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
        _validate_market_quote(market.get("market_comparison"), "cutoff")
        _validate_market_quote(market.get("live_market"), "live")


def _validate_market_quote(value: object, expected_type: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or value.get("source") != "polymarket":
        raise PlatformSnapshotValidationError("Market quote source is invalid")
    if value.get("quote_type") != expected_type:
        raise PlatformSnapshotValidationError("Market quote type is invalid")
    for field in (
        "market_probability",
        "best_bid_probability",
        "best_ask_probability",
        "bid_ask_spread",
    ):
        number = value.get(field)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not 0 <= number <= 1
        ):
            raise PlatformSnapshotValidationError(f"Market quote {field} is invalid")
    bid = float(value["best_bid_probability"])
    ask = float(value["best_ask_probability"])
    if bid > ask or not math.isclose(
        float(value["bid_ask_spread"]), ask - bid, abs_tol=1e-9
    ):
        raise PlatformSnapshotValidationError("Market quote spread is incoherent")
    multiplier = value.get("market_decimal_multiplier")
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(multiplier)
        or multiplier < 1
    ):
        raise PlatformSnapshotValidationError("Market quote multiplier is invalid")
    _timestamp(value.get("observed_at"), "market observed_at")
    _timestamp(value.get("retrieved_at"), "market retrieved_at")


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
