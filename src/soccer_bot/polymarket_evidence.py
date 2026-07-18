from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from .polymarket_contracts import canonical_json_sha256


EVIDENCE_VERSION = "polymarket_prediction_market_evidence_v1"
COVERAGE_VERSION = "polymarket_market_coverage_v1"
REQUIRED_MONEYLINE = ("home_win", "draw", "away_win")


class PolymarketEvidenceError(RuntimeError):
    """Raised when immutable market evidence cannot be trusted."""


def taker_buy_quote(
    asks: Sequence[tuple[object, object]],
    *,
    requested_shares: float,
    model_probability: float,
    fee_rate: float | None,
    minimum_order_size: float | None = None,
) -> dict[str, object]:
    """Walk the visible ask ladder and price an immediate taker buy.

    The payout is one unit per winning share. Fees follow Polymarket's frozen
    sports curve, sum(q_i * r * p_i * (1-p_i)). Missing point-in-time fee
    eligibility deliberately suppresses net economics instead of assuming zero.
    """

    quantity = _finite(requested_shares, "requested_shares")
    probability = _probability(model_probability, "model_probability")
    if quantity <= 0:
        raise PolymarketEvidenceError("requested_shares_must_be_positive")
    parsed: list[tuple[float, float]] = []
    seen_prices: set[float] = set()
    for raw_price, raw_size in asks:
        price = _probability(raw_price, "ask_price", strict=True)
        size = _finite(raw_size, "ask_size")
        if size <= 0:
            raise PolymarketEvidenceError("ask_size_must_be_positive")
        if price in seen_prices:
            raise PolymarketEvidenceError("duplicate_ask_price")
        seen_prices.add(price)
        parsed.append((price, size))
    parsed.sort()
    remaining = quantity
    fills: list[dict[str, float]] = []
    gross_cost = 0.0
    fee = 0.0 if fee_rate is not None else None
    rate = None if fee_rate is None else _finite(fee_rate, "fee_rate")
    if rate is not None and rate < 0:
        raise PolymarketEvidenceError("fee_rate_must_be_nonnegative")
    for price, available in parsed:
        if remaining <= 1e-12:
            break
        filled = min(remaining, available)
        level_cost = filled * price
        gross_cost += level_cost
        if fee is not None and rate is not None:
            fee += filled * rate * price * (1.0 - price)
        fills.append({"price": price, "shares": filled, "gross_cost": level_cost})
        remaining -= filled
    filled_shares = quantity - max(0.0, remaining)
    fully_filled = remaining <= 1e-9
    vwap = gross_cost / filled_shares if filled_shares > 0 else None
    best_ask = parsed[0][0] if parsed else None
    minimum_ok = (
        minimum_order_size is None
        or quantity >= _finite(minimum_order_size, "minimum_order_size")
    )
    net_cost = gross_cost + fee if fee is not None else None
    expected_profit = (
        probability * filled_shares - net_cost
        if net_cost is not None and fully_filled and minimum_ok
        else None
    )
    return {
        "requested_shares": quantity,
        "filled_shares": filled_shares,
        "unfilled_shares": max(0.0, remaining),
        "fully_filled": fully_filled,
        "minimum_order_size_satisfied": minimum_ok,
        "best_ask": best_ask,
        "vwap": vwap,
        "vwap_slippage": (
            vwap - best_ask if vwap is not None and best_ask is not None else None
        ),
        "gross_cost": gross_cost,
        "fee": fee,
        "net_cost": net_cost,
        "model_expected_payout": (
            probability * filled_shares if fully_filled else None
        ),
        "model_expected_profit": expected_profit,
        "economically_eligible": bool(
            fully_filled and minimum_ok and fee is not None
        ),
        "fills": fills,
    }


