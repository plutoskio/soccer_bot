from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from soccer_bot.polymarket_contracts import canonical_json_sha256
from soccer_bot.polymarket_evidence import (
    COVERAGE_UNIVERSE_VERSION,
    EVIDENCE_VERSION,
    REQUIRED_MONEYLINE,
    taker_buy_quote,
)
from soccer_bot.prospective_settlement import load_prospective_settlement_ledger


UTC = timezone.utc


class ProspectiveMarketSettlementError(RuntimeError):
    """Raised when prospective model/market evidence cannot be settled safely."""


def load_market_settlement_ledger(
    *, ledger_path: Path, settlement_config_path: Path
) -> tuple[list[dict[str, object]], str | None]:
    return _read_ledger(ledger_path, config=_read_object(settlement_config_path))


def update_market_settlement_ledger(
    *,
    coverage_universe_directory: Path,
    evidence_directory: Path,
    score_settlement_ledger_path: Path,
    score_settlement_config_path: Path,
    market_policy_path: Path,
    settlement_config_path: Path,
    output_directory: Path,
    settled_at: datetime,
) -> dict[str, object]:
    """Append outcome-linked research rows without changing source forecasts/books."""

    settled_at = _utc(settled_at)
    config = _read_object(settlement_config_path)
    policy = _read_object(market_policy_path)
    _validate_config(
        config,
        policy=policy,
        policy_path=market_policy_path,
        score_settlement_config_path=score_settlement_config_path,
    )
    score_records, score_head = load_prospective_settlement_ledger(
        ledger_path=score_settlement_ledger_path,
        settlement_config_path=score_settlement_config_path,
    )
    coverage = _load_coverage_universe(coverage_universe_directory, config=config)
    evidence = _load_market_evidence(evidence_directory, config=config)

    output_directory.mkdir(parents=True, exist_ok=True)
    ledger_path = output_directory / "ledger.jsonl"
    existing, head = _read_ledger(ledger_path, config=config)
    existing_pairs = {
        (str(row["fixture_id"]), str(row["information_state"])): row
        for row in existing
    }

    score_by_pair: dict[tuple[str, str], Mapping[str, object]] = {}
    for score in score_records:
        pair = (str(score["fixture_id"]), str(score["information_state"]))
        if pair in score_by_pair:
            raise ProspectiveMarketSettlementError("duplicate score settlement pair")
        score_by_pair[pair] = score
    for pair, row in existing_pairs.items():
        score = score_by_pair.get(pair)
        if score is None or row.get("score_settlement_record_sha256") != score.get(
            "record_sha256"
        ):
            raise ProspectiveMarketSettlementError(
                "existing market settlement lost its immutable score outcome"
            )

    pending_coverage = 0
    skipped_ineligible = 0
    new_records: list[dict[str, object]] = []
    for pair, score in sorted(
        score_by_pair.items(),
        key=lambda item: (
            _timestamp(item[1]["kickoff"]),
            item[0][0],
            item[0][1],
        ),
    ):
        if pair in existing_pairs:
            continue
        if score.get("eligible_for_prospective_gate") is not True:
            skipped_ineligible += 1
            continue
        coverage_row = _coverage_for_settled_forecast(
            coverage.get(pair, []), score=score
        )
        if coverage_row is None:
            pending_coverage += 1
            continue
        evidence_row = None
        if coverage_row["market_evidence_available"]:
            evidence_id = str(coverage_row["evidence_id"])
            evidence_row = evidence.get(evidence_id)
            if evidence_row is None:
                raise ProspectiveMarketSettlementError(
                    "covered forecast has no immutable market evidence"
                )
        record = _build_record(
            score=score,
            coverage=coverage_row,
            evidence=evidence_row,
            config=config,
            policy=policy,
            settlement_config_sha256=_file_sha256(settlement_config_path),
            score_settlement_config_sha256=_file_sha256(
                score_settlement_config_path
            ),
            market_policy_sha256=_file_sha256(market_policy_path),
            score_ledger_head_sha256=score_head,
            settled_at=settled_at,
        )
        record["previous_record_sha256"] = head
        record["record_sha256"] = _logical_sha256(record)
        head = str(record["record_sha256"])
        new_records.append(record)

    if new_records:
        _atomic_append_jsonl(ledger_path, new_records)
    all_records, verified_head = _read_ledger(ledger_path, config=config)
    if verified_head != head:
        raise ProspectiveMarketSettlementError("market ledger head changed after append")
    covered = sum(bool(row["market_evidence_available"]) for row in all_records)
    executable = sum(bool(row["economically_executable"]) for row in all_records)
    manifest = {
        "manifest_version": "polymarket_regulation_market_settlement_manifest_v1",
        "generated_at": settled_at.isoformat(),
        "ledger_version": config["ledger_version"],
        "settlement_config_sha256": _file_sha256(settlement_config_path),
        "market_policy_sha256": _file_sha256(market_policy_path),
        "score_settlement_config_sha256": _file_sha256(
            score_settlement_config_path
        ),
        "score_settlement_ledger_head_sha256": score_head,
        "ledger_records": len(all_records),
        "covered_market_records": covered,
        "economically_executable_records": executable,
        "ledger_head_sha256": verified_head,
        "ledger_file_sha256": _file_sha256(ledger_path) if ledger_path.exists() else None,
        "aggregate_performance_written": False,
        "evaluation_report_written": False,
        "orders_or_trading_actions_performed": False,
    }
    _atomic_json_write(output_directory / "manifest.json", manifest)
    return {
        "status": "updated" if new_records else "no_new_settlements",
        "records_added": len(new_records),
        "ledger_records": len(all_records),
        "covered_market_records": covered,
        "economically_executable_records": executable,
        "pending_coverage_records": pending_coverage,
        "skipped_ineligible_records": skipped_ineligible,
        "ledger_head_sha256": verified_head,
        "aggregate_performance_written": False,
        "evaluation_report_written": False,
        "orders_or_trading_actions_performed": False,
    }


