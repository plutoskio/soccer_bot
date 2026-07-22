from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

from soccer_bot.prospective_evidence import load_forecast_evidence
from soccer_bot.prospective_settlement import load_prospective_settlement_ledger


HISTORY_VERSION = "published_prediction_history_v1"
BOOKMAKER_MINIMUM_SETTLED_FIXTURE_HORIZONS = 500
BOOKMAKER_MINIMUM_CALENDAR_MONTHS = 3


class PredictionHistoryError(RuntimeError):
    """Raised when published history cannot be materialized or verified safely."""


def build_prediction_history(
    *,
    evidence_directory: Path,
    ledger_path: Path,
    settlement_config_path: Path,
    generated_at: datetime,
    platform_snapshot_directory: Path | None = None,
) -> dict[str, object]:
    evidence = {
        str(item["evidence_key"]): item
        for item in load_forecast_evidence(evidence_directory)
    }
    records, ledger_head = load_prospective_settlement_ledger(
        ledger_path=ledger_path,
        settlement_config_path=settlement_config_path,
    )
    bookmaker_comparisons = _load_bookmaker_comparisons(
        platform_snapshot_directory
    )
    fixtures: dict[str, dict[str, object]] = {}
    excluded_ineligible = 0
    for record in records:
        if record.get("eligible_for_prospective_gate") is not True:
            excluded_ineligible += 1
            continue
        item = evidence.get(str(record.get("evidence_key")))
        if item is None:
            raise PredictionHistoryError("settled record has no immutable forecast evidence")
        prediction = item.get("prediction")
        if not isinstance(prediction, Mapping):
            raise PredictionHistoryError("forecast evidence prediction is invalid")
        fixture_id = str(record["fixture_id"])
        fixture = prediction.get("fixture")
        if not isinstance(fixture, Mapping):
            raise PredictionHistoryError("forecast evidence fixture is invalid")
        outcome = record.get("realized_regulation_score")
        if not isinstance(outcome, Mapping):
            raise PredictionHistoryError("settled record outcome is invalid")
        value = fixtures.setdefault(
            fixture_id,
            {
                "fixture_id": fixture_id,
                "kickoff": record["kickoff"],
                "competition_id": record["competition_id"],
                "competition_name": fixture.get("competition_name"),
                "home_team_name": fixture.get("home_team_name"),
                "away_team_name": fixture.get("away_team_name"),
                "result": {
                    "status": "settled",
                    "home_goals": outcome["home_goals"],
                    "away_goals": outcome["away_goals"],
                    "outcome": outcome["result"],
                    "settled_at": record["settled_at"],
                },
                "prediction_groups": [],
            },
        )
        if value["kickoff"] != record["kickoff"] or value["result"]["home_goals"] != outcome["home_goals"] or value["result"]["away_goals"] != outcome["away_goals"]:
            raise PredictionHistoryError("fixture history rows disagree")
        group = {
            "prediction_key": record["evidence_key"],
            "evidence_classification": "published_forward",
            "evidence_label": "PUBLISHED BEFORE KICKOFF",
            "family_key": "regulation_score",
            "display_name": "Score and goals",
            "model_version": record["model_version"],
            "logical_model_sha256": record["logical_model_sha256"],
            "model_status_at_prediction": "experimental",
            "information_state": record["information_state"],
            "prediction_at": record["prediction_at"],
            "first_published_at": record["first_snapshot_created_at"],
            "eligible_for_performance_claim": False,
            "expected_home_goals": prediction["expected_home_goals"],
            "expected_away_goals": prediction["expected_away_goals"],
            "warnings": prediction.get("warnings", []),
            "markets": _prediction_markets(
                prediction,
                record,
                bookmaker_comparisons.get(
                    (
                        fixture_id,
                        str(record["information_state"]),
                        str(record["prediction_at"]),
                    ),
                    {},
                ),
            ),
        }
        value["prediction_groups"].append(group)

    rows = sorted(fixtures.values(), key=lambda row: (row["kickoff"], row["fixture_id"]), reverse=True)
    for fixture in rows:
        fixture["prediction_groups"].sort(
            key=lambda group: (
                group["information_state"] != "pre_lineup_24h_v1",
                group["prediction_at"],
            )
        )
    history_hash = _logical_sha256(rows)
    result: dict[str, object] = {
        "history_version": HISTORY_VERSION,
        "generated_at": generated_at.isoformat(),
        "as_of": max((row["result"]["settled_at"] for row in rows), default=generated_at.isoformat()),
        "fixture_count": len(rows),
        "prediction_group_count": sum(len(row["prediction_groups"]) for row in rows),
        "excluded_ineligible_records": excluded_ineligible,
        "ledger_head_sha256": ledger_head,
        "history_rows_sha256": history_hash,
        "bookmaker_readiness": _bookmaker_readiness(rows),
        "fixtures": rows,
    }
    validate_prediction_history(result)
    return result


