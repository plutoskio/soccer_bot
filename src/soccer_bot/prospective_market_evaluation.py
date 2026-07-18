from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
from statistics import median
from typing import Any

from soccer_bot.polymarket_evidence import REQUIRED_MONEYLINE
from soccer_bot.prospective_market_settlement import load_market_settlement_ledger


UTC = timezone.utc
READINESS_VERSION = "polymarket_regulation_market_evaluation_readiness_v1"
REPORT_VERSION = "polymarket_regulation_market_evaluation_report_v1"


class ProspectiveMarketEvaluationError(RuntimeError):
    """Raised when the frozen market evaluation cannot be reproduced exactly."""


def update_market_evaluation_readiness(
    *,
    ledger_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    output_directory: Path,
    as_of: datetime,
) -> dict[str, object]:
    prepared = _prepare(
        ledger_path=ledger_path,
        settlement_config_path=settlement_config_path,
        evaluation_config_path=evaluation_config_path,
        as_of=as_of,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / str(
        prepared["config"]["report_policy"]["immutable_filename"]
    )
    existing = _existing_report_summary(report_path, prepared=prepared)
    readiness = _readiness_artifact(prepared, report_summary=existing)
    _assert_count_only(readiness)
    _atomic_json_write(output_directory / "readiness.json", readiness)
    return readiness


def run_one_shot_market_evaluation(
    *,
    ledger_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    output_directory: Path,
    evaluated_at: datetime,
) -> dict[str, object]:
    prepared = _prepare(
        ledger_path=ledger_path,
        settlement_config_path=settlement_config_path,
        evaluation_config_path=evaluation_config_path,
        as_of=evaluated_at,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / str(
        prepared["config"]["report_policy"]["immutable_filename"]
    )
    existing = _existing_report_summary(report_path, prepared=prepared)
    if existing is not None:
        return {
            "status": "report_already_exists",
            **existing,
            "performance_statistics_exposed": False,
        }
    cutoff = prepared["readiness"]["deterministic_evaluation_cutoff_month"]
    if cutoff is None:
        locked = _readiness_artifact(prepared, report_summary=None)
        _assert_count_only(locked)
        _atomic_json_write(output_directory / "readiness.json", locked)
        return locked
    selected = _selected_records(
        prepared["records"],
        first_month=prepared["readiness"]["first_full_calendar_month"],
        cutoff_month=cutoff,
        horizons=tuple(prepared["config"]["horizons"]),
    )
    _validate_performance_rows(selected, config=prepared["config"])
    results = evaluate_market_records(selected, config=prepared["config"])
    report: dict[str, object] = {
        "report_version": REPORT_VERSION,
        "created_at": _utc(evaluated_at).isoformat(),
        "evaluation_version": prepared["config"]["evaluation_version"],
        "evaluation_config_sha256": prepared["evaluation_config_sha256"],
        "settlement_config_sha256": prepared["settlement_config_sha256"],
        "evaluation_module_sha256": _file_sha256(Path(__file__)),
        "input_ledger": {
            "records_observed": len(prepared["records"]),
            "head_sha256": prepared["ledger_head_sha256"],
            "file_sha256": prepared["ledger_file_sha256"],
            "selected_records": len(selected),
            "selected_record_hashes_sha256": _selected_hash(selected),
        },
        "evaluation_window": {
            "first_full_calendar_month": prepared["readiness"][
                "first_full_calendar_month"
            ],
            "deterministic_cutoff_month": cutoff,
            "result_maturity_days_after_month_end": prepared["config"][
                "evaluation_window"
            ]["result_maturity_days_after_month_end"],
        },
        "frozen_program": {
            "minimum_evidence": prepared["config"]["minimum_evidence"],
            "bootstrap": prepared["config"]["bootstrap"],
            "calibration": prepared["config"]["calibration"],
            "selection_bias": prepared["config"]["selection_bias"],
            "execution_estimand": prepared["config"]["execution_estimand"],
        },
        "five_separate_questions": results,
        "interpretation_guardrails": {
            "predictive_accuracy_is_not_executable_edge": True,
            "calibration_is_not_return_on_capital": True,
            "market_disagreement_is_not_a_trade_signal_by_itself": True,
            "paper_execution_is_not_live_fill_or_future_profit": True,
            "uncovered_market_outcomes_are_not_imputed": True,
            "selection_bias_limits_the_estimand_to_observed_supported_coverage": True,
            "automatic_model_promotion": False,
            "automatic_betting": False,
        },
    }
    report["report_record_sha256"] = _logical_sha256(report)
    _write_once_json(report_path, report)
    validated = _validate_report(report_path, prepared=prepared)
    return {
        "status": "evaluation_completed",
        "report_artifact_sha256": validated["report_artifact_sha256"],
        "report_record_sha256": report["report_record_sha256"],
        "deterministic_evaluation_cutoff_month": cutoff,
        "automatic_model_promotion": False,
        "automatic_betting": False,
    }


def build_count_only_market_readiness(
    records: list[Mapping[str, object]],
    *,
    as_of: datetime,
    config: Mapping[str, object],
) -> dict[str, object]:
    first_month = _first_full_month(_timestamp(config["prospective_start_inclusive"]))
    maturity_days = int(config["evaluation_window"]["result_maturity_days_after_month_end"])
    matured = _matured_months(first_month, _utc(as_of), maturity_days)
    eligible = [row for row in records if row["eligible_for_market_evaluation"]]
    cutoff = None
    cutoff_counts = None
    for month in matured:
        counts = _counts_through(
            eligible,
            first_month=first_month,
            cutoff_month=month,
            config=config,
        )
        if all(value["all_minimums_met"] for value in counts.values()):
            cutoff = month
            cutoff_counts = counts
            break
    latest = matured[-1] if matured else None
    current = _counts_through(
        eligible,
        first_month=first_month,
        cutoff_month=latest,
        config=config,
    )
    return {
        "first_full_calendar_month": first_month,
        "latest_matured_calendar_month": latest,
        "available_matured_calendar_months": len(matured),
        "deterministic_evaluation_cutoff_month": cutoff,
        "all_requirements_met": cutoff is not None,
        "horizons": current,
        "counts_at_deterministic_cutoff": cutoff_counts,
    }


def evaluate_market_records(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> dict[str, object]:
    """Compute the five frozen questions only inside the explicit ready path."""

    results: dict[str, object] = {}
    for horizon_index, horizon in enumerate(config["horizons"]):
        universe = [row for row in records if row["information_state"] == horizon]
        if not universe:
            raise ProspectiveMarketEvaluationError(
                f"explicit evaluation has no rows for horizon {horizon}"
            )
        covered = [row for row in universe if row["market_evidence_available"]]
        results[horizon] = {
            "population": {
                "settled_forecast_universe": len(universe),
                "market_covered": len(covered),
                "market_coverage_rate": len(covered) / len(universe),
                "competitions": len({row["competition_id"] for row in universe}),
                "calendar_months": len({_month_key(row["kickoff"]) for row in universe}),
            },
            "1_predictive_accuracy": _predictive_accuracy(
                covered,
                config=config,
                seed_offset=1000 * horizon_index,
            ),
            "2_calibration": {
                "model": _calibration(covered, side="model", config=config),
                "market_no_vig": _calibration(covered, side="market", config=config),
            },
            "3_market_disagreement": _disagreement(covered, config=config),
            "4_executable_edge": _execution_edge(
                covered,
                config=config,
                seed_offset=1000 * horizon_index,
            ),
            "5_selection_bias": _selection_bias(universe, config=config),
        }
    return results


def _predictive_accuracy(
    records: list[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    seed_offset: int,
) -> dict[str, object]:
    output: dict[str, object] = {"records": len(records)}
    for metric in ("log_loss", "brier"):
        model_values = [_market_metric(row, f"model_{metric}") for row in records]
        market_values = [_market_metric(row, f"market_{metric}") for row in records]
        deltas = [left - right for left, right in zip(model_values, market_values)]
        output[metric] = {
            "model_mean": _mean(model_values),
            "market_mean": _mean(market_values),
            "model_minus_market_mean": _mean(deltas),
            "negative_delta_means_model_is_better": True,
            "paired_calendar_month_bootstrap_95_interval": _bootstrap_mean(
                records,
                value=lambda row, name=metric: _market_metric(
                    row, f"model_{name}"
                )
                - _market_metric(row, f"market_{name}"),
                config=config,
                seed_offset=seed_offset + (0 if metric == "log_loss" else 1),
            ),
        }
    return output


def _calibration(
    records: list[Mapping[str, object]],
    *,
    side: str,
    config: Mapping[str, object],
) -> dict[str, object]:
    edges = [float(value) for value in config["calibration"]["bin_edges"]]
    bins = []
    total = len(records) * len(REQUIRED_MONEYLINE)
    weighted_error = 0.0
    maximum_error = 0.0
    for index, (lower, upper) in enumerate(zip(edges, edges[1:])):
        values = []
        outcomes = []
        for row in records:
            probabilities = _probabilities(row, side)
            realized = row["realized_regulation_result"]
            for key in REQUIRED_MONEYLINE:
                probability = probabilities[key]
                inside = lower <= probability <= upper if index == len(edges) - 2 else lower <= probability < upper
                if inside:
                    values.append(probability)
                    outcomes.append(float(key == realized))
        mean_forecast = _mean(values) if values else None
        observed = _mean(outcomes) if outcomes else None
        absolute_error = (
            abs(mean_forecast - observed)
            if mean_forecast is not None and observed is not None
            else None
        )
        if absolute_error is not None:
            weighted_error += len(values) / total * absolute_error
            maximum_error = max(maximum_error, absolute_error)
        bins.append(
            {
                "lower_inclusive": lower,
                "upper_inclusive_only_for_final_bin": upper,
                "count": len(values),
                "mean_forecast_probability": mean_forecast,
                "observed_frequency": observed,
                "absolute_calibration_error": absolute_error,
            }
        )
    return {
        "method": "one_vs_rest_fixed_probability_bins_pooled_across_three_outcomes",
        "selection_forecasts": total,
        "expected_calibration_error": weighted_error,
        "maximum_calibration_error": maximum_error,
        "bins": bins,
    }


def _disagreement(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> dict[str, object]:
    maximum = [_market_metric(row, "maximum_absolute_disagreement") for row in records]
    thresholds = [float(value) for value in config["disagreement"]["absolute_thresholds"]]
    by_selection = {}
    for key in REQUIRED_MONEYLINE:
        values = [
            float(row["market_metrics"]["disagreement"][key]) for row in records
        ]
        by_selection[key] = {
            "mean_signed_model_minus_market": _mean(values),
            "mean_absolute": _mean([abs(value) for value in values]),
        }
    return {
        "maximum_absolute_per_fixture": _distribution_summary(maximum),
        "frequency_above_threshold": {
            _number_key(threshold): sum(value >= threshold for value in maximum)
            / len(maximum)
            for threshold in thresholds
        },
        "by_selection": by_selection,
    }


def _execution_edge(
    records: list[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    seed_offset: int,
) -> dict[str, object]:
    output = {}
    for index, quantity in enumerate(config["execution_estimand"]["share_quantities"]):
        key = _number_key(float(quantity))
        actions = [row["execution_research"][key] for row in records]
        bets = [action for action in actions if action["strategy_action"] != "no_bet"]
        profit = math.fsum(float(action["realized_profit"]) for action in bets)
        capital = math.fsum(float(action["capital_committed"]) for action in bets)
        selected_rows = [
            row
            for row, action in zip(records, actions)
            if action["strategy_action"] != "no_bet"
        ]
        output[key] = {
            "eligible_market_records": len(records),
            "paper_bets": len(bets),
            "selection_rate": len(bets) / len(records),
            "capital_committed": capital,
            "realized_profit": profit,
            "realized_return_on_cost": profit / capital,
            "wins": sum(bool(action["won"]) for action in bets),
            "calendar_month_cluster_bootstrap_95_interval_for_return_on_cost": _bootstrap_ratio(
                selected_rows,
                numerator=lambda row, q=key: float(
                    row["execution_research"][q]["realized_profit"]
                ),
                denominator=lambda row, q=key: float(
                    row["execution_research"][q]["capital_committed"]
                ),
                config=config,
                seed_offset=seed_offset + 100 + index,
            ),
            "actual_orders_or_trades": 0,
        }
    return output


def _selection_bias(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> dict[str, object]:
    covered = [row for row in records if row["market_evidence_available"]]
    uncovered = [row for row in records if not row["market_evidence_available"]]
    strata: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in records:
        strata[(str(row["competition_id"]), _month_key(row["kickoff"]))].append(row)
    minimum = int(config["selection_bias"]["minimum_covered_per_stratum"])
    cap = float(config["selection_bias"]["maximum_inverse_coverage_weight"])
    supported = []
    unsupported = []
    weighted_rows = []
    for (competition, month), rows in sorted(strata.items()):
        observed = [row for row in rows if row["market_evidence_available"]]
        rate = len(observed) / len(rows)
        item = {
            "competition_id": competition,
            "calendar_month": month,
            "universe": len(rows),
            "covered": len(observed),
            "coverage_rate": rate,
        }
        if len(observed) < minimum or rate <= 0 or 1.0 / rate > cap:
            unsupported.append(item)
        else:
            weight = 1.0 / rate
            supported.append({**item, "inverse_coverage_weight": weight})
            weighted_rows.extend((row, weight) for row in observed)
    weighted_model_loss = _weighted_mean(
        [(_market_metric(row, "model_log_loss"), weight) for row, weight in weighted_rows]
    )
    weighted_market_loss = _weighted_mean(
        [(_market_metric(row, "market_log_loss"), weight) for row, weight in weighted_rows]
    )
    weights = [weight for _row, weight in weighted_rows]
    return {
        "coverage": {
            "universe": len(records),
            "covered": len(covered),
            "uncovered": len(uncovered),
            "coverage_rate": len(covered) / len(records),
        },
        "model_accuracy_by_coverage": {
            "covered_mean_log_loss": _mean(
                [float(row["model_metrics"]["log_loss"]) for row in covered]
            ),
            "uncovered_mean_log_loss": _mean(
                [float(row["model_metrics"]["log_loss"]) for row in uncovered]
            )
            if uncovered
            else None,
            "covered_minus_uncovered_mean_log_loss": (
                _mean([float(row["model_metrics"]["log_loss"]) for row in covered])
                - _mean([float(row["model_metrics"]["log_loss"]) for row in uncovered])
                if uncovered
                else None
            ),
        },
        "fixed_stratification": "competition_id_x_calendar_month_within_horizon",
        "supported_strata": len(supported),
        "unsupported_strata": unsupported,
        "positivity_requirement_met_for_every_stratum": not unsupported,
        "standardized_supported_coverage_only": {
            "model_mean_log_loss": weighted_model_loss,
            "market_mean_log_loss": weighted_market_loss,
            "model_minus_market_mean_log_loss": (
                weighted_model_loss - weighted_market_loss
                if weighted_model_loss is not None and weighted_market_loss is not None
                else None
            ),
            "maximum_inverse_coverage_weight": max(weights) if weights else None,
            "effective_sample_size": (
                math.fsum(weights) ** 2 / math.fsum(value * value for value in weights)
                if weights
                else 0.0
            ),
        },
        "identification_statement": (
            "conditional_standardization_supported_for_observed_market_strata_only"
            if not unsupported
            else "full_universe_market_edge_not_identified_due_to_coverage_positivity_gaps"
        ),
        "uncovered_market_probabilities_or_pnl_imputed": False,
    }


def _prepare(
    *,
    ledger_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    as_of: datetime,
) -> dict[str, Any]:
    config = _read_object(evaluation_config_path)
    settlement_config = _read_object(settlement_config_path)
    _validate_config(
        config,
        settlement_config=settlement_config,
        settlement_config_path=settlement_config_path,
    )
    records, head = load_market_settlement_ledger(
        ledger_path=ledger_path, settlement_config_path=settlement_config_path
    )
    _validate_ledger_envelopes(records, config=config)
    readiness = build_count_only_market_readiness(
        records, as_of=as_of, config=config
    )
    return {
        "config": config,
        "records": records,
        "ledger_head_sha256": head,
        "ledger_file_sha256": _file_sha256(ledger_path) if ledger_path.exists() else None,
        "evaluation_config_sha256": _file_sha256(evaluation_config_path),
        "settlement_config_sha256": _file_sha256(settlement_config_path),
        "readiness": readiness,
        "as_of": _utc(as_of),
    }


def _readiness_artifact(
    prepared: Mapping[str, Any], *, report_summary: Mapping[str, object] | None
) -> dict[str, object]:
    status = (
        "report_already_exists"
        if report_summary is not None
        else "ready_for_explicit_one_shot_evaluation"
        if prepared["readiness"]["all_requirements_met"]
        else "locked_insufficient_evidence"
    )
    return {
        "readiness_version": READINESS_VERSION,
        "generated_at": prepared["as_of"].isoformat(),
        "status": status,
        "evaluation_version": prepared["config"]["evaluation_version"],
        "evaluation_config_sha256": prepared["evaluation_config_sha256"],
        "settlement_config_sha256": prepared["settlement_config_sha256"],
        "ledger_records": len(prepared["records"]),
        "ledger_head_sha256": prepared["ledger_head_sha256"],
        "minimum_evidence": prepared["config"]["minimum_evidence"],
        **prepared["readiness"],
        "explicit_one_shot_command_required": True,
        "automatic_evaluation_execution": False,
        "performance_statistics_exposed": False,
        "report_written": report_summary is not None,
        "report_artifact_sha256": (
            report_summary.get("report_artifact_sha256") if report_summary else None
        ),
    }


def _counts_through(
    records: list[Mapping[str, object]],
    *,
    first_month: str,
    cutoff_month: str | None,
    config: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    minimum = config["minimum_evidence"]
    output = {}
    for horizon in config["horizons"]:
        values = [
            row
            for row in records
            if row["information_state"] == horizon
            and cutoff_month is not None
            and first_month <= _month_key(row["kickoff"]) <= cutoff_month
        ]
        covered = [row for row in values if row["market_evidence_available"]]
        executable = [row for row in covered if row["economically_executable"]]
        bet_counts = {
            _number_key(float(quantity)): sum(
                row["execution_research"][_number_key(float(quantity))][
                    "strategy_action"
                ]
                != "no_bet"
                for row in executable
            )
            for quantity in config["execution_estimand"]["share_quantities"]
        }
        criteria = {
            "complete_calendar_month_blocks": len(
                {_month_key(row["kickoff"]) for row in values}
            )
            >= int(minimum["complete_calendar_month_blocks"]),
            "settled_forecast_universe": len(values)
            >= int(minimum["settled_forecast_universe_per_horizon"]),
            "covered_market_records": len(covered)
            >= int(minimum["covered_market_records_per_horizon"]),
            "economically_executable_records": len(executable)
            >= int(minimum["economically_executable_records_per_horizon"]),
            "competitions": len({row["competition_id"] for row in values})
            >= int(minimum["minimum_competitions_per_horizon"]),
            "paper_bets_each_quantity": all(
                value >= int(minimum["paper_bets_per_horizon_per_quantity"])
                for value in bet_counts.values()
            ),
        }
        output[horizon] = {
            "eligible_settled_forecast_universe": len(values),
            "covered_market_records": len(covered),
            "economically_executable_records": len(executable),
            "nonempty_mature_calendar_month_blocks": len(
                {_month_key(row["kickoff"]) for row in values}
            ),
            "competitions": len({row["competition_id"] for row in values}),
            "paper_bets_by_share_quantity": bet_counts,
            "minimums_met": criteria,
            "all_minimums_met": all(criteria.values()),
        }
    return output


def _selected_records(
    records: list[Mapping[str, object]],
    *,
    first_month: str,
    cutoff_month: str,
    horizons: tuple[str, ...],
) -> list[Mapping[str, object]]:
    return sorted(
        [
            row
            for row in records
            if row["eligible_for_market_evaluation"]
            and row["information_state"] in horizons
            and first_month <= _month_key(row["kickoff"]) <= cutoff_month
        ],
        key=lambda row: (
            _timestamp(row["kickoff"]),
            str(row["fixture_id"]),
            str(row["information_state"]),
        ),
    )


def _validate_config(
    config: Mapping[str, object],
    *,
    settlement_config: Mapping[str, object],
    settlement_config_path: Path,
) -> None:
    if config.get("status") != "frozen_before_first_eligible_market_settlement":
        raise ProspectiveMarketEvaluationError("market evaluation is not frozen")
    if config.get("evaluation_version") != "polymarket_regulation_market_evaluation_v1":
        raise ProspectiveMarketEvaluationError("market evaluation version changed")
    if config.get("settlement_config_sha256") != _file_sha256(
        settlement_config_path
    ) or config.get("ledger_version") != settlement_config.get("ledger_version"):
        raise ProspectiveMarketEvaluationError("market evaluation input identity changed")
    if config.get("horizons") != ["pre_lineup_24h_v1", "pre_lineup_72h_clean_v1"]:
        raise ProspectiveMarketEvaluationError("market evaluation horizons changed")
    if config.get("prospective_start_inclusive") != "2026-07-17T00:00:00+00:00":
        raise ProspectiveMarketEvaluationError("market evaluation start changed")
    window = config.get("evaluation_window")
    if not isinstance(window, Mapping) or (
        window.get("timezone") != "UTC"
        or window.get("first_month_policy")
        != "first_full_calendar_month_starting_at_or_after_prospective_start"
        or window.get("cutoff_policy")
        != "first_matured_month_where_all_horizons_meet_every_minimum"
        or int(window.get("result_maturity_days_after_month_end", 0)) != 7
    ):
        raise ProspectiveMarketEvaluationError("market evaluation window changed")
    readiness = config.get("readiness_policy")
    if not isinstance(readiness, Mapping) or (
        readiness.get("automatic_mode") != "counts_only"
        or readiness.get("performance_statistics_before_ready") is not False
        or readiness.get("automatic_evaluation_execution") is not False
        or readiness.get("explicit_one_shot_command_required") is not True
    ):
        raise ProspectiveMarketEvaluationError("anti-peeking policy changed")
    minimum = config.get("minimum_evidence")
    required_positive = (
        "complete_calendar_month_blocks",
        "settled_forecast_universe_per_horizon",
        "covered_market_records_per_horizon",
        "economically_executable_records_per_horizon",
        "minimum_competitions_per_horizon",
        "paper_bets_per_horizon_per_quantity",
    )
    if not isinstance(minimum, Mapping) or any(
        isinstance(minimum.get(key), bool)
        or not isinstance(minimum.get(key), int)
        or int(minimum[key]) <= 0
        for key in required_positive
    ):
        raise ProspectiveMarketEvaluationError("market evidence minimum invalid")
    bootstrap = config.get("bootstrap")
    if not isinstance(bootstrap, Mapping) or (
        bootstrap.get("method") != "calendar_month_cluster_percentile"
        or int(bootstrap.get("replicates", 0)) != 5000
        or int(bootstrap.get("seed", 0)) != 20260718
        or float(bootstrap.get("lower_quantile", -1)) != 0.025
        or float(bootstrap.get("upper_quantile", -1)) != 0.975
    ):
        raise ProspectiveMarketEvaluationError("market bootstrap changed")
    edges = config.get("calibration", {}).get("bin_edges") if isinstance(config.get("calibration"), Mapping) else None
    if edges != [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        raise ProspectiveMarketEvaluationError("calibration bins changed")
    report = config.get("report_policy")
    if not isinstance(report, Mapping) or (
        report.get("write_once") is not True
        or report.get("immutable_filename") != "report.json"
        or report.get("automatic_model_promotion") is not False
        or report.get("automatic_betting") is not False
    ):
        raise ProspectiveMarketEvaluationError("market report policy changed")
    expected = config.get("frozen_artifact_sha256")
    actual = {
        "settlement_config": _file_sha256(settlement_config_path),
        "evaluation_module": _file_sha256(Path(__file__)),
    }
    if not isinstance(expected, Mapping) or set(expected) != set(actual) or any(
        expected[key] != digest for key, digest in actual.items()
    ):
        raise ProspectiveMarketEvaluationError("frozen market evaluator artifact changed")


def _validate_ledger_envelopes(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> None:
    for row in records:
        if (
            row.get("ledger_version") != config["ledger_version"]
            or row.get("information_state") not in config["horizons"]
            or row.get("settlement_config_sha256")
            != config["settlement_config_sha256"]
            or not isinstance(row.get("competition_id"), str)
            or not row.get("competition_id")
            or row.get("orders_or_trading_actions_performed") is not False
        ):
            raise ProspectiveMarketEvaluationError("market ledger identity mismatch")
        _timestamp(row["kickoff"])
        checks = row.get("integrity_checks")
        if not isinstance(checks, Mapping) or not checks or any(
            not isinstance(value, bool) for value in checks.values()
        ) or row.get("eligible_for_market_evaluation") is not all(checks.values()):
            raise ProspectiveMarketEvaluationError("market ledger eligibility invalid")


def _validate_performance_rows(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> None:
    for row in records:
        model = row.get("model_metrics")
        if not isinstance(model, Mapping):
            raise ProspectiveMarketEvaluationError("model metrics missing")
        _finite(model.get("log_loss"))
        _finite(model.get("brier"))
        if row["market_evidence_available"]:
            market = row.get("market_metrics")
            execution = row.get("execution_research")
            if not isinstance(market, Mapping) or not isinstance(execution, Mapping):
                raise ProspectiveMarketEvaluationError("covered market metrics missing")
            for key in (
                "model_log_loss",
                "market_log_loss",
                "model_brier",
                "market_brier",
                "maximum_absolute_disagreement",
            ):
                _finite(market.get(key))
            expected_keys = {
                _number_key(float(value))
                for value in config["execution_estimand"]["share_quantities"]
            }
            if set(execution) != expected_keys:
                raise ProspectiveMarketEvaluationError("execution quantities changed")


def _existing_report_summary(
    path: Path, *, prepared: Mapping[str, Any]
) -> dict[str, object] | None:
    if not path.exists():
        return None
    return _validate_report(path, prepared=prepared)


def _validate_report(
    path: Path, *, prepared: Mapping[str, Any]
) -> dict[str, object]:
    value = _read_object(path)
    expected_record = _logical_sha256(
        {key: item for key, item in value.items() if key != "report_record_sha256"}
    )
    ledger = value.get("input_ledger")
    if (
        value.get("report_version") != REPORT_VERSION
        or value.get("evaluation_version") != prepared["config"]["evaluation_version"]
        or value.get("evaluation_config_sha256")
        != prepared["evaluation_config_sha256"]
        or value.get("settlement_config_sha256")
        != prepared["settlement_config_sha256"]
        or value.get("evaluation_module_sha256") != _file_sha256(Path(__file__))
        or value.get("report_record_sha256") != expected_record
        or not isinstance(ledger, Mapping)
        or int(ledger.get("records_observed", -1)) > len(prepared["records"])
        or ledger.get("head_sha256")
        != (
            prepared["records"][int(ledger["records_observed"]) - 1]["record_sha256"]
            if int(ledger.get("records_observed", 0)) > 0
            else None
        )
        or value.get("interpretation_guardrails", {}).get("automatic_betting")
        is not False
    ):
        raise ProspectiveMarketEvaluationError("immutable market report invalid")
    observed = int(ledger["records_observed"])
    prefix = prepared["records"][:observed]
    cutoff = value["evaluation_window"]["deterministic_cutoff_month"]
    selected = _selected_records(
        prefix,
        first_month=value["evaluation_window"]["first_full_calendar_month"],
        cutoff_month=cutoff,
        horizons=tuple(prepared["config"]["horizons"]),
    )
    if ledger.get("selected_record_hashes_sha256") != _selected_hash(selected):
        raise ProspectiveMarketEvaluationError("market report ledger prefix changed")
    return {
        "report_artifact_sha256": _file_sha256(path),
        "report_record_sha256": value["report_record_sha256"],
        "deterministic_evaluation_cutoff_month": cutoff,
    }


def _bootstrap_mean(
    records: list[Mapping[str, object]],
    *,
    value: Callable[[Mapping[str, object]], float],
    config: Mapping[str, object],
    seed_offset: int,
) -> dict[str, object]:
    return _bootstrap(
        records,
        statistic=lambda sample: _mean([value(row) for row in sample]),
        config=config,
        seed_offset=seed_offset,
    )


def _bootstrap_ratio(
    records: list[Mapping[str, object]],
    *,
    numerator: Callable[[Mapping[str, object]], float],
    denominator: Callable[[Mapping[str, object]], float],
    config: Mapping[str, object],
    seed_offset: int,
) -> dict[str, object]:
    return _bootstrap(
        records,
        statistic=lambda sample: math.fsum(numerator(row) for row in sample)
        / math.fsum(denominator(row) for row in sample),
        config=config,
        seed_offset=seed_offset,
    )


def _bootstrap(
    records: list[Mapping[str, object]],
    *,
    statistic: Callable[[list[Mapping[str, object]]], float],
    config: Mapping[str, object],
    seed_offset: int,
) -> dict[str, object]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in records:
        grouped[_month_key(row["kickoff"])].append(row)
    months = sorted(grouped)
    if not months:
        raise ProspectiveMarketEvaluationError("bootstrap has no calendar months")
    replicates = int(config["bootstrap"]["replicates"])
    rng = random.Random(int(config["bootstrap"]["seed"]) + seed_offset)
    estimates = []
    for _ in range(replicates):
        sampled_months = [months[rng.randrange(len(months))] for _ in months]
        sample = [row for month in sampled_months for row in grouped[month]]
        estimates.append(statistic(sample))
    estimates.sort()
    return {
        "method": "calendar_month_cluster_percentile",
        "replicates": replicates,
        "seed": int(config["bootstrap"]["seed"]) + seed_offset,
        "point_estimate": statistic(records),
        "lower": _quantile(estimates, float(config["bootstrap"]["lower_quantile"])),
        "upper": _quantile(estimates, float(config["bootstrap"]["upper_quantile"])),
    }


def _probabilities(row: Mapping[str, object], side: str) -> dict[str, float]:
    market = row["market_metrics"]
    key = "model_probabilities" if side == "model" else "market_no_vig_probabilities"
    return {name: float(market[key][name]) for name in REQUIRED_MONEYLINE}


def _market_metric(row: Mapping[str, object], key: str) -> float:
    return _finite(row["market_metrics"].get(key))


def _distribution_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean": _mean(values),
        "median": median(ordered),
        "p90": _quantile(ordered, 0.90),
        "p95": _quantile(ordered, 0.95),
        "maximum": ordered[-1],
    }


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ProspectiveMarketEvaluationError("mean requires observations")
    return math.fsum(values) / len(values)


def _weighted_mean(values: list[tuple[float, float]]) -> float | None:
    if not values:
        return None
    denominator = math.fsum(weight for _value, weight in values)
    return math.fsum(value * weight for value, weight in values) / denominator


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values or not 0 <= probability <= 1:
        raise ProspectiveMarketEvaluationError("quantile input invalid")
    position = (len(values) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    return float(values[lower]) + (position - lower) * (
        float(values[upper]) - float(values[lower])
    )


def _matured_months(first: str, as_of: datetime, maturity_days: int) -> list[str]:
    output = []
    current = _month_start(first)
    while _month_matures_at(_month_key(current), maturity_days) <= as_of:
        output.append(_month_key(current))
        current = _next_month(current)
    return output


def _first_full_month(start: datetime) -> str:
    month = datetime(start.year, start.month, 1, tzinfo=UTC)
    return _month_key(month if start == month else _next_month(month))


def _month_matures_at(month: str, maturity_days: int) -> datetime:
    return _next_month(_month_start(month)) + timedelta(days=maturity_days)


def _month_start(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as error:
        raise ProspectiveMarketEvaluationError("calendar month invalid") from error


def _next_month(value: datetime) -> datetime:
    return datetime(value.year + (value.month == 12), value.month % 12 + 1, 1, tzinfo=UTC)


def _month_key(value: object) -> str:
    parsed = value if isinstance(value, datetime) else _timestamp(value)
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProspectiveMarketEvaluationError("timestamp not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProspectiveMarketEvaluationError("timestamp invalid") from error
    if parsed.tzinfo is None:
        raise ProspectiveMarketEvaluationError("timestamp lacks timezone")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ProspectiveMarketEvaluationError("datetime lacks timezone")
    return value.astimezone(UTC)


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProspectiveMarketEvaluationError("metric is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProspectiveMarketEvaluationError("metric is not finite")
    return result


def _number_key(value: float) -> str:
    return format(value, ".15g")


def _selected_hash(records: list[Mapping[str, object]]) -> str:
    return hashlib.sha256(
        "\n".join(str(row["record_sha256"]) for row in records).encode()
    ).hexdigest()


def _assert_count_only(value: Mapping[str, object]) -> None:
    forbidden = (
        "log_loss",
        "brier",
        "calibration",
        "disagreement",
        "profit",
        "return_on_cost",
        "roi",
        "win_rate",
    )

    def walk(item: object, path: str = "") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).lower()
                if any(token in lowered for token in forbidden):
                    raise ProspectiveMarketEvaluationError(
                        f"readiness exposed performance field: {path}{key}"
                    )
                walk(child, f"{path}{key}.")
        elif isinstance(item, list):
            for child in item:
                walk(child, path)

    walk(value)


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
        raise ProspectiveMarketEvaluationError(f"cannot read JSON: {path}") from error
    if not isinstance(value, dict):
        raise ProspectiveMarketEvaluationError(f"JSON is not an object: {path}")
    return value


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_once_json(path: Path, value: Mapping[str, object]) -> None:
    if path.exists():
        raise ProspectiveMarketEvaluationError("immutable report already exists")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError as error:
        raise ProspectiveMarketEvaluationError("immutable report raced") from error
    finally:
        temporary.unlink(missing_ok=True)