def _coverage_for_settled_forecast(
    candidates: list[dict[str, object]],
    *,
    score: Mapping[str, object],
) -> dict[str, object] | None:
    """Select the immutable coverage row for the forecast that was settled.

    A forecast-envelope upgrade can give the same fixture/horizon forecast a
    new row hash without changing its probabilities. Conversely, a corrected
    forecast can share the same prediction time. The score settlement contains
    the parent moneyline probabilities, so use those to distinguish the two
    cases and then retain the first observed equivalent coverage state.
    """

    matching = [
        row
        for row in candidates
        if row.get("prediction_at") == score.get("prediction_at")
        and row.get("kickoff") == score.get("kickoff")
    ]
    if not matching:
        return None
    if len(matching) == 1:
        return matching[0]

    expected = _score_parent_moneyline_probabilities(score)
    probability_matches = []
    for row in matching:
        raw = row.get("model_probabilities")
        if not isinstance(raw, Mapping):
            raise ProspectiveMarketSettlementError(
                "coverage model probabilities missing"
            )
        observed = {
            key: _probability(raw.get(key), "coverage model probability")
            for key in REQUIRED_MONEYLINE
        }
        _require_probability_simplex(observed, "coverage model")
        if all(
            math.isclose(
                observed[key], expected[key], rel_tol=0.0, abs_tol=1e-12
            )
            for key in REQUIRED_MONEYLINE
        ):
            probability_matches.append(row)
    if not probability_matches:
        raise ProspectiveMarketSettlementError(
            "coverage probabilities do not match settled forecast"
        )
    if len(probability_matches) == 1:
        return probability_matches[0]

    first_observed = min(
        _timestamp(row.get("first_observed_at")) for row in probability_matches
    )
    earliest = [
        row
        for row in probability_matches
        if _timestamp(row.get("first_observed_at")) == first_observed
    ]
    if len(earliest) != 1:
        raise ProspectiveMarketSettlementError(
            "multiple equivalent coverage records share first observation time"
        )
    return earliest[0]


