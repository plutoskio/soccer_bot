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
    if "match_context" in value:
        _validate_match_context(value["match_context"], prediction_at, index)
    if "model_expectation" in value:
        _validate_model_expectation(value["model_expectation"], index)
    families = value["families"]
    if not isinstance(families, list) or not families:
        raise PlatformSnapshotValidationError(f"State {index} has no families")
    family_keys = set()
    for family in families:
        _validate_family(family)
        if family["family_key"] in family_keys:
            raise PlatformSnapshotValidationError(f"State {index} repeats a family")
        family_keys.add(family["family_key"])


def _validate_match_context(value: object, prediction_at: datetime, index: int) -> None:
    if not isinstance(value, dict):
        raise PlatformSnapshotValidationError(f"State {index} match context must be an object")
    cutoff_at = _timestamp(value.get("cutoff_at"), "match context cutoff_at")
    if cutoff_at != prediction_at:
        raise PlatformSnapshotValidationError(
            f"State {index} match context cutoff differs from prediction"
        )
    for side in ("home", "away"):
        team = value.get(side)
        if not isinstance(team, dict):
            raise PlatformSnapshotValidationError(
                f"State {index} {side} match context must be an object"
            )
        team_id = team.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise PlatformSnapshotValidationError("Match context team id is invalid")
        rest_days = team.get("rest_days")
        if rest_days is not None:
            _finite_number(rest_days, "match context rest_days", minimum=0)
        for field in ("matches_last_7d", "matches_last_14d", "matches_last_30d"):
            _nonnegative_integer(team.get(field), f"match context {field}")
        if not (
            team["matches_last_7d"]
            <= team["matches_last_14d"]
            <= team["matches_last_30d"]
        ):
            raise PlatformSnapshotValidationError("Match context workload counts are incoherent")
        matches = team.get("recent_matches")
        if not isinstance(matches, list) or len(matches) > 5:
            raise PlatformSnapshotValidationError("Match context recent matches are invalid")
        seen = set()
        last_kickoff = None
        for match in matches:
            if not isinstance(match, dict):
                raise PlatformSnapshotValidationError("Recent match must be an object")
            fixture_id = match.get("fixture_id")
            if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen:
                raise PlatformSnapshotValidationError("Recent match fixture id is invalid")
            seen.add(fixture_id)
            kickoff = _timestamp(match.get("kickoff"), "recent match kickoff")
            available_at = _timestamp(match.get("available_at"), "recent match available_at")
            if kickoff >= available_at or available_at >= prediction_at:
                raise PlatformSnapshotValidationError("Recent match was unavailable at cutoff")
            if last_kickoff is not None and kickoff > last_kickoff:
                raise PlatformSnapshotValidationError("Recent matches are not reverse chronological")
            last_kickoff = kickoff
            for field in ("competition_name", "opponent_name"):
                if not isinstance(match.get(field), str) or not match[field]:
                    raise PlatformSnapshotValidationError(f"Recent match {field} is invalid")
            if match.get("venue") not in {"home", "away"}:
                raise PlatformSnapshotValidationError("Recent match venue is invalid")
            if not isinstance(match.get("neutral_venue"), bool):
                raise PlatformSnapshotValidationError("Recent match neutral venue is invalid")
            team_score = _nonnegative_integer(match.get("team_score"), "recent team score")
            opponent_score = _nonnegative_integer(
                match.get("opponent_score"), "recent opponent score"
            )
            expected = "win" if team_score > opponent_score else "draw" if team_score == opponent_score else "loss"
            if match.get("outcome") != expected:
                raise PlatformSnapshotValidationError("Recent match outcome and score disagree")
        trends = team.get("trends")
        if not isinstance(trends, dict):
            raise PlatformSnapshotValidationError("Match context trends must be an object")
        for window, maximum in (("last_5", 5), ("last_10", 10)):
            _validate_trend(trends.get(window), maximum)


def _validate_trend(value: object, maximum: int) -> None:
    if not isinstance(value, dict):
        raise PlatformSnapshotValidationError("Match trend must be an object")
    sample = _nonnegative_integer(value.get("sample_size"), "trend sample size")
    if sample > maximum:
        raise PlatformSnapshotValidationError("Match trend sample exceeds its window")
    results = [
        _nonnegative_integer(value.get(field), f"trend {field}")
        for field in ("wins", "draws", "losses")
    ]
    if sum(results) != sample:
        raise PlatformSnapshotValidationError("Match trend record differs from sample size")
    for field in (
        "goals_for_per_match",
        "goals_against_per_match",
        "clean_sheet_rate",
        "both_teams_scored_rate",
    ):
        number = value.get(field)
        if sample == 0:
            if number is not None:
                raise PlatformSnapshotValidationError("Empty match trend must use null metrics")
            continue
        upper = 1 if field.endswith("rate") else None
        _finite_number(number, f"trend {field}", minimum=0, maximum=upper)


def _validate_model_expectation(value: object, index: int) -> None:
    if not isinstance(value, dict):
        raise PlatformSnapshotValidationError(
            f"State {index} model expectation must be an object"
        )
    for field in ("expected_home_goals", "expected_away_goals"):
        _finite_number(value.get(field), f"model expectation {field}", minimum=0)


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
        _validate_market_quote(market.get("market_comparison"), "cutoff_consensus")
        if market.get("live_market") is not None:
            raise PlatformSnapshotValidationError(
                "Live external markets are disabled for platform publication"
            )


def _validate_market_quote(value: object, expected_type: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or value.get("source") != "api_football":
        raise PlatformSnapshotValidationError("Market quote source is invalid")
    if value.get("quote_type") != expected_type:
        raise PlatformSnapshotValidationError("Market quote type is invalid")
    for field in (
        "market_probability",
    ):
        number = value.get(field)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not 0 <= number <= 1
        ):
            raise PlatformSnapshotValidationError(f"Market quote {field} is invalid")
    multiplier = value.get("market_decimal_multiplier")
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(multiplier)
        or multiplier < 1
    ):
        raise PlatformSnapshotValidationError("Market quote multiplier is invalid")
    probability = float(value["market_probability"])
    if probability <= 0 or not math.isclose(
        multiplier, 1.0 / probability, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise PlatformSnapshotValidationError(
            "Bookmaker consensus probability and multiplier are incoherent"
        )
    bookmaker_count = value.get("bookmaker_count")
    if (
        isinstance(bookmaker_count, bool)
        or not isinstance(bookmaker_count, int)
        or bookmaker_count <= 0
    ):
        raise PlatformSnapshotValidationError("Bookmaker count is invalid")
    if value.get("consensus_method") != "median_proportional_devig":
        raise PlatformSnapshotValidationError("Bookmaker consensus method is invalid")
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


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformSnapshotValidationError(f"{field} must be a nonnegative integer")
    return value


def _finite_number(
    value: object,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        raise PlatformSnapshotValidationError(f"{field} is invalid")
    return float(value)


def _logical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