def validate_prediction_history(value: object) -> None:
    if not isinstance(value, dict) or value.get("history_version") != HISTORY_VERSION:
        raise PredictionHistoryError("unsupported prediction history version")
    for key in ("generated_at", "as_of"):
        _timestamp(value.get(key), key)
    fixtures = value.get("fixtures")
    if not isinstance(fixtures, list):
        raise PredictionHistoryError("prediction history fixtures must be a list")
    if value.get("fixture_count") != len(fixtures):
        raise PredictionHistoryError("prediction history fixture count differs")
    seen = set()
    groups = 0
    previous = None
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise PredictionHistoryError("prediction history fixture is invalid")
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen:
            raise PredictionHistoryError("prediction history fixture identity is invalid")
        seen.add(fixture_id)
        kickoff = _timestamp(fixture.get("kickoff"), "kickoff")
        if previous is not None and kickoff > previous:
            raise PredictionHistoryError("prediction history is not newest first")
        previous = kickoff
        for key in ("competition_name", "home_team_name", "away_team_name"):
            if not isinstance(fixture.get(key), str) or not fixture[key]:
                raise PredictionHistoryError(f"prediction history fixture missing {key}")
        result = fixture.get("result")
        if not isinstance(result, dict) or result.get("status") != "settled":
            raise PredictionHistoryError("prediction history result is not settled")
        _nonnegative_int(result.get("home_goals"), "home goals")
        _nonnegative_int(result.get("away_goals"), "away goals")
        if result.get("outcome") not in {"home_win", "draw", "away_win"}:
            raise PredictionHistoryError("prediction history outcome is invalid")
        prediction_groups = fixture.get("prediction_groups")
        if not isinstance(prediction_groups, list) or not prediction_groups:
            raise PredictionHistoryError("prediction history fixture has no predictions")
        for group in prediction_groups:
            _validate_group(group, kickoff)
            groups += 1
    if value.get("prediction_group_count") != groups:
        raise PredictionHistoryError("prediction history group count differs")
    if value.get("history_rows_sha256") != _logical_sha256(fixtures):
        raise PredictionHistoryError("prediction history row hash differs")
    readiness = value.get("bookmaker_readiness")
    if not isinstance(readiness, dict) or readiness.get("status") not in {
        "collecting",
        "ready",
    }:
        raise PredictionHistoryError("bookmaker readiness is invalid")
    settled = _nonnegative_int(
        readiness.get("settled_fixture_horizons"),
        "bookmaker settled fixture horizons",
    )
    months = _nonnegative_int(
        readiness.get("calendar_months"),
        "bookmaker calendar months",
    )
    expected_ready = (
        settled >= BOOKMAKER_MINIMUM_SETTLED_FIXTURE_HORIZONS
        and months >= BOOKMAKER_MINIMUM_CALENDAR_MONTHS
    )
    if (readiness["status"] == "ready") is not expected_ready:
        raise PredictionHistoryError("bookmaker readiness gate is incoherent")
    if (readiness.get("comparison") is not None) is not expected_ready:
        raise PredictionHistoryError("bookmaker comparison exposure is incoherent")