def _score_parent_moneyline_probabilities(
    score: Mapping[str, object],
) -> dict[str, float]:
    contracts = score.get("reference_contract_settlements")
    if not isinstance(contracts, Mapping):
        raise ProspectiveMarketSettlementError(
            "settled forecast parent moneyline probabilities missing"
        )
    baseline = contracts.get("baseline")
    handicaps = baseline.get("goal_handicap") if isinstance(baseline, Mapping) else None
    level = handicaps.get("0") if isinstance(handicaps, Mapping) else None
    home = level.get("home") if isinstance(level, Mapping) else None
    forecast = home.get("forecast") if isinstance(home, Mapping) else None
    if not isinstance(forecast, Mapping):
        raise ProspectiveMarketSettlementError(
            "settled forecast parent moneyline probabilities missing"
        )
    probabilities = {
        "home_win": _probability(forecast.get("win"), "settled home probability"),
        "draw": _probability(forecast.get("push"), "settled draw probability"),
        "away_win": _probability(forecast.get("loss"), "settled away probability"),
    }
    _require_probability_simplex(probabilities, "settled parent moneyline")
    return probabilities


def _build_record(
    *,
    score: Mapping[str, object],
    coverage: Mapping[str, object],
    evidence: Mapping[str, object] | None,
    config: Mapping[str, object],
    policy: Mapping[str, object],
    settlement_config_sha256: str,
    score_settlement_config_sha256: str,
    market_policy_sha256: str,
    score_ledger_head_sha256: str | None,
    settled_at: datetime,
) -> dict[str, object]:
    covered = evidence is not None
    if covered is not bool(coverage["market_evidence_available"]):
        raise ProspectiveMarketSettlementError("coverage/evidence status mismatch")
    realized = score.get("realized_regulation_score")
    if not isinstance(realized, Mapping) or realized.get("result") not in REQUIRED_MONEYLINE:
        raise ProspectiveMarketSettlementError("score settlement outcome is invalid")
    outcome = str(realized["result"])
    coverage_model_raw = coverage.get("model_probabilities")
    if not isinstance(coverage_model_raw, Mapping):
        raise ProspectiveMarketSettlementError("coverage model probabilities missing")
    coverage_model = {
        key: _probability(coverage_model_raw.get(key), "coverage model probability")
        for key in REQUIRED_MONEYLINE
    }
    _require_probability_simplex(coverage_model, "coverage model")
    floor = float(config["probability_floor"])
    model_metrics = {
        "probabilities": coverage_model,
        "realized_outcome": outcome,
        "log_loss": -math.log(max(coverage_model[outcome], floor)),
        "brier": math.fsum(
            (coverage_model[key] - float(key == outcome)) ** 2
            for key in REQUIRED_MONEYLINE
        ),
    }
    checks = {
        "score_settlement_gate_eligible": score.get("eligible_for_prospective_gate")
        is True,
        "score_and_coverage_fixture_match": score.get("fixture_id")
        == coverage.get("fixture_id"),
        "score_and_coverage_horizon_match": score.get("information_state")
        == coverage.get("information_state"),
        "score_and_coverage_prediction_time_match": score.get("prediction_at")
        == coverage.get("prediction_at"),
        "score_and_coverage_kickoff_match": score.get("kickoff")
        == coverage.get("kickoff"),
        "coverage_is_outcome_blind": coverage.get(
            "contains_realized_result_or_performance"
        )
        is False,
        "coverage_performed_no_trade": coverage.get("trading_action_performed")
        is False,
    }
    market_metrics = None
    execution = None
    if evidence is not None:
        _validate_evidence_against_coverage(evidence, coverage=coverage, config=config)
        selections = evidence["selections"]
        model = {
            key: _probability(selections[key]["model_probability"], "model probability")
            for key in REQUIRED_MONEYLINE
        }
        market = {
            key: _probability(
                selections[key]["market_no_vig_probability"],
                "market probability",
            )
            for key in REQUIRED_MONEYLINE
        }
        _require_probability_simplex(model, "model")
        _require_probability_simplex(market, "market")
        model_log_loss = -math.log(max(model[outcome], floor))
        market_log_loss = -math.log(max(market[outcome], floor))
        model_brier = math.fsum(
            (model[key] - float(key == outcome)) ** 2 for key in REQUIRED_MONEYLINE
        )
        market_brier = math.fsum(
            (market[key] - float(key == outcome)) ** 2 for key in REQUIRED_MONEYLINE
        )
        market_metrics = {
            "realized_outcome": outcome,
            "model_probabilities": model,
            "market_no_vig_probabilities": market,
            "model_log_loss": model_log_loss,
            "market_log_loss": market_log_loss,
            "model_minus_market_log_loss": model_log_loss - market_log_loss,
            "model_brier": model_brier,
            "market_brier": market_brier,
            "model_minus_market_brier": model_brier - market_brier,
            "disagreement": {
                key: model[key] - market[key] for key in REQUIRED_MONEYLINE
            },
            "maximum_absolute_disagreement": max(
                abs(model[key] - market[key]) for key in REQUIRED_MONEYLINE
            ),
        }
        if canonical_json_sha256(model) != canonical_json_sha256(coverage_model):
            raise ProspectiveMarketSettlementError(
                "coverage and evidence model probabilities differ"
            )
        execution = _settle_execution(
            selections=selections,
            outcome=outcome,
            share_quantities=[float(value) for value in policy["execution"]["share_quantities"]],
            tie_break_order=tuple(config["execution_strategy"]["tie_break_order"]),
        )
        checks.update(
            {
                "evidence_identity_matches_coverage": True,
                "evidence_is_strictly_pre_prediction": all(
                    _timestamp(selections[key]["retrieved_at"])
                    < _timestamp(evidence["prediction_at"])
                    for key in REQUIRED_MONEYLINE
                ),
                "evidence_capture_target_matches_prediction": all(
                    selections[key]["capture_target_at"] == evidence["prediction_at"]
                    and selections[key]["capture_deadline_at"]
                    == evidence["prediction_at"]
                    for key in REQUIRED_MONEYLINE
                ),
                "market_probability_simplex_valid": True,
                "execution_quotes_recomputed": True,
                "market_evidence_is_outcome_blind": evidence.get(
                    "contains_realized_result_or_performance"
                )
                is False,
                "market_evidence_performed_no_trade": evidence.get(
                    "trading_action_performed"
                )
                is False,
            }
        )
    if not all(checks.values()):
        raise ProspectiveMarketSettlementError("market settlement integrity check failed")
    return {
        "ledger_version": config["ledger_version"],
        "fixture_id": score["fixture_id"],
        "competition_id": score["competition_id"],
        "information_state": score["information_state"],
        "prediction_at": score["prediction_at"],
        "kickoff": score["kickoff"],
        "settled_at": settled_at.isoformat(),
        "coverage_id": coverage["coverage_id"],
        "market_evidence_available": covered,
        "economically_executable": bool(
            coverage["economically_executable"] if covered else False
        ),
        "coverage_exclusion_reason": coverage["exclusion_reason"],
        "score_settlement_record_sha256": score["record_sha256"],
        "score_settlement_ledger_head_at_run_sha256": score_ledger_head_sha256,
        "coverage_record_sha256": canonical_json_sha256(coverage),
        "market_evidence_id": evidence.get("evidence_id") if evidence else None,
        "market_evidence_record_sha256": (
            canonical_json_sha256(evidence) if evidence else None
        ),
        "settlement_config_sha256": settlement_config_sha256,
        "score_settlement_config_sha256": score_settlement_config_sha256,
        "market_policy_sha256": market_policy_sha256,
        "realized_regulation_result": outcome,
        "model_metrics": model_metrics,
        "integrity_checks": checks,
        "eligible_for_market_evaluation": all(checks.values()),
        "market_metrics": market_metrics,
        "execution_research": execution,
        "orders_or_trading_actions_performed": False,
    }