def capture_polymarket_market_evidence(
    connection,
    *,
    snapshot: Mapping[str, object],
    policy: Mapping[str, object],
    policy_sha256: str,
    output_directory: Path,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Pair immutable pre-cutoff books with a frozen prediction snapshot.

    This function is outcome-blind. Its persistent summary contains only
    coverage counts and exclusion counts; realized results, scores, P&L and
    aggregate performance are structurally absent.
    """

    captured_at = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if canonical_json_sha256(policy) != policy_sha256:
        raise PolymarketEvidenceError("policy_sha256_mismatch")
    predictions = snapshot.get("predictions")
    if not isinstance(predictions, list):
        raise PolymarketEvidenceError("snapshot_predictions_not_list")
    model_hash = _sha256(snapshot.get("logical_model_sha256"), "model_hash")
    prediction_rows_hash = _sha256(
        snapshot.get("prediction_rows_sha256"), "prediction_rows_hash"
    )
    snapshot_hash = canonical_json_sha256(snapshot)
    horizons = policy.get("horizon_stage")
    if not isinstance(horizons, Mapping):
        raise PolymarketEvidenceError("policy_horizon_stage_invalid")
    output_directory.mkdir(parents=True, exist_ok=True)
    evidence_directory = output_directory / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)

    by_horizon: dict[str, dict[str, object]] = {}
    exclusions: dict[str, int] = {}
    written = 0
    existing = 0
    execution_eligible = 0
    for raw_prediction in predictions:
        if not isinstance(raw_prediction, Mapping):
            raise PolymarketEvidenceError("prediction_row_not_object")
        horizon = str(raw_prediction.get("information_state", ""))
        bucket = by_horizon.setdefault(
            horizon,
            {
                "prediction_rows": 0,
                "complete_moneyline_mappings": 0,
                "pre_cutoff_complete_books": 0,
                "valid_bid_ask_books": 0,
                "evidence_records": 0,
                "economically_executable_records": 0,
            },
        )
        bucket["prediction_rows"] = int(bucket["prediction_rows"]) + 1
        stage = horizons.get(horizon)
        if not isinstance(stage, str) or not stage:
            _increment(exclusions, "unsupported_prediction_horizon")
            continue
        try:
            evidence, progress = _build_prediction_evidence(
                connection,
                prediction=raw_prediction,
                snapshot=snapshot,
                snapshot_sha256=snapshot_hash,
                prediction_rows_sha256=prediction_rows_hash,
                model_sha256=model_hash,
                policy=policy,
                policy_sha256=policy_sha256,
                stage=stage,
                captured_at=captured_at,
            )
        except _CoverageExclusion as error:
            for key, value in error.progress.items():
                bucket[key] = int(bucket[key]) + int(value)
            _increment(exclusions, error.reason)
            continue
        for key, value in progress.items():
            bucket[key] = int(bucket[key]) + int(value)
        evidence_id = str(evidence["evidence_id"])
        path = evidence_directory / str(raw_prediction["fixture_id"]) / f"{evidence_id}.json"
        created = _write_once_json(path, evidence)
        written += int(created)
        existing += int(not created)
        bucket["evidence_records"] = int(bucket["evidence_records"]) + 1
        if evidence["economically_executable"]:
            execution_eligible += 1
            bucket["economically_executable_records"] = (
                int(bucket["economically_executable_records"]) + 1
            )

    total_predictions = len(predictions)
    evidence_records = written + existing
    horizon_prediction_rows = 0
    horizon_evidence_records = 0
    horizon_execution_records = 0
    for bucket in by_horizon.values():
        funnel = [
            int(bucket["prediction_rows"]),
            int(bucket["complete_moneyline_mappings"]),
            int(bucket["pre_cutoff_complete_books"]),
            int(bucket["valid_bid_ask_books"]),
            int(bucket["evidence_records"]),
            int(bucket["economically_executable_records"]),
        ]
        if any(left < right for left, right in zip(funnel, funnel[1:])):
            raise PolymarketEvidenceError("horizon_coverage_funnel_invalid")
        horizon_prediction_rows += funnel[0]
        horizon_evidence_records += funnel[4]
        horizon_execution_records += funnel[5]
    if (
        evidence_records > total_predictions
        or execution_eligible > evidence_records
        or horizon_prediction_rows != total_predictions
        or horizon_evidence_records != evidence_records
        or horizon_execution_records != execution_eligible
    ):
        raise PolymarketEvidenceError("coverage_count_invariant_failed")
    coverage = {
        "coverage_version": COVERAGE_VERSION,
        "generated_at": captured_at.isoformat(),
        "policy_version": policy["policy_version"],
        "mapping_version": policy["mapping_version"],
        "policy_sha256": policy_sha256,
        "model_version": snapshot.get("model_version"),
        "logical_model_sha256": model_hash,
        "prediction_rows_sha256": prediction_rows_hash,
        "prediction_rows": total_predictions,
        "new_evidence_records": written,
        "existing_evidence_records": existing,
        "evidence_records": evidence_records,
        "economically_executable_records": execution_eligible,
        "horizons": by_horizon,
        "exclusion_counts": dict(sorted(exclusions.items())),
        "outcome_or_performance_fields_written": False,
        "orders_or_trading_actions_performed": False,
    }
    _atomic_replace_json(output_directory / "coverage.json", coverage)
    receipt = {
        "status": "updated" if written else "no_new_evidence",
        **coverage,
    }
    _append_jsonl(output_directory / "receipts.jsonl", receipt)
    return receipt


class _CoverageExclusion(Exception):
    def __init__(self, reason: str, progress: dict[str, int] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.progress = progress or {}


def _build_prediction_evidence(
    connection,
    *,
    prediction: Mapping[str, object],
    snapshot: Mapping[str, object],
    snapshot_sha256: str,
    prediction_rows_sha256: str,
    model_sha256: str,
    policy: Mapping[str, object],
    policy_sha256: str,
    stage: str,
    captured_at: datetime,
) -> tuple[dict[str, object], dict[str, int]]:
    fixture_id = str(prediction.get("fixture_id", ""))
    if not fixture_id:
        raise PolymarketEvidenceError("prediction_fixture_id_missing")
    prediction_at = _timestamp(prediction.get("prediction_at"), "prediction_at")
    kickoff = _timestamp(prediction.get("kickoff"), "kickoff")
    probabilities = {
        "home_win": _probability(prediction.get("home_win_probability"), "home_win"),
        "draw": _probability(prediction.get("draw_probability"), "draw"),
        "away_win": _probability(prediction.get("away_win_probability"), "away_win"),
    }
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-8):
        raise PolymarketEvidenceError("prediction_probabilities_do_not_sum_to_one")
    prediction_row_sha256 = canonical_json_sha256(prediction)
    mapping_rows = connection.execute(
        """
        SELECT om.canonical_selection,cm.mapping_id,cm.prediction_market_id,
               om.outcome_id,o.source_token_id,cm.rules_sha256
        FROM polymarket_contract_mapping cm
        JOIN polymarket_contract_outcome_mapping om USING (mapping_id)
        JOIN prediction_market_outcome o USING (outcome_id)
        WHERE cm.fixture_id=? AND cm.mapping_version=?
          AND cm.mapping_policy_sha256=? AND cm.mapping_status='accepted'
          AND cm.contract_key='regulation_moneyline' AND om.polarity=1
        ORDER BY om.canonical_selection,cm.prediction_market_id
        """,
        [fixture_id, policy["mapping_version"], policy_sha256],
    ).fetchall()
    grouped: dict[str, list[tuple]] = {key: [] for key in REQUIRED_MONEYLINE}
    for row in mapping_rows:
        if row[0] in grouped:
            grouped[row[0]].append(row)
    if any(len(grouped[key]) != 1 for key in REQUIRED_MONEYLINE):
        reason = (
            "moneyline_mapping_ambiguous"
            if any(len(grouped[key]) > 1 for key in REQUIRED_MONEYLINE)
            else "moneyline_mapping_incomplete"
        )
        raise _CoverageExclusion(reason)
    progress = {"complete_moneyline_mappings": 1}
    selected_books: dict[str, dict[str, object]] = {}
    for selection in REQUIRED_MONEYLINE:
        mapping = grouped[selection][0]
        book = _select_book(
            connection,
            mapping=mapping,
            stage=stage,
            prediction_at=prediction_at,
            kickoff=kickoff,
        )
        if book is None:
            raise _CoverageExclusion("pre_cutoff_book_missing", progress)
        selected_books[selection] = book
    progress["pre_cutoff_complete_books"] = 1
    retrieved = [book["retrieved_at"] for book in selected_books.values()]
    skew = (max(retrieved) - min(retrieved)).total_seconds()
    maximum_skew = int(policy["capture"]["maximum_snapshot_skew_seconds"])
    if skew > maximum_skew:
        raise _CoverageExclusion("moneyline_snapshot_skew_exceeded", progress)
    maximum_spread = float(policy["capture"]["maximum_bid_ask_spread"])
    for book in selected_books.values():
        try:
            _validate_book(book, maximum_spread=maximum_spread)
        except _CoverageExclusion as error:
            raise _CoverageExclusion(error.reason, progress) from error
        except PolymarketEvidenceError as error:
            raise _CoverageExclusion("orderbook_validation_failed", progress) from error
    progress["valid_bid_ask_books"] = 1

    midpoint_sum = sum(
        (float(book["best_bid"]) + float(book["best_ask"])) / 2.0
        for book in selected_books.values()
    )
    if not math.isfinite(midpoint_sum) or midpoint_sum <= 0:
        raise _CoverageExclusion("moneyline_midpoint_normalizer_invalid", progress)
    fee_policy = policy["execution"]["fee_policy"]
    quantities = [float(value) for value in policy["execution"]["share_quantities"]]
    selections: dict[str, object] = {}
    all_execution_eligible = True
    for selection, book in selected_books.items():
        fees_enabled = book["fees_enabled"]
        fee_rate = (
            float(fee_policy["sports_taker_fee_rate"])
            if fees_enabled is True
            else 0.0 if fees_enabled is False else None
        )
        quotes = [
            taker_buy_quote(
                book["asks"],
                requested_shares=quantity,
                model_probability=probabilities[selection],
                fee_rate=fee_rate,
                minimum_order_size=book["minimum_order_size"],
            )
            for quantity in quantities
        ]
        all_execution_eligible = all_execution_eligible and all(
            quote["economically_eligible"] for quote in quotes
        )
        selections[selection] = {
            "model_probability": probabilities[selection],
            "market_midpoint": (
                float(book["best_bid"]) + float(book["best_ask"])
            )
            / 2.0,
            "market_no_vig_probability": (
                (float(book["best_bid"]) + float(book["best_ask"]))
                / 2.0
                / midpoint_sum
            ),
            "model_minus_market_no_vig": probabilities[selection]
            - (
                (float(book["best_bid"]) + float(book["best_ask"]))
                / 2.0
                / midpoint_sum
            ),
            "mapping_id": book["mapping_id"],
            "prediction_market_id": book["prediction_market_id"],
            "outcome_id": book["outcome_id"],
            "source_token_id": book["source_token_id"],
            "rules_sha256": book["rules_sha256"],
            "orderbook_snapshot_id": book["orderbook_snapshot_id"],
            "provider_book_hash": book["book_hash"],
            "raw_artifact_id": book["raw_artifact_id"],
            "raw_content_sha256": book["raw_content_sha256"],
            "observed_at": book["observed_at"].isoformat(),
            "retrieved_at": book["retrieved_at"].isoformat(),
            "capture_target_at": book["capture_target_at"].isoformat(),
            "capture_window_start_at": book["capture_window_start_at"].isoformat(),
            "capture_deadline_at": book["capture_deadline_at"].isoformat(),
            "kickoff_known_at_retrieval": book[
                "kickoff_known_at_retrieval"
            ].isoformat(),
            "best_bid": book["best_bid"],
            "best_ask": book["best_ask"],
            "bid_ask_spread": float(book["best_ask"]) - float(book["best_bid"]),
            "tick_size": book["tick_size"],
            "minimum_order_size": book["minimum_order_size"],
            "last_trade_price": book["last_trade_price"],
            "negative_risk": book["negative_risk"],
            "fees_enabled": fees_enabled,
            "fee_rate": fee_rate,
            "bids": [
                {"price": price, "size": size} for price, size in book["bids"]
            ],
            "asks": [
                {"price": price, "size": size} for price, size in book["asks"]
            ],
            "taker_buy_quotes": quotes,
        }
    evidence_id = hashlib.sha256(
        "|".join(
            (
                EVIDENCE_VERSION,
                fixture_id,
                str(prediction["information_state"]),
                prediction_at.isoformat(),
                model_sha256,
                policy_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return (
        {
            "evidence_version": EVIDENCE_VERSION,
            "evidence_id": evidence_id,
            "captured_at": captured_at.isoformat(),
            "fixture_id": fixture_id,
            "information_state": prediction["information_state"],
            "prediction_at": prediction_at.isoformat(),
            "kickoff": kickoff.isoformat(),
            "model_version": snapshot.get("model_version"),
            "logical_model_sha256": model_sha256,
            "prediction_rows_sha256": prediction_rows_sha256,
            "prediction_row_sha256": prediction_row_sha256,
            "prediction_snapshot_sha256": snapshot_sha256,
            "policy_version": policy["policy_version"],
            "mapping_version": policy["mapping_version"],
            "policy_sha256": policy_sha256,
            "cadence_stage": stage,
            "maximum_snapshot_skew_seconds": maximum_skew,
            "observed_snapshot_skew_seconds": skew,
            "market_consensus_method": "midpoint_then_normalize_three_yes_tokens",
            "economically_executable": all_execution_eligible,
            "selections": selections,
            "contains_realized_result_or_performance": False,
            "trading_action_performed": False,
        },
        progress,
    )


def _select_book(
    connection,
    *,
    mapping: tuple,
    stage: str,
    prediction_at: datetime,
    kickoff: datetime,
) -> dict[str, object] | None:
    (
        _selection,
        mapping_id,
        prediction_market_id,
        outcome_id,
        source_token_id,
        rules_sha256,
    ) = mapping
    rows = connection.execute(
        """
        SELECT s.orderbook_snapshot_id,s.book_hash,s.observed_at,s.retrieved_at,
               s.best_bid,s.best_ask,s.tick_size,s.minimum_order_size,
               s.last_trade_price,s.negative_risk,s.raw_artifact_id,
               r.content_sha256,s.capture_target_at,s.capture_window_start_at,
               s.capture_deadline_at,s.kickoff_known_at_retrieval,
               (SELECT arg_max(mo.fees_enabled,mo.retrieved_at)
                FROM prediction_market_observation mo
                WHERE mo.prediction_market_id=?
                  AND mo.retrieved_at<=s.retrieved_at) AS fees_enabled
        FROM orderbook_snapshot s
        JOIN raw_artifact r USING (raw_artifact_id)
        WHERE s.outcome_id=? AND s.source_token_id=? AND s.cadence_stage=?
          AND s.book_complete=true AND s.capture_timing_valid=true
          AND s.capture_target_at=? AND s.capture_deadline_at=?
          AND s.retrieved_at<? AND s.kickoff_known_at_retrieval=?
        ORDER BY s.retrieved_at DESC,s.orderbook_snapshot_id
        """,
        [
            prediction_market_id,
            outcome_id,
            source_token_id,
            stage,
            prediction_at,
            prediction_at,
            prediction_at,
            kickoff,
        ],
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    levels = connection.execute(
        """
        SELECT side,price,size FROM orderbook_level
        WHERE orderbook_snapshot_id=?
        ORDER BY side,level_index
        """,
        [row[0]],
    ).fetchall()
    bids = [(float(price), float(size)) for side, price, size in levels if side == "bid"]
    asks = [(float(price), float(size)) for side, price, size in levels if side == "ask"]
    fees_enabled = row[16]
    return {
        "mapping_id": mapping_id,
        "prediction_market_id": prediction_market_id,
        "outcome_id": outcome_id,
        "source_token_id": source_token_id,
        "rules_sha256": rules_sha256,
        "orderbook_snapshot_id": row[0],
        "book_hash": row[1],
        "observed_at": _aware(row[2]),
        "retrieved_at": _aware(row[3]),
        "best_bid": row[4],
        "best_ask": row[5],
        "tick_size": row[6],
        "minimum_order_size": row[7],
        "last_trade_price": row[8],
        "negative_risk": row[9],
        "raw_artifact_id": row[10],
        "raw_content_sha256": _sha256(row[11], "raw_content_sha256"),
        "capture_target_at": _aware(row[12]),
        "capture_window_start_at": _aware(row[13]),
        "capture_deadline_at": _aware(row[14]),
        "kickoff_known_at_retrieval": _aware(row[15]),
        "fees_enabled": fees_enabled,
        "bids": bids,
        "asks": asks,
    }


def _validate_book(book: Mapping[str, object], *, maximum_spread: float) -> None:
    best_bid = _probability(book.get("best_bid"), "best_bid", strict=True)
    best_ask = _probability(book.get("best_ask"), "best_ask", strict=True)
    if best_bid > best_ask:
        raise _CoverageExclusion("crossed_orderbook")
    if best_ask - best_bid > maximum_spread + 1e-12:
        raise _CoverageExclusion("bid_ask_spread_too_wide")
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise _CoverageExclusion("two_sided_orderbook_missing")
    parsed_bids = [
        (_probability(price, "bid_price", strict=True), _positive(size, "bid_size"))
        for price, size in bids
    ]
    parsed_asks = [
        (_probability(price, "ask_price", strict=True), _positive(size, "ask_size"))
        for price, size in asks
    ]
    if not math.isclose(max(price for price, _ in parsed_bids), best_bid, abs_tol=1e-12):
        raise _CoverageExclusion("best_bid_level_mismatch")
    if not math.isclose(min(price for price, _ in parsed_asks), best_ask, abs_tol=1e-12):
        raise _CoverageExclusion("best_ask_level_mismatch")


def _write_once_json(path: Path, value: Mapping[str, object]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "evidence_version",
            "evidence_id",
            "fixture_id",
            "information_state",
            "prediction_at",
            "logical_model_sha256",
            "prediction_row_sha256",
            "policy_sha256",
        ):
            if existing.get(key) != value.get(key):
                raise PolymarketEvidenceError("existing_evidence_identity_mismatch")
        return False
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temp_name = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return _write_once_json(path, value)
        return True
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_replace_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PolymarketEvidenceError(f"{label}_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolymarketEvidenceError(f"{label}_invalid") from error
    if parsed.tzinfo is None:
        raise PolymarketEvidenceError(f"{label}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PolymarketEvidenceError("warehouse_timestamp_invalid")
    return value.astimezone(timezone.utc)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise PolymarketEvidenceError(f"{label}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise PolymarketEvidenceError(f"{label}_invalid") from error
    if not math.isfinite(parsed):
        raise PolymarketEvidenceError(f"{label}_not_finite")
    return parsed


def _positive(value: object, label: str) -> float:
    parsed = _finite(value, label)
    if parsed <= 0:
        raise PolymarketEvidenceError(f"{label}_must_be_positive")
    return parsed


def _probability(value: object, label: str, *, strict: bool = False) -> float:
    parsed = _finite(value, label)
    valid = 0 < parsed < 1 if strict else 0 <= parsed <= 1
    if not valid:
        raise PolymarketEvidenceError(f"{label}_outside_unit_interval")
    return parsed


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PolymarketEvidenceError(f"{label}_invalid")
    return value