def _validate_group(value: object, kickoff: datetime) -> None:
    if not isinstance(value, dict):
        raise PredictionHistoryError("prediction group is invalid")
    if value.get("evidence_classification") != "published_forward":
        raise PredictionHistoryError("prediction group is not published evidence")
    prediction_at = _timestamp(value.get("prediction_at"), "prediction_at")
    published_at = _timestamp(value.get("first_published_at"), "first_published_at")
    if prediction_at >= kickoff or published_at >= kickoff:
        raise PredictionHistoryError("prediction group is not pre-kickoff")
    if value.get("eligible_for_performance_claim") is not False:
        raise PredictionHistoryError("ungated history cannot make performance claims")
    markets = value.get("markets")
    if not isinstance(markets, list) or not markets:
        raise PredictionHistoryError("prediction group has no markets")
    market_ids = set()
    for market in markets:
        if not isinstance(market, dict):
            raise PredictionHistoryError("history market is invalid")
        market_id = market.get("market_id")
        if not isinstance(market_id, str) or not market_id or market_id in market_ids:
            raise PredictionHistoryError("history market identity is invalid")
        market_ids.add(market_id)
        probability = market.get("probability")
        if probability is not None and not _probability(probability):
            raise PredictionHistoryError("history market probability is invalid")
        comparison = market.get("market_comparison")
        if comparison is not None:
            _validate_bookmaker_comparison(comparison, prediction_at)
        if market.get("realized_settlement") not in {
            "win", "half_win", "push", "half_loss", "loss"
        }:
            raise PredictionHistoryError("history market settlement is invalid")