def _settle_execution(
    *,
    selections: Mapping[str, object],
    outcome: str,
    share_quantities: list[float],
    tie_break_order: tuple[str, ...],
) -> dict[str, object]:
    if tie_break_order != REQUIRED_MONEYLINE:
        raise ProspectiveMarketSettlementError("execution tie-break order changed")
    output: dict[str, object] = {}
    for quantity in share_quantities:
        candidates = []
        for order, key in enumerate(tie_break_order):
            selection = selections.get(key)
            if not isinstance(selection, Mapping):
                raise ProspectiveMarketSettlementError("market selection is missing")
            asks = selection.get("asks")
            if not isinstance(asks, list):
                raise ProspectiveMarketSettlementError("ask ladder is missing")
            recomputed = taker_buy_quote(
                [(level["price"], level["size"]) for level in asks],
                requested_shares=quantity,
                model_probability=float(selection["model_probability"]),
                fee_rate=(
                    None
                    if selection.get("fee_rate") is None
                    else float(selection["fee_rate"])
                ),
                minimum_order_size=(
                    None
                    if selection.get("minimum_order_size") is None
                    else float(selection["minimum_order_size"])
                ),
            )
            stored = [
                quote
                for quote in selection.get("taker_buy_quotes", [])
                if math.isclose(float(quote.get("requested_shares", -1)), quantity)
            ]
            if len(stored) != 1 or canonical_json_sha256(stored[0]) != canonical_json_sha256(
                recomputed
            ):
                raise ProspectiveMarketSettlementError("stored taker quote mismatch")
            profit = recomputed["model_expected_profit"]
            if (
                recomputed["economically_eligible"]
                and isinstance(profit, (int, float))
                and not isinstance(profit, bool)
                and math.isfinite(float(profit))
                and float(profit) > 0.0
            ):
                candidates.append((float(profit), -order, key, recomputed))
        if not candidates:
            output[_quantity_key(quantity)] = {
                "strategy_action": "no_bet",
                "selection": None,
                "reason": "no_fully_executable_strictly_positive_model_ev_selection",
                "realized_profit": 0.0,
                "capital_committed": 0.0,
            }
            continue
        _profit, _order, selected, quote = max(candidates)
        net_cost = float(quote["net_cost"])
        payout = quantity if selected == outcome else 0.0
        realized_profit = payout - net_cost
        output[_quantity_key(quantity)] = {
            "strategy_action": "paper_buy_yes_as_taker",
            "selection": selected,
            "requested_shares": quantity,
            "model_probability": float(selections[selected]["model_probability"]),
            "gross_cost": quote["gross_cost"],
            "fee": quote["fee"],
            "net_cost": net_cost,
            "vwap": quote["vwap"],
            "model_expected_profit_at_selection": quote["model_expected_profit"],
            "realized_payout": payout,
            "realized_profit": realized_profit,
            "realized_return_on_cost": realized_profit / net_cost,
            "capital_committed": net_cost,
            "won": selected == outcome,
        }
    return output


