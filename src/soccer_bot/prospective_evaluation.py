from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
from typing import Any

from soccer_bot.modeling.score_grid_shadow import (
    load_score_grid_prospective_gate,
    load_score_grid_shadow_model,
    score_grid_shadow_sha256,
)
from soccer_bot.prospective_settlement import load_prospective_settlement_ledger


READINESS_VERSION = "regulation_score_grid_v3_evaluation_readiness_v1"
DECISION_VERSION = "regulation_score_grid_v3_prospective_decision_v1"
UTC = timezone.utc


class ProspectiveEvaluationError(RuntimeError):
    """Raised when the frozen evaluation program cannot be run faithfully."""


def update_evaluation_readiness(
    *,
    ledger_path: Path,
    model_path: Path,
    gate_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    output_directory: Path,
    as_of: datetime,
) -> dict[str, object]:
    """Write count-only readiness without calculating any performance statistic."""

    prepared = _prepare(
        ledger_path=ledger_path,
        model_path=model_path,
        gate_path=gate_path,
        settlement_config_path=settlement_config_path,
        evaluation_config_path=evaluation_config_path,
        as_of=as_of,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    decision_path = output_directory / str(
        prepared["config"]["decision_policy"]["immutable_filename"]
    )
    decision_summary = _existing_decision_summary(
        decision_path,
        config=prepared["config"],
        evaluation_config_sha256=prepared["evaluation_config_sha256"],
    )
    if decision_summary is not None:
        _validate_decision_against_current_ledger(decision_path, prepared=prepared)
    readiness = _readiness_artifact(prepared, decision_summary=decision_summary)
    _assert_count_only(readiness)
    _atomic_json_write(output_directory / "readiness.json", readiness)
    return readiness


def run_one_shot_evaluation(
    *,
    ledger_path: Path,
    model_path: Path,
    gate_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    output_directory: Path,
    evaluated_at: datetime,
) -> dict[str, object]:
    """Run the frozen decision exactly once, and only after deterministic readiness."""

    prepared = _prepare(
        ledger_path=ledger_path,
        model_path=model_path,
        gate_path=gate_path,
        settlement_config_path=settlement_config_path,
        evaluation_config_path=evaluation_config_path,
        as_of=evaluated_at,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    decision_path = output_directory / str(
        prepared["config"]["decision_policy"]["immutable_filename"]
    )
    existing = _existing_decision_summary(
        decision_path,
        config=prepared["config"],
        evaluation_config_sha256=prepared["evaluation_config_sha256"],
    )
    if existing is not None:
        _validate_decision_against_current_ledger(decision_path, prepared=prepared)
        return {
            "status": "decision_already_exists",
            **existing,
            "performance_statistics_exposed": False,
        }
    readiness = prepared["readiness"]
    cutoff = readiness["deterministic_evaluation_cutoff_month"]
    if cutoff is None:
        locked = _readiness_artifact(prepared, decision_summary=None)
        _assert_count_only(locked)
        _atomic_json_write(output_directory / "readiness.json", locked)
        return locked

    selected = _selected_records(
        prepared["records"],
        first_month=readiness["first_full_calendar_month"],
        cutoff_month=cutoff,
        horizons=tuple(prepared["config"]["horizons"]),
    )
    _validate_selected_metrics(selected, config=prepared["config"])
    evaluation = evaluate_selected_records(
        selected,
        cutoff_month=cutoff,
        config=prepared["config"],
        gate=prepared["gate"],
    )
    decision: dict[str, object] = {
        "decision_artifact_version": DECISION_VERSION,
        "created_at": _utc(evaluated_at).isoformat(),
        "evaluation_version": prepared["config"]["evaluation_version"],
        "model_version": prepared["model"].model_version,
        "logical_model_sha256": score_grid_shadow_sha256(prepared["model"]),
        "prospective_gate_version": prepared["gate"]["gate_version"],
        "evaluation_config_sha256": prepared["evaluation_config_sha256"],
        "prospective_gate_file_sha256": _file_sha256(gate_path),
        "settlement_config_sha256": _file_sha256(settlement_config_path),
        "evaluation_module_sha256": _file_sha256(Path(__file__)),
        "input_ledger": {
            "path_name": ledger_path.name,
            "records_observed": len(prepared["records"]),
            "head_sha256": prepared["ledger_head_sha256"],
            "file_sha256": prepared["ledger_file_sha256"],
            "selected_record_count": len(selected),
            "selected_record_hashes_sha256": _selected_hash(selected),
        },
        "evaluation_window": {
            "first_full_calendar_month": readiness["first_full_calendar_month"],
            "deterministic_cutoff_month": cutoff,
            "cutoff_matured_at": _month_matures_at(
                cutoff,
                int(
                    prepared["config"]["evaluation_window"][
                        "result_maturity_days_after_month_end"
                    ]
                ),
            ).isoformat(),
            "cutoff_policy": prepared["config"]["evaluation_window"][
                "cutoff_policy"
            ],
        },
        "frozen_program": {
            "bootstrap": prepared["config"]["bootstrap"],
            "metric_policy": prepared["config"]["metric_policy"],
            "minimum_evidence": prepared["gate"]["minimum_evidence"],
        },
        "results": evaluation["horizons"],
        "all_primary_and_secondary_gates_pass": evaluation["all_gates_pass"],
        "decision": "pass" if evaluation["all_gates_pass"] else "fail",
        "decision_meaning": (
            "eligible_for_human_promotion_review_only"
            if evaluation["all_gates_pass"]
            else "challenger_rejected_new_version_and_untouched_holdout_required"
        ),
        "automatic_publication_or_betting": False,
    }
    decision["decision_record_sha256"] = _logical_sha256(decision)
    _write_once_json(decision_path, decision)
    validated = _validate_decision(
        decision_path,
        config=prepared["config"],
        evaluation_config_sha256=prepared["evaluation_config_sha256"],
    )
    return {
        "status": "evaluation_completed",
        "decision": decision["decision"],
        "decision_artifact_sha256": validated["decision_artifact_sha256"],
        "decision_record_sha256": decision["decision_record_sha256"],
        "deterministic_evaluation_cutoff_month": cutoff,
        "automatic_publication_or_betting": False,
    }


def build_count_only_readiness(
    records: list[Mapping[str, object]],
    *,
    as_of: datetime,
    config: Mapping[str, object],
    gate: Mapping[str, object],
) -> dict[str, object]:
    """Calculate evidence counts and the first deterministic eligible cutoff."""

    as_of = _utc(as_of)
    holdout = _timestamp(gate["prospective_holdout_kickoff_start_inclusive"])
    first_month = _first_full_month(holdout)
    maturity_days = int(
        config["evaluation_window"]["result_maturity_days_after_month_end"]
    )
    matured_months = _matured_months(first_month, as_of, maturity_days)
    horizons = tuple(config["horizons"])
    eligible = [record for record in records if record["eligible_for_prospective_gate"]]
    cutoff = None
    cutoff_counts = None
    for month in matured_months:
        counts = _counts_through(
            eligible,
            first_month=first_month,
            cutoff_month=month,
            horizons=horizons,
            gate=gate,
        )
        if all(value["all_minimums_met"] for value in counts.values()):
            cutoff = month
            cutoff_counts = counts
            break
    latest = matured_months[-1] if matured_months else None
    current_counts = _counts_through(
        eligible,
        first_month=first_month,
        cutoff_month=latest,
        horizons=horizons,
        gate=gate,
    )
    return {
        "first_full_calendar_month": first_month,
        "latest_matured_calendar_month": latest,
        "available_matured_calendar_months": len(matured_months),
        "deterministic_evaluation_cutoff_month": cutoff,
        "all_requirements_met": cutoff is not None,
        "horizons": current_counts,
        "counts_at_deterministic_cutoff": cutoff_counts,
    }


def evaluate_selected_records(
    records: list[Mapping[str, object]],
    *,
    cutoff_month: str,
    config: Mapping[str, object],
    gate: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate an already-ready, deterministic synthetic or production window."""

    results = {}
    every_check = []
    for horizon in config["horizons"]:
        values = [record for record in records if record["information_state"] == horizon]
        if not values:
            raise ProspectiveEvaluationError(f"ready horizon has no records: {horizon}")
        months = sorted({_month_key(record["kickoff"]) for record in values})
        primary = str(config["metric_policy"]["primary_metric"])
        primary_deltas = [_metric_delta(record, primary) for record in values]
        mean_primary = _mean(primary_deltas)
        interval = paired_month_block_bootstrap(
            values,
            metric=primary,
            replicates=int(config["bootstrap"]["replicates"]),
            seed=int(config["bootstrap"]["seed"]),
            lower_quantile=float(config["bootstrap"]["lower_quantile"]),
            upper_quantile=float(config["bootstrap"]["upper_quantile"]),
        )
        checks: dict[str, bool] = {
            "primary_mean_delta_negative": mean_primary < 0,
            "primary_bootstrap_upper_below_zero": interval["upper"] < 0,
        }
        mean_deltas = {primary: mean_primary}
        for metric in config["metric_policy"]["nonpositive_mean_delta_metrics"]:
            value = _mean([_metric_delta(record, metric) for record in values])
            mean_deltas[metric] = value
            checks[f"{metric}_mean_nonpositive"] = value <= 0
        for metric, maximum in config["metric_policy"]["maximum_mean_delta"].items():
            value = _mean([_metric_delta(record, metric) for record in values])
            mean_deltas[metric] = value
            checks[f"{metric}_mean_at_most_{maximum}"] = value <= float(maximum)
        maximum_moneyline_difference = max(
            max(
                _finite_number(record["metrics"][side]["maximum_absolute_parent_moneyline_difference"])
                for side in ("candidate", "baseline")
            )
            for record in values
        )
        checks["maximum_absolute_parent_moneyline_difference_within_limit"] = (
            maximum_moneyline_difference
            <= float(
                config["metric_policy"][
                    "maximum_absolute_parent_moneyline_difference"
                ]
            )
        )
        all_pass = all(checks.values())
        every_check.append(all_pass)
        results[horizon] = {
            "fixtures": len(values),
            "competitions": len({record["competition_id"] for record in values}),
            "calendar_month_blocks": len(months),
            "first_calendar_month": months[0],
            "last_calendar_month": months[-1],
            "cutoff_month": cutoff_month,
            "mean_candidate_minus_baseline": mean_deltas,
            "exact_score_log_loss_month_block_bootstrap_95_interval": interval,
            "maximum_absolute_parent_moneyline_difference": maximum_moneyline_difference,
            "gate_checks": checks,
            "all_gates_pass": all_pass,
        }
    return {"horizons": results, "all_gates_pass": all(every_check)}


def paired_month_block_bootstrap(
    records: list[Mapping[str, object]],
    *,
    metric: str,
    replicates: int,
    seed: int,
    lower_quantile: float,
    upper_quantile: float,
) -> dict[str, float | int | str]:
    if replicates <= 0 or not 0 <= lower_quantile < upper_quantile <= 1:
        raise ProspectiveEvaluationError("invalid bootstrap configuration")
    grouped: dict[str, list[float]] = defaultdict(list)
    for record in records:
        grouped[_month_key(record["kickoff"])].append(_metric_delta(record, metric))
    months = sorted(grouped)
    if not months:
        raise ProspectiveEvaluationError("bootstrap requires calendar-month blocks")
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sampled = [months[rng.randrange(len(months))] for _ in months]
        values = [value for month in sampled for value in grouped[month]]
        estimates.append(_mean(values))
    estimates.sort()
    return {
        "method": "paired_calendar_month_cluster_percentile",
        "replicates": replicates,
        "seed": seed,
        "point_estimate": _mean(
            [_metric_delta(record, metric) for record in records]
        ),
        "lower": _quantile_type_7(estimates, lower_quantile),
        "upper": _quantile_type_7(estimates, upper_quantile),
    }


def _prepare(
    *,
    ledger_path: Path,
    model_path: Path,
    gate_path: Path,
    settlement_config_path: Path,
    evaluation_config_path: Path,
    as_of: datetime,
) -> dict[str, Any]:
    as_of = _utc(as_of)
    config = _read_object(evaluation_config_path)
    settlement_config = _read_object(settlement_config_path)
    model = load_score_grid_shadow_model(model_path)
    gate = load_score_grid_prospective_gate(gate_path, model=model)
    _validate_config(
        config,
        settlement_config=settlement_config,
        model=model,
        gate=gate,
        model_path=model_path,
        gate_path=gate_path,
        settlement_config_path=settlement_config_path,
    )
    records, head = load_prospective_settlement_ledger(
        ledger_path=ledger_path,
        settlement_config_path=settlement_config_path,
    )
    _validate_ledger_envelopes(
        records,
        config=config,
        settlement_config_sha256=_file_sha256(settlement_config_path),
        gate_file_sha256=_file_sha256(gate_path),
        model_artifact_sha256=_file_sha256(model_path),
    )
    readiness = build_count_only_readiness(
        records,
        as_of=as_of,
        config=config,
        gate=gate,
    )
    return {
        "as_of": as_of,
        "config": config,
        "settlement_config": settlement_config,
        "model": model,
        "gate": gate,
        "records": records,
        "ledger_head_sha256": head,
        "ledger_file_sha256": _file_sha256(ledger_path) if ledger_path.exists() else None,
        "evaluation_config_sha256": _file_sha256(evaluation_config_path),
        "readiness": readiness,
    }


def _readiness_artifact(
    prepared: Mapping[str, Any], *, decision_summary: Mapping[str, object] | None
) -> dict[str, object]:
    readiness = prepared["readiness"]
    if decision_summary is not None:
        status = "decision_already_exists"
    elif readiness["all_requirements_met"]:
        status = "ready_for_explicit_one_shot_evaluation"
    else:
        status = "locked_insufficient_evidence"
    return {
        "readiness_version": READINESS_VERSION,
        "generated_at": prepared["as_of"].isoformat(),
        "status": status,
        "evaluation_version": prepared["config"]["evaluation_version"],
        "model_version": prepared["model"].model_version,
        "logical_model_sha256": score_grid_shadow_sha256(prepared["model"]),
        "prospective_gate_version": prepared["gate"]["gate_version"],
        "evaluation_config_sha256": prepared["evaluation_config_sha256"],
        "ledger_records": len(prepared["records"]),
        "ledger_head_sha256": prepared["ledger_head_sha256"],
        "ledger_file_sha256": prepared["ledger_file_sha256"],
        "minimum_evidence": prepared["gate"]["minimum_evidence"],
        "result_maturity_days_after_month_end": prepared["config"][
            "evaluation_window"
        ]["result_maturity_days_after_month_end"],
        **readiness,
        "explicit_one_shot_command_required": True,
        "automatic_decision_execution": False,
        "performance_statistics_exposed": False,
        "decision_written": decision_summary is not None,
        "decision_artifact_sha256": (
            decision_summary.get("decision_artifact_sha256")
            if decision_summary is not None
            else None
        ),
    }


def _counts_through(
    records: list[Mapping[str, object]],
    *,
    first_month: str,
    cutoff_month: str | None,
    horizons: tuple[str, ...],
    gate: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    minimum = gate["minimum_evidence"]
    output = {}
    for horizon in horizons:
        values = [
            record
            for record in records
            if record["information_state"] == horizon
            and cutoff_month is not None
            and first_month <= _month_key(record["kickoff"]) <= cutoff_month
        ]
        fixtures = len(values)
        months = len({_month_key(record["kickoff"]) for record in values})
        competitions = len({record["competition_id"] for record in values})
        criteria = {
            "complete_calendar_month_blocks": months
            >= int(minimum["complete_calendar_month_blocks"]),
            "settled_fixtures": fixtures >= int(minimum["fixtures_per_horizon"]),
            "competitions": competitions
            >= int(minimum["minimum_competitions_per_horizon"]),
        }
        output[horizon] = {
            "eligible_settled_fixtures": fixtures,
            "nonempty_mature_calendar_month_blocks": months,
            "competitions": competitions,
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
    values = [
        record
        for record in records
        if record["eligible_for_prospective_gate"]
        and record["information_state"] in horizons
        and first_month <= _month_key(record["kickoff"]) <= cutoff_month
    ]
    return sorted(
        values,
        key=lambda record: (
            _timestamp(record["kickoff"]),
            str(record["fixture_id"]),
            str(record["information_state"]),
        ),
    )


def _validate_config(
    config: Mapping[str, object],
    *,
    settlement_config: Mapping[str, object],
    model,
    gate: Mapping[str, object],
    model_path: Path,
    gate_path: Path,
    settlement_config_path: Path,
) -> None:
    if config.get("status") != "frozen_before_first_eligible_settlement":
        raise ProspectiveEvaluationError("evaluation program is not frozen")
    if config.get("evaluation_version") != (
        "regulation_score_grid_v3_prospective_evaluation_v1"
    ) or config.get("frozen_at") != "2026-07-17T21:24:52+00:00":
        raise ProspectiveEvaluationError("evaluation version or freeze time changed")
    if (
        config.get("model_version") != model.model_version
        or config.get("logical_model_sha256") != score_grid_shadow_sha256(model)
        or config.get("prospective_gate_version") != gate["gate_version"]
        or config.get("ledger_version") != settlement_config["ledger_version"]
    ):
        raise ProspectiveEvaluationError("evaluation identity mismatch")
    if config.get("horizons") != [
        "pre_lineup_24h_v1",
        "pre_lineup_72h_clean_v1",
    ]:
        raise ProspectiveEvaluationError("evaluation horizons changed")
    window = config.get("evaluation_window")
    if not isinstance(window, Mapping) or (
        window.get("timezone") != "UTC"
        or window.get("first_month_policy")
        != "first_full_calendar_month_starting_at_or_after_holdout"
        or window.get("month_inclusion_policy")
        != "include_every_calendar_month_from_first_full_month_through_deterministic_cutoff"
        or int(window.get("result_maturity_days_after_month_end", 0)) != 7
        or window.get("cutoff_policy")
        != "first_matured_month_where_all_horizons_meet_every_minimum"
        or window.get("block_count_policy")
        != "nonempty_gate_eligible_calendar_months_per_horizon"
    ):
        raise ProspectiveEvaluationError("evaluation window policy changed")
    readiness = config.get("readiness_policy")
    if not isinstance(readiness, Mapping) or (
        readiness.get("automatic_mode") != "counts_only"
        or readiness.get("performance_statistics_before_ready") is not False
        or readiness.get("automatic_decision_execution") is not False
        or readiness.get("ready_status_requires_explicit_one_shot_command") is not True
    ):
        raise ProspectiveEvaluationError("readiness anti-peeking policy changed")
    bootstrap = config.get("bootstrap")
    gate_bootstrap = gate["uncertainty"]
    if not isinstance(bootstrap, Mapping) or (
        bootstrap.get("method") != "paired_calendar_month_cluster_percentile"
        or bootstrap.get("resampling")
        != "sample_month_labels_with_replacement_then_concatenate_all_paired_fixture_deltas"
        or bootstrap.get("mean_weighting")
        != "fixture_weighted_within_each_resampled_cluster_concatenation"
        or int(bootstrap.get("replicates", 0)) != int(gate_bootstrap["bootstrap_replicates"])
        or int(bootstrap.get("seed", 0)) != int(gate_bootstrap["bootstrap_seed"])
        or float(bootstrap.get("lower_quantile", -1)) != 0.025
        or float(bootstrap.get("upper_quantile", -1)) != 0.975
        or bootstrap.get("quantile_interpolation") != "linear_type_7"
        or bootstrap.get("separate_deterministic_rng_per_horizon") is not True
    ):
        raise ProspectiveEvaluationError("bootstrap program changed")
    _validate_metric_policy(config["metric_policy"], gate)
    decision = config.get("decision_policy")
    if not isinstance(decision, Mapping) or (
        decision.get("write_once") is not True
        or decision.get("immutable_filename") != "decision.json"
        or decision.get("pass_means_eligible_for_human_promotion_review_only") is not True
        or decision.get("automatic_publication_or_betting") is not False
        or decision.get("failure_requires_new_challenger_and_untouched_holdout") is not True
    ):
        raise ProspectiveEvaluationError("one-shot decision policy changed")
    expected = config.get("frozen_artifact_sha256")
    actual = {
        "prospective_gate": _file_sha256(gate_path),
        "settlement_config": _file_sha256(settlement_config_path),
        "shadow_model_artifact": _file_sha256(model_path),
        "evaluation_module": _file_sha256(Path(__file__)),
    }
    if not isinstance(expected, Mapping) or set(expected) != set(actual):
        raise ProspectiveEvaluationError("frozen evaluator artifact registry changed")
    for key, digest in actual.items():
        if expected[key] != digest:
            raise ProspectiveEvaluationError(f"frozen evaluator {key} hash mismatch")


def _validate_metric_policy(policy: object, gate: Mapping[str, object]) -> None:
    if not isinstance(policy, Mapping):
        raise ProspectiveEvaluationError("metric policy is missing")
    primary = gate["primary_gate"]
    secondary = gate["secondary_gates"]
    if (
        policy.get("delta_sign") != "candidate_minus_baseline_negative_is_better"
        or policy.get("primary_metric") != primary["metric"]
        or policy.get("primary_requires_negative_mean_each_horizon")
        is not primary["require_negative_mean_delta_each_horizon"]
        or policy.get("primary_requires_upper_95_percentile_below_zero_each_horizon")
        is not primary[
            "require_paired_month_block_bootstrap_95_upper_below_zero_each_horizon"
        ]
        or policy.get("nonpositive_mean_delta_metrics")
        != secondary["require_nonpositive_mean_delta_each_horizon"]
        or policy.get("maximum_mean_delta")
        != secondary["maximum_mean_delta_each_horizon"]
        or float(policy.get("maximum_absolute_parent_moneyline_difference", -1))
        != float(secondary["maximum_absolute_parent_moneyline_difference"])
        or policy.get("all_gates_must_pass_at_every_horizon") is not True
    ):
        raise ProspectiveEvaluationError("metric policy differs from frozen gate")


def _validate_ledger_envelopes(
    records: list[Mapping[str, object]],
    *,
    config: Mapping[str, object],
    settlement_config_sha256: str,
    gate_file_sha256: str,
    model_artifact_sha256: str,
) -> None:
    for record in records:
        if (
            record.get("ledger_version") != config["ledger_version"]
            or record.get("model_version") != config["model_version"]
            or record.get("logical_model_sha256") != config["logical_model_sha256"]
            or record.get("prospective_gate_version")
            != config["prospective_gate_version"]
            or record.get("settlement_config_sha256") != settlement_config_sha256
            or record.get("prospective_gate_file_sha256") != gate_file_sha256
            or record.get("shadow_model_artifact_sha256") != model_artifact_sha256
            or record.get("information_state") not in config["horizons"]
        ):
            raise ProspectiveEvaluationError("ledger row identity mismatch")
        if not isinstance(record.get("competition_id"), str) or not record[
            "competition_id"
        ]:
            raise ProspectiveEvaluationError("ledger row competition is missing")
        _timestamp(record.get("kickoff"))
        checks = record.get("integrity_checks")
        if not isinstance(checks, Mapping) or not checks or any(
            not isinstance(value, bool) for value in checks.values()
        ):
            raise ProspectiveEvaluationError("ledger integrity vector is invalid")
        if record.get("eligible_for_prospective_gate") is not all(checks.values()):
            raise ProspectiveEvaluationError("ledger gate eligibility is inconsistent")


def _validate_selected_metrics(
    records: list[Mapping[str, object]], *, config: Mapping[str, object]
) -> None:
    """Validate performance fields only inside the ready, explicit one-shot path."""

    required_metrics = {
        str(config["metric_policy"]["primary_metric"]),
        *map(str, config["metric_policy"]["nonpositive_mean_delta_metrics"]),
        *map(str, config["metric_policy"]["maximum_mean_delta"].keys()),
    }
    for record in records:
        metrics = record.get("metrics")
        if not isinstance(metrics, Mapping):
            raise ProspectiveEvaluationError("ledger metrics are missing")
        candidate = metrics.get("candidate")
        baseline = metrics.get("baseline")
        stored_delta = metrics.get("candidate_minus_baseline")
        if not all(isinstance(value, Mapping) for value in (candidate, baseline, stored_delta)):
            raise ProspectiveEvaluationError("ledger metric sections are invalid")
        for metric in required_metrics:
            recomputed = _finite_number(candidate.get(metric)) - _finite_number(
                baseline.get(metric)
            )
            if not math.isclose(
                recomputed,
                _finite_number(stored_delta.get(metric)),
                rel_tol=0.0,
                abs_tol=1e-15,
            ):
                raise ProspectiveEvaluationError("ledger metric delta is inconsistent")
        for side in (candidate, baseline):
            _finite_number(side.get("maximum_absolute_parent_moneyline_difference"))


def _metric_delta(record: Mapping[str, object], metric: str) -> float:
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ProspectiveEvaluationError("record metrics are missing")
    candidate = metrics.get("candidate")
    baseline = metrics.get("baseline")
    if not isinstance(candidate, Mapping) or not isinstance(baseline, Mapping):
        raise ProspectiveEvaluationError("record metric sides are missing")
    return _finite_number(candidate.get(metric)) - _finite_number(baseline.get(metric))


def _existing_decision_summary(
    path: Path,
    *,
    config: Mapping[str, object],
    evaluation_config_sha256: str,
) -> dict[str, object] | None:
    if not path.exists():
        return None
    return _validate_decision(
        path,
        config=config,
        evaluation_config_sha256=evaluation_config_sha256,
    )


def _validate_decision(
    path: Path,
    *,
    config: Mapping[str, object],
    evaluation_config_sha256: str,
) -> dict[str, object]:
    value = _read_object(path)
    expected = _logical_sha256(
        {key: item for key, item in value.items() if key != "decision_record_sha256"}
    )
    all_gates_pass = value.get("all_primary_and_secondary_gates_pass")
    results = value.get("results")
    result_flags = (
        [results[horizon].get("all_gates_pass") for horizon in config["horizons"]]
        if isinstance(results, Mapping)
        and set(results) == set(config["horizons"])
        and all(isinstance(results[horizon], Mapping) for horizon in config["horizons"])
        else None
    )
    expected_decision = "pass" if all_gates_pass is True else "fail"
    expected_meaning = (
        "eligible_for_human_promotion_review_only"
        if expected_decision == "pass"
        else "challenger_rejected_new_version_and_untouched_holdout_required"
    )
    if (
        value.get("decision_artifact_version") != DECISION_VERSION
        or value.get("evaluation_version") != config["evaluation_version"]
        or value.get("model_version") != config["model_version"]
        or value.get("logical_model_sha256") != config["logical_model_sha256"]
        or value.get("prospective_gate_version")
        != config["prospective_gate_version"]
        or value.get("evaluation_config_sha256") != evaluation_config_sha256
        or value.get("prospective_gate_file_sha256")
        != config["frozen_artifact_sha256"]["prospective_gate"]
        or value.get("settlement_config_sha256")
        != config["frozen_artifact_sha256"]["settlement_config"]
        or value.get("evaluation_module_sha256")
        != config["frozen_artifact_sha256"]["evaluation_module"]
        or value.get("decision_record_sha256") != expected
        or not isinstance(all_gates_pass, bool)
        or result_flags is None
        or any(not isinstance(flag, bool) for flag in result_flags)
        or all_gates_pass is not all(result_flags)
        or value.get("decision") != expected_decision
        or value.get("decision_meaning") != expected_meaning
        or value.get("automatic_publication_or_betting") is not False
    ):
        raise ProspectiveEvaluationError("immutable evaluation decision is invalid")
    return {
        "decision_artifact_sha256": _file_sha256(path),
        "decision_record_sha256": value["decision_record_sha256"],
        "deterministic_evaluation_cutoff_month": value["evaluation_window"][
            "deterministic_cutoff_month"
        ],
    }


def _validate_decision_against_current_ledger(
    path: Path, *, prepared: Mapping[str, Any]
) -> None:
    """Prove that an existing decision still has its immutable ledger prefix."""

    value = _read_object(path)
    ledger = value.get("input_ledger")
    window = value.get("evaluation_window")
    if not isinstance(ledger, Mapping) or not isinstance(window, Mapping):
        raise ProspectiveEvaluationError("decision evidence manifest is invalid")
    observed = ledger.get("records_observed")
    records = prepared["records"]
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise ProspectiveEvaluationError("decision ledger length is invalid")
    if observed > len(records):
        raise ProspectiveEvaluationError("decision ledger prefix is missing")
    prefix_head = records[observed - 1]["record_sha256"] if observed else None
    if prefix_head != ledger.get("head_sha256"):
        raise ProspectiveEvaluationError("decision ledger prefix head changed")
    cutoff = window.get("deterministic_cutoff_month")
    first_month = window.get("first_full_calendar_month")
    if not isinstance(cutoff, str) or not isinstance(first_month, str):
        raise ProspectiveEvaluationError("decision evaluation window is invalid")
    selected = _selected_records(
        records[:observed],
        first_month=first_month,
        cutoff_month=cutoff,
        horizons=tuple(prepared["config"]["horizons"]),
    )
    if (
        len(selected) != ledger.get("selected_record_count")
        or _selected_hash(selected) != ledger.get("selected_record_hashes_sha256")
    ):
        raise ProspectiveEvaluationError("decision selected evidence changed")


def _assert_count_only(value: Mapping[str, object]) -> None:
    forbidden = (
        "log_loss",
        "brier",
        "rps",
        "mean_delta",
        "confidence",
        "bootstrap_interval",
        "candidate_minus_baseline",
    )
    body = json.dumps(value, sort_keys=True).lower()
    if any(term in body for term in forbidden):
        raise ProspectiveEvaluationError("readiness artifact exposed performance")
    if value.get("performance_statistics_exposed") is not False:
        raise ProspectiveEvaluationError("readiness performance flag is unsafe")


def _first_full_month(holdout: datetime) -> str:
    holdout = _utc(holdout)
    current = datetime(holdout.year, holdout.month, 1, tzinfo=UTC)
    if holdout == current:
        return _format_month(current)
    return _format_month(_next_month(current))


def _matured_months(first_month: str, as_of: datetime, maturity_days: int) -> list[str]:
    if maturity_days < 0:
        raise ProspectiveEvaluationError("month maturity delay cannot be negative")
    values = []
    month = first_month
    while _month_matures_at(month, maturity_days) <= as_of:
        values.append(month)
        month = _format_month(_next_month(_parse_month(month)))
        if len(values) > 1200:
            raise ProspectiveEvaluationError("calendar-month range is unbounded")
    return values


def _month_matures_at(month: str, maturity_days: int) -> datetime:
    return _next_month(_parse_month(month)) + timedelta(days=maturity_days)


def _month_key(value: object) -> str:
    parsed = _timestamp(value)
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _parse_month(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
    except (TypeError, ValueError) as error:
        raise ProspectiveEvaluationError("invalid calendar month") from error
    return parsed.replace(tzinfo=UTC)


def _format_month(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _next_month(value: datetime) -> datetime:
    return datetime(
        value.year + int(value.month == 12),
        1 if value.month == 12 else value.month + 1,
        1,
        tzinfo=UTC,
    )


def _selected_hash(records: list[Mapping[str, object]]) -> str:
    hashes = [str(record["record_sha256"]) for record in records]
    return hashlib.sha256(
        json.dumps(hashes, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _quantile_type_7(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ProspectiveEvaluationError("quantile requires values")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _mean(values: list[float]) -> float:
    if not values:
        raise ProspectiveEvaluationError("mean requires observations")
    return math.fsum(values) / len(values)


def _finite_number(value: object) -> float:
    if isinstance(value, bool):
        raise ProspectiveEvaluationError("metric is not finite numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ProspectiveEvaluationError("metric is not finite numeric") from error
    if not math.isfinite(parsed):
        raise ProspectiveEvaluationError("metric is not finite numeric")
    return parsed


def _write_once_json(path: Path, value: Mapping[str, object]) -> None:
    body = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            _fsync_directory(path.parent)
        except FileExistsError as error:
            raise ProspectiveEvaluationError(
                "immutable evaluation decision already exists"
            ) from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProspectiveEvaluationError(f"could not read {path.name}") from error
    if not isinstance(value, dict):
        raise ProspectiveEvaluationError(f"{path.name} is not an object")
    return value


def _logical_sha256(value: Mapping[str, object]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProspectiveEvaluationError("timestamp is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProspectiveEvaluationError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ProspectiveEvaluationError("timestamp lacks timezone")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ProspectiveEvaluationError("evaluation time lacks timezone")
    return value.astimezone(UTC)