def _validate_bookmaker_comparison(value: object, prediction_at: datetime) -> None:
    if not isinstance(value, dict):
        raise PredictionHistoryError("history bookmaker comparison is invalid")
    if value.get("source") != "api_football" or value.get("quote_type") != "cutoff_consensus":
        raise PredictionHistoryError("history bookmaker comparison source is invalid")
    probability = value.get("market_probability")
    if not _probability(probability) or float(probability) <= 0:
        raise PredictionHistoryError("history bookmaker probability is invalid")
    multiplier = value.get("market_decimal_multiplier")
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(multiplier)
        or not math.isclose(
            float(multiplier),
            1.0 / float(probability),
            rel_tol=1e-9,
            abs_tol=1e-9,
        )
    ):
        raise PredictionHistoryError("history bookmaker multiplier is invalid")
    count = value.get("bookmaker_count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise PredictionHistoryError("history bookmaker count is invalid")
    if value.get("consensus_method") != "median_proportional_devig":
        raise PredictionHistoryError("history bookmaker consensus method is invalid")
    if _timestamp(value.get("observed_at"), "bookmaker observed_at") > prediction_at:
        raise PredictionHistoryError("history bookmaker quote is post-cutoff")
    if _timestamp(value.get("retrieved_at"), "bookmaker retrieved_at") >= prediction_at:
        raise PredictionHistoryError("history bookmaker retrieval is post-cutoff")


def _prediction_markets(
    prediction: Mapping[str, object],
    record: Mapping[str, object],
    bookmaker_comparisons: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    outcome = record["realized_regulation_score"]
    home_goals = int(outcome["home_goals"])
    away_goals = int(outcome["away_goals"])
    actual_result = str(outcome["result"])
    markets: list[dict[str, object]] = []

    names = {
        "home_win": prediction["fixture"]["home_team_name"],
        "draw": "Draw",
        "away_win": prediction["fixture"]["away_team_name"],
    }
    for key, probability in prediction["parent_moneyline"].items():
        comparison = bookmaker_comparisons.get(key)
        if comparison is not None and not math.isclose(
            float(comparison["model_probability"]),
            float(probability),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise PredictionHistoryError(
                "bookmaker comparison does not match immutable model probability"
            )
        markets.append(
            _scalar_market(
                "Match result",
                f"moneyline:{key}",
                names[key],
                float(probability),
                key == actual_result,
                market_comparison=(
                    None if comparison is None else dict(comparison["quote"])
                ),
            )
        )

    actual_score = (home_goals, away_goals)
    exact = {
        (int(item["home_goals"]), int(item["away_goals"])): float(item["probability"])
        for item in prediction["top_exact_scores"][:10]
    }
    if actual_score not in exact:
        for cell in prediction["score_grid"]:
            pair = (int(cell["home_goals"]), int(cell["away_goals"]))
            if pair == actual_score:
                exact[pair] = float(cell["probability"])
                break
    for (home, away), probability in sorted(exact.items(), key=lambda item: item[1], reverse=True):
        markets.append(_scalar_market("Exact score", f"exact:{home}:{away}", f"{home}–{away}", probability, (home, away) == actual_score))

    markets.extend(_distribution_markets("Total goals", "total", prediction["total_goal_distribution"], home_goals + away_goals))
    markets.extend(_distribution_markets("Home goals", "home_goals", prediction["home_goal_distribution"], home_goals))
    markets.extend(_distribution_markets("Away goals", "away_goals", prediction["away_goal_distribution"], away_goals))
    markets.extend(_distribution_markets("Goal difference", "goal_difference", prediction["goal_difference_distribution"], home_goals - away_goals))
    actual_btts = "yes" if home_goals > 0 and away_goals > 0 else "no"
    for key, probability in prediction["both_teams_to_score"].items():
        markets.append(_scalar_market("Both teams to score", f"btts:{key}", key.title(), float(probability), key == actual_btts))

    reference = record["reference_contract_settlements"]["candidate"]
    for line, sides in reference["total_goals"].items():
        for side, item in sides.items():
            markets.append(_settlement_market("Goal totals", f"total_line:{line}:{side}", f"{side.title()} {line}", item))
    for line, teams in reference["goal_handicap"].items():
        for team, item in teams.items():
            label = prediction["fixture"]["home_team_name"] if team == "home" else prediction["fixture"]["away_team_name"]
            markets.append(_settlement_market("Goal handicap", f"handicap:{line}:{team}", f"{label} {line}", item))
    return markets


def _distribution_markets(
    group: str,
    prefix: str,
    distribution: object,
    actual: int,
) -> list[dict[str, object]]:
    if not isinstance(distribution, list):
        raise PredictionHistoryError("prediction distribution is invalid")
    values = [
        item
        for item in distribution
        if (
            (
                abs(int(item["value"])) <= 8
                if prefix == "goal_difference"
                else 0 <= int(item["value"]) <= 8
            )
            or int(item["value"]) == actual
        )
    ]
    return [
        _scalar_market(group, f"{prefix}:{int(item['value'])}", str(int(item["value"])), float(item["probability"]), int(item["value"]) == actual)
        for item in values
    ]


def _scalar_market(
    group: str,
    market_id: str,
    label: str,
    probability: float,
    realized: bool,
    *,
    market_comparison: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "market_id": market_id,
        "group": group,
        "label": label,
        "probability": probability,
        "fair_decimal_multiplier": 1.0 / probability,
        "settlement_probabilities": None,
        "realized_settlement": "win" if realized else "loss",
        "market_comparison": (
            None if market_comparison is None else dict(market_comparison)
        ),
    }


def _settlement_market(group: str, market_id: str, label: str, item: Mapping[str, object]) -> dict[str, object]:
    forecast = {key: float(value) for key, value in item["forecast"].items()}
    return {
        "market_id": market_id,
        "group": group,
        "label": label,
        "probability": None,
        "fair_decimal_multiplier": None,
        "settlement_probabilities": forecast,
        "realized_settlement": item["realized_outcome"],
        "market_comparison": None,
    }


def _bookmaker_readiness(fixtures: list[dict[str, object]]) -> dict[str, object]:
    paired = []
    quote_count = 0
    for fixture in fixtures:
        for group in fixture["prediction_groups"]:
            markets = [
                market
                for market in group["markets"]
                if market["group"] == "Match result"
            ]
            quoted = [
                market for market in markets if market.get("market_comparison") is not None
            ]
            quote_count += len(quoted)
            if len(markets) != 3 or len(quoted) != 3:
                continue
            realized = next(
                (market for market in markets if market["realized_settlement"] == "win"),
                None,
            )
            if realized is None:
                raise PredictionHistoryError("settled moneyline has no realized outcome")
            paired.append(
                {
                    "month": str(fixture["kickoff"])[:7],
                    "model_probability": float(realized["probability"]),
                    "market_probability": float(
                        realized["market_comparison"]["market_probability"]
                    ),
                }
            )
    months = len({row["month"] for row in paired})
    ready = (
        len(paired) >= BOOKMAKER_MINIMUM_SETTLED_FIXTURE_HORIZONS
        and months >= BOOKMAKER_MINIMUM_CALENDAR_MONTHS
    )
    comparison = None
    if ready:
        model_log_loss = sum(
            -math.log(max(row["model_probability"], 1e-15)) for row in paired
        ) / len(paired)
        market_log_loss = sum(
            -math.log(max(row["market_probability"], 1e-15)) for row in paired
        ) / len(paired)
        comparison = {
            "paired_fixture_horizons": len(paired),
            "model_log_loss": model_log_loss,
            "market_log_loss": market_log_loss,
            "market_minus_model": market_log_loss - model_log_loss,
        }
    return {
        "status": "ready" if ready else "collecting",
        "settled_timestamp_safe_quotes": quote_count,
        "settled_fixture_horizons": len(paired),
        "calendar_months": months,
        "minimum_settled_fixture_horizons": BOOKMAKER_MINIMUM_SETTLED_FIXTURE_HORIZONS,
        "minimum_calendar_months": BOOKMAKER_MINIMUM_CALENDAR_MONTHS,
        "performance_statistics_exposed": ready,
        "gate_policy": "minimums_frozen_before_first_forward_comparison",
        "comparison": comparison,
    }


def _load_bookmaker_comparisons(
    snapshot_directory: Path | None,
) -> dict[tuple[str, str, str], dict[str, dict[str, object]]]:
    if snapshot_directory is None or not snapshot_directory.is_dir():
        return {}
    paths = sorted(
        path for path in snapshot_directory.glob("*.json") if path.name != "latest.json"
    )
    if not paths and (snapshot_directory / "latest.json").is_file():
        paths = [snapshot_directory / "latest.json"]
    comparisons: dict[tuple[str, str, str], dict[str, dict[str, object]]] = {}
    for path in paths:
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PredictionHistoryError(
                "platform snapshot history is unreadable"
            ) from error
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("snapshot_version")
            != "specialized_bet_platform_snapshot_v1"
            or not isinstance(snapshot.get("states"), list)
        ):
            raise PredictionHistoryError("platform snapshot history is invalid")
        for state in snapshot["states"]:
            if not isinstance(state, Mapping):
                raise PredictionHistoryError("platform snapshot state is invalid")
            families = state.get("families")
            if not isinstance(families, list):
                raise PredictionHistoryError("platform snapshot families are invalid")
            key = (
                str(state["fixture_id"]),
                str(state["information_state"]),
                str(state["prediction_at"]),
            )
            family = next(
                (
                    item
                    for item in families
                    if isinstance(item, Mapping)
                    if item["family_key"] == "regulation_moneyline"
                    and item["status"] == "validated"
                ),
                None,
            )
            if family is None:
                continue
            markets = family.get("markets")
            if not isinstance(markets, list):
                raise PredictionHistoryError("platform moneyline markets are invalid")
            outcomes: dict[str, dict[str, object]] = {}
            for market in markets:
                if not isinstance(market, Mapping):
                    raise PredictionHistoryError("platform moneyline market is invalid")
                selection = market.get("selection")
                outcome = (
                    selection.get("outcome")
                    if isinstance(selection, Mapping)
                    else None
                )
                quote = market.get("market_comparison")
                if outcome in {"home_win", "draw", "away_win"} and quote is not None:
                    prediction_at = _timestamp(
                        state.get("prediction_at"),
                        "platform prediction_at",
                    )
                    kickoff = _timestamp(state.get("kickoff"), "platform kickoff")
                    if prediction_at >= kickoff:
                        raise PredictionHistoryError(
                            "quoted platform state is not pre-kickoff"
                        )
                    _validate_bookmaker_comparison(quote, prediction_at)
                    outcomes[str(outcome)] = {
                        "model_probability": float(market["probability"]),
                        "quote": dict(quote),
                    }
            if not outcomes:
                continue
            if set(outcomes) != {"home_win", "draw", "away_win"}:
                raise PredictionHistoryError(
                    "bookmaker comparison is not a complete moneyline"
                )
            existing = comparisons.get(key)
            if existing is not None and existing != outcomes:
                raise PredictionHistoryError(
                    "timestamp-safe bookmaker comparison changed across snapshots"
                )
            comparisons[key] = outcomes
    return comparisons


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise PredictionHistoryError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PredictionHistoryError(f"{field} is invalid") from error
    if parsed.tzinfo is None:
        raise PredictionHistoryError(f"{field} must include a timezone")
    return parsed


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PredictionHistoryError(f"{field} is invalid")
    return value


def _probability(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _logical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