def _validate_evidence_against_coverage(
    evidence: Mapping[str, object],
    *,
    coverage: Mapping[str, object],
    config: Mapping[str, object],
) -> None:
    if evidence.get("evidence_version") != EVIDENCE_VERSION:
        raise ProspectiveMarketSettlementError("market evidence version mismatch")
    for key in (
        "evidence_id",
        "fixture_id",
        "information_state",
        "prediction_at",
        "kickoff",
        "logical_model_sha256",
        "prediction_row_sha256",
        "policy_sha256",
    ):
        coverage_key = "evidence_id" if key == "evidence_id" else key
        if evidence.get(key) != coverage.get(coverage_key):
            raise ProspectiveMarketSettlementError(
                f"market evidence/coverage {key} mismatch"
            )
    if evidence.get("policy_sha256") != config["market_policy_canonical_sha256"]:
        raise ProspectiveMarketSettlementError("market evidence policy changed")
    selections = evidence.get("selections")
    if not isinstance(selections, Mapping) or set(selections) != set(REQUIRED_MONEYLINE):
        raise ProspectiveMarketSettlementError("market evidence selections invalid")


def _load_coverage_universe(
    directory: Path, *, config: Mapping[str, object]
) -> dict[tuple[str, str], list[dict[str, object]]]:
    output: dict[tuple[str, str], list[dict[str, object]]] = {}
    seen_ids = set()
    if not directory.exists():
        return output
    for path in sorted(directory.rglob("*.json")):
        row = _read_object(path)
        if row.get("coverage_universe_version") != COVERAGE_UNIVERSE_VERSION:
            raise ProspectiveMarketSettlementError("coverage universe version mismatch")
        coverage_id = row.get("coverage_id")
        if coverage_id in seen_ids:
            raise ProspectiveMarketSettlementError("duplicate coverage universe id")
        seen_ids.add(coverage_id)
        if (
            row.get("policy_sha256") != config["market_policy_canonical_sha256"]
            or row.get("contains_realized_result_or_performance") is not False
            or row.get("trading_action_performed") is not False
            or row.get("canonical_policy")
            != "first_observed_coverage_state_per_prediction_row"
        ):
            raise ProspectiveMarketSettlementError("coverage universe guardrail failed")
        expected = hashlib.sha256(
            "|".join(
                (
                    COVERAGE_UNIVERSE_VERSION,
                    str(row["fixture_id"]),
                    str(row["information_state"]),
                    str(row["prediction_at"]),
                    str(row["prediction_row_sha256"]),
                    str(row["logical_model_sha256"]),
                    str(row["policy_sha256"]),
                )
            ).encode("utf-8")
        ).hexdigest()
        if coverage_id != expected or path.stem != coverage_id:
            raise ProspectiveMarketSettlementError("coverage universe id mismatch")
        covered = row.get("market_evidence_available")
        if not isinstance(covered, bool) or (
            covered
            and (
                not isinstance(row.get("evidence_id"), str)
                or row.get("exclusion_reason") is not None
            )
        ) or (
            not covered
            and (
                row.get("evidence_id") is not None
                or not isinstance(row.get("exclusion_reason"), str)
            )
        ):
            raise ProspectiveMarketSettlementError("coverage classification invalid")
        pair = (str(row["fixture_id"]), str(row["information_state"]))
        output.setdefault(pair, []).append(row)
    return output


def _load_market_evidence(
    directory: Path, *, config: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    output = {}
    if not directory.exists():
        return output
    for path in sorted(directory.rglob("*.json")):
        row = _read_object(path)
        evidence_id = row.get("evidence_id")
        if (
            row.get("evidence_version") != EVIDENCE_VERSION
            or not isinstance(evidence_id, str)
            or path.stem != evidence_id
            or evidence_id in output
            or row.get("policy_sha256") != config["market_policy_canonical_sha256"]
        ):
            raise ProspectiveMarketSettlementError("market evidence identity invalid")
        output[evidence_id] = row
    return output


def _validate_config(
    config: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    policy_path: Path,
    score_settlement_config_path: Path,
) -> None:
    if config.get("status") != "frozen_before_first_market_settlement":
        raise ProspectiveMarketSettlementError("market settlement program not frozen")
    if config.get("ledger_version") != "polymarket_regulation_market_settlement_v1":
        raise ProspectiveMarketSettlementError("market settlement version changed")
    if (
        config.get("market_policy_file_sha256") != _file_sha256(policy_path)
        or config.get("market_policy_canonical_sha256")
        != canonical_json_sha256(policy)
        or config.get("market_policy_version") != policy.get("policy_version")
        or config.get("score_settlement_config_sha256")
        != _file_sha256(score_settlement_config_path)
    ):
        raise ProspectiveMarketSettlementError("frozen settlement input changed")
    if config.get("pairing_key") != ["fixture_id", "information_state"]:
        raise ProspectiveMarketSettlementError("market settlement pairing changed")
    if (
        config.get("canonical_coverage_policy")
        != "first_observed_coverage_state_per_prediction_row"
    ):
        raise ProspectiveMarketSettlementError("canonical coverage policy changed")
    if not 0 < float(config.get("probability_floor", 0)) < 1:
        raise ProspectiveMarketSettlementError("market probability floor invalid")
    strategy = config.get("execution_strategy")
    if not isinstance(strategy, Mapping) or (
        strategy.get("one_selection_per_fixture_horizon_quantity") is not True
        or strategy.get("selection_rule")
        != "maximum_strictly_positive_model_expected_profit"
        or strategy.get("tie_break_order") != list(REQUIRED_MONEYLINE)
        or strategy.get("partial_fills_allowed") is not False
        or strategy.get("unknown_fee_allowed") is not False
        or strategy.get("actual_trading") is not False
    ):
        raise ProspectiveMarketSettlementError("execution strategy changed")
    reporting = config.get("reporting_policy")
    if not isinstance(reporting, Mapping) or (
        reporting.get("per_fixture_metrics") is not True
        or reporting.get("aggregate_performance_before_evaluation_gate") is not False
        or reporting.get("evaluation_report_before_minimum_evidence") is not False
    ):
        raise ProspectiveMarketSettlementError("premature market reporting enabled")
    integrity = config.get("integrity")
    required_integrity = (
        "require_verified_score_settlement_hash_chain",
        "require_immutable_coverage_record",
        "require_exact_pair_prediction_time_and_kickoff_match",
        "require_strictly_pre_prediction_market_retrieval",
        "require_market_capture_target_and_deadline_equal_prediction_time",
        "recompute_every_execution_quote",
        "append_only_hash_chain",
        "never_rewrite_settled_records",
        "never_execute_orders",
    )
    if not isinstance(integrity, Mapping) or any(
        integrity.get(key) is not True for key in required_integrity
    ):
        raise ProspectiveMarketSettlementError("market settlement integrity disabled")


def _read_ledger(
    path: Path, *, config: Mapping[str, object]
) -> tuple[list[dict[str, object]], str | None]:
    if not path.exists():
        return [], None
    records = []
    previous = None
    pairs = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProspectiveMarketSettlementError(
                f"market ledger line {number} is invalid JSON"
            ) from error
        if not isinstance(row, dict) or row.get("ledger_version") != config["ledger_version"]:
            raise ProspectiveMarketSettlementError("market ledger envelope invalid")
        if row.get("previous_record_sha256") != previous:
            raise ProspectiveMarketSettlementError("market ledger hash chain broken")
        claimed = row.get("record_sha256")
        expected = _logical_sha256(
            {key: value for key, value in row.items() if key != "record_sha256"}
        )
        pair = (row.get("fixture_id"), row.get("information_state"))
        if claimed != expected or pair in pairs:
            raise ProspectiveMarketSettlementError("market ledger row invalid")
        if row.get("orders_or_trading_actions_performed") is not False:
            raise ProspectiveMarketSettlementError("market ledger claims trading")
        pairs.add(pair)
        previous = str(claimed)
        records.append(row)
    return records, previous


def _require_probability_simplex(value: Mapping[str, float], label: str) -> None:
    if set(value) != set(REQUIRED_MONEYLINE) or not math.isclose(
        math.fsum(value.values()), 1.0, rel_tol=0.0, abs_tol=1e-8
    ):
        raise ProspectiveMarketSettlementError(f"{label} probabilities invalid")


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProspectiveMarketSettlementError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ProspectiveMarketSettlementError(f"{label} is outside [0,1]")
    return result


def _quantity_key(value: float) -> str:
    return format(value, ".15g")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProspectiveMarketSettlementError("timestamp is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProspectiveMarketSettlementError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ProspectiveMarketSettlementError("timestamp lacks timezone")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ProspectiveMarketSettlementError("datetime lacks timezone")
    return value.astimezone(UTC)


def _logical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProspectiveMarketSettlementError(f"cannot read JSON: {path}") from error
    if not isinstance(value, dict):
        raise ProspectiveMarketSettlementError(f"JSON is not an object: {path}")
    return value


def _atomic_append_jsonl(path: Path, records: list[Mapping[str, object]]) -> None:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise ProspectiveMarketSettlementError("market ledger has partial final row")
    appended = b"".join(
        (
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode()
        for row in records
    )
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(existing)
        handle.write(appended)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
