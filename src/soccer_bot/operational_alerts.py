from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any


STATUS_VERSION = "prediction_operations_status_v1"
SEVERITY_ORDER = {"warning": 1, "critical": 2}


class OperationalAlertError(RuntimeError):
    """Raised when the watchdog cannot produce a trustworthy status record."""


def run_operational_watchdog(
    *,
    root: Path,
    collector_config: dict,
    publication_result: Mapping[str, object],
    now: datetime,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> dict[str, object]:
    """Evaluate and durably record prediction-pipeline operational alerts.

    The watchdog intentionally depends only on the sanitized publication result,
    append-only publication receipts, configuration identities, and filesystem
    capacity. It never opens the warehouse and never records environment values.
    """

    config = collector_config.get("operations", {})
    if not config.get("enabled", False):
        return {"status": "disabled", "should_fail_run": False, "alerts": []}
    now = _utc(now)
    report_directory = _inside_root(
        root, config.get("report_directory", "data/reports/operations")
    )
    publication_config = collector_config.get("prediction_publication", {})
    publication_report = _inside_root(
        root,
        Path(
            str(
                publication_config.get(
                    "report_directory", "data/reports/predictions"
                )
            )
        )
        / "publication.jsonl",
    )
    stale_after_seconds = _positive_int(
        config.get("publication_stale_after_seconds", 1200),
        "publication_stale_after_seconds",
    )
    cycle_stale_after_seconds = _positive_int(
        config.get("cycle_stale_after_seconds", 1200),
        "cycle_stale_after_seconds",
    )
    warning_percent = _percentage(
        config.get("volume_warning_percent", 80), "volume_warning_percent"
    )
    critical_percent = _percentage(
        config.get("volume_critical_percent", 95), "volume_critical_percent"
    )
    if warning_percent >= critical_percent:
        raise OperationalAlertError(
            "volume_warning_percent must be below volume_critical_percent"
        )

    alerts: list[dict[str, object]] = []
    checks: dict[str, object] = {}
    _evaluate_publication(
        alerts=alerts,
        checks=checks,
        result=publication_result,
        publication_config=publication_config,
        report_path=publication_report,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )
    _evaluate_volume(
        alerts=alerts,
        checks=checks,
        data_path=root / "data",
        disk_usage=disk_usage,
        warning_percent=warning_percent,
        critical_percent=critical_percent,
    )
    alerts.sort(key=lambda item: (-SEVERITY_ORDER[str(item["severity"])], str(item["code"])))
    overall = (
        "critical"
        if any(item["severity"] == "critical" for item in alerts)
        else "warning" if alerts else "ok"
    )
    status: dict[str, object] = {
        "status_version": STATUS_VERSION,
        "generated_at": now.isoformat(),
        "cycle_stale_after_seconds": cycle_stale_after_seconds,
        "expected_next_cycle_by": (
            now + timedelta(seconds=cycle_stale_after_seconds)
        ).isoformat(),
        "overall_status": overall,
        "checks": checks,
        "alerts": alerts,
        "should_fail_run": bool(
            config.get("fail_run_on_critical", True) and overall == "critical"
        ),
    }
    _write_status_and_transitions(report_directory, status)
    return status


def _evaluate_publication(
    *,
    alerts: list[dict[str, object]],
    checks: dict[str, object],
    result: Mapping[str, object],
    publication_config: Mapping[str, object],
    report_path: Path,
    now: datetime,
    stale_after_seconds: int,
) -> None:
    expected_version = str(publication_config.get("model_version", ""))
    expected_hash = str(publication_config.get("logical_model_sha256", ""))
    expected_reproducibility_hash = str(
        publication_config.get("reproducibility_sha256", "")
    )
    minimum_rows = int(publication_config.get("minimum_prediction_rows", 1))
    publication_status = str(result.get("status", "missing"))
    current_as_of = _optional_timestamp(result.get("as_of"))
    latest_success = (
        current_as_of
        if publication_status == "uploaded" and current_as_of is not None
        else _latest_successful_as_of(report_path)
    )
    age_seconds = (
        max(0.0, (now - latest_success).total_seconds())
        if latest_success is not None
        else None
    )
    checks["champion_publication"] = {
        "status": publication_status,
        "expected_model_version": expected_version,
        "observed_model_version": result.get("model_version"),
        "expected_logical_model_sha256": expected_hash,
        "observed_logical_model_sha256": result.get("logical_model_sha256"),
        "expected_reproducibility_sha256": expected_reproducibility_hash,
        "observed_reproducibility_sha256": result.get(
            "model_reproducibility_sha256"
        ),
        "prediction_rows": result.get("prediction_rows"),
        "last_successful_as_of": latest_success.isoformat() if latest_success else None,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "stale_after_seconds": stale_after_seconds,
    }
    if publication_status != "uploaded":
        _add_alert(
            alerts,
            code="champion_publication_failed",
            severity="critical",
            component="champion_publication",
            summary=f"Champion publication status is {publication_status}",
        )
    if result.get("report_status") == "failed":
        _add_alert(
            alerts,
            code="publication_receipt_write_failed",
            severity="critical",
            component="champion_publication",
            summary="The append-only publication receipt could not be written",
        )
    if latest_success is None or age_seconds is None or age_seconds > stale_after_seconds:
        _add_alert(
            alerts,
            code="champion_publication_stale",
            severity="critical",
            component="champion_publication",
            summary="No sufficiently fresh successful champion publication exists",
        )
    if publication_status == "uploaded":
        if result.get("model_version") != expected_version or result.get(
            "logical_model_sha256"
        ) != expected_hash or (
            expected_reproducibility_hash
            and result.get("model_reproducibility_sha256")
            != expected_reproducibility_hash
        ):
            _add_alert(
                alerts,
                code="champion_model_identity_mismatch",
                severity="critical",
                component="champion_publication",
                summary="Published champion identity differs from frozen configuration",
            )
        rows = _nonnegative_int(result.get("prediction_rows"))
        if rows is None or rows < minimum_rows:
            _add_alert(
                alerts,
                code="champion_prediction_rows_below_minimum",
                severity="critical",
                component="champion_publication",
                summary="Champion publication contains fewer rows than configured",
            )
    else:
        publication_error = str(result.get("error", ""))
        if "mismatch" in publication_error:
            _add_alert(
                alerts,
                code="champion_model_identity_mismatch",
                severity="critical",
                component="champion_publication",
                summary="Champion publication failed an identity check",
            )
        if "below_minimum_prediction_rows" in publication_error:
            _add_alert(
                alerts,
                code="champion_prediction_rows_below_minimum",
                severity="critical",
                component="champion_publication",
                summary="Champion candidate contains fewer rows than configured",
            )

    platform_config = publication_config.get("specialized_platform", {})
    if not isinstance(platform_config, Mapping) or not platform_config.get(
        "enabled", False
    ):
        checks["specialized_platform"] = {"status": "disabled"}
    else:
        platform = result.get("specialized_platform")
        if not isinstance(platform, Mapping):
            platform = {}
        platform_status = str(platform.get("status", "missing"))
        state_rows = _nonnegative_int(platform.get("state_rows"))
        checks["specialized_platform"] = {
            "status": platform_status,
            "snapshot_version": platform.get("snapshot_version"),
            "family_registry_version": platform.get("family_registry_version"),
            "state_rows": state_rows,
            "fixture_count": _nonnegative_int(platform.get("fixture_count")),
            "ranking_policy": platform.get("ranking_policy"),
            "forward_evidence": platform.get("forward_evidence"),
        }
        if platform_status != "uploaded":
            _add_alert(
                alerts,
                code="specialized_platform_publication_failed",
                severity="critical",
                component="specialized_platform",
                summary=f"Specialized platform status is {platform_status}",
            )
        elif (
            state_rows is None
            or state_rows < int(platform_config.get("minimum_state_rows", 1))
            or platform.get("ranking_policy") != "validated_families_only"
        ):
            _add_alert(
                alerts,
                code="specialized_platform_safety_check_failed",
                severity="critical",
                component="specialized_platform",
                summary="Specialized platform rows or ranking policy are unsafe",
            )

    evidence_config = publication_config.get("polymarket_market_evidence", {})
    if not isinstance(evidence_config, Mapping) or not evidence_config.get(
        "enabled", False
    ):
        checks["polymarket_market_evidence"] = {"status": "disabled"}
    else:
        evidence = result.get("polymarket_market_evidence")
        if not isinstance(evidence, Mapping):
            evidence = {}
        evidence_status = str(evidence.get("status", "missing"))
        evidence_counts = {
            key: _nonnegative_int(evidence.get(key))
            for key in (
                "prediction_rows",
                "new_evidence_records",
                "existing_evidence_records",
                "evidence_records",
                "economically_executable_records",
                "new_coverage_universe_records",
                "existing_coverage_universe_records",
                "coverage_universe_records",
            )
        }
        checks["polymarket_market_evidence"] = {
            "status": evidence_status,
            "expected_policy_sha256": evidence_config.get("policy_sha256"),
            "observed_policy_sha256": evidence.get("policy_sha256"),
            **evidence_counts,
            "horizons": evidence.get("horizons"),
            "exclusion_counts": evidence.get("exclusion_counts"),
            "outcome_or_performance_fields_written": evidence.get(
                "outcome_or_performance_fields_written"
            ),
            "orders_or_trading_actions_performed": evidence.get(
                "orders_or_trading_actions_performed"
            ),
        }
        if evidence_status not in {"updated", "no_new_evidence"}:
            _add_alert(
                alerts,
                code="polymarket_market_evidence_failed",
                severity="critical",
                component="polymarket_market_evidence",
                summary=f"Polymarket evidence status is {evidence_status}",
            )
        else:
            if evidence.get("policy_sha256") != evidence_config.get(
                "policy_sha256"
            ):
                _add_alert(
                    alerts,
                    code="polymarket_evidence_policy_identity_mismatch",
                    severity="critical",
                    component="polymarket_market_evidence",
                    summary="Polymarket evidence policy differs from frozen configuration",
                )
            count_values = list(evidence_counts.values())
            invalid_counts = any(value is None for value in count_values)
            if not invalid_counts:
                invalid_counts = (
                    evidence_counts["new_evidence_records"]
                    + evidence_counts["existing_evidence_records"]
                    != evidence_counts["evidence_records"]
                    or evidence_counts["evidence_records"]
                    > evidence_counts["prediction_rows"]
                    or evidence_counts["economically_executable_records"]
                    > evidence_counts["evidence_records"]
                    or evidence_counts["new_coverage_universe_records"]
                    + evidence_counts["existing_coverage_universe_records"]
                    != evidence_counts["coverage_universe_records"]
                    or evidence_counts["coverage_universe_records"]
                    != evidence_counts["prediction_rows"]
                )
            horizons = evidence.get("horizons")
            if not isinstance(horizons, Mapping):
                invalid_counts = True
            elif not invalid_counts:
                horizon_totals = [0, 0, 0]
                for horizon in horizons.values():
                    if not isinstance(horizon, Mapping):
                        invalid_counts = True
                        break
                    funnel = [
                        _nonnegative_int(horizon.get(key))
                        for key in (
                            "prediction_rows",
                            "complete_moneyline_mappings",
                            "pre_cutoff_complete_books",
                            "valid_bid_ask_books",
                            "evidence_records",
                            "economically_executable_records",
                        )
                    ]
                    if any(value is None for value in funnel) or any(
                        left < right
                        for left, right in zip(funnel, funnel[1:])
                    ):
                        invalid_counts = True
                        break
                    horizon_totals[0] += funnel[0]
                    horizon_totals[1] += funnel[4]
                    horizon_totals[2] += funnel[5]
                if horizon_totals != [
                    evidence_counts["prediction_rows"],
                    evidence_counts["evidence_records"],
                    evidence_counts["economically_executable_records"],
                ]:
                    invalid_counts = True
            if invalid_counts:
                _add_alert(
                    alerts,
                    code="polymarket_evidence_receipt_invalid",
                    severity="critical",
                    component="polymarket_market_evidence",
                    summary="Polymarket evidence returned internally inconsistent counts",
                )
            if (
                evidence.get("outcome_or_performance_fields_written") is not False
                or evidence.get("orders_or_trading_actions_performed") is not False
            ):
                _add_alert(
                    alerts,
                    code="polymarket_evidence_safety_violation",
                    severity="critical",
                    component="polymarket_market_evidence",
                    summary="Polymarket evidence violated outcome-blind or read-only guardrails",
                )
            if isinstance(horizons, Mapping):
                mapped = 0
                books = 0
                for horizon in horizons.values():
                    if not isinstance(horizon, Mapping):
                        continue
                    mapped += _nonnegative_int(
                        horizon.get("complete_moneyline_mappings")
                    ) or 0
                    books += _nonnegative_int(
                        horizon.get("pre_cutoff_complete_books")
                    ) or 0
                if mapped > books:
                    _add_alert(
                        alerts,
                        code="polymarket_pre_cutoff_capture_gap",
                        severity="warning",
                        component="polymarket_market_evidence",
                        summary="Mapped prediction rows are missing complete pre-cutoff books",
                    )

    research_config = (
        evidence_config.get("market_research", {})
        if isinstance(evidence_config, Mapping)
        else {}
    )
    if not isinstance(research_config, Mapping) or not research_config.get(
        "enabled", False
    ):
        checks["polymarket_market_settlement_ledger"] = {"status": "disabled"}
        checks["polymarket_market_evaluation_readiness"] = {"status": "disabled"}
    else:
        settlement = result.get("polymarket_market_settlement_ledger")
        if not isinstance(settlement, Mapping):
            settlement = {}
        settlement_status = str(settlement.get("status", "missing"))
        settlement_counts = {
            key: _nonnegative_int(settlement.get(key))
            for key in (
                "records_added",
                "ledger_records",
                "covered_market_records",
                "economically_executable_records",
                "pending_coverage_records",
                "skipped_ineligible_records",
            )
        }
        settlement_head = settlement.get("ledger_head_sha256")
        checks["polymarket_market_settlement_ledger"] = {
            "status": settlement_status,
            "expected_settlement_config_sha256": research_config.get(
                "settlement_config_sha256"
            ),
            "observed_settlement_config_sha256": settlement.get(
                "settlement_config_sha256"
            ),
            **settlement_counts,
            "ledger_head_sha256": settlement_head,
            "aggregate_performance_written": settlement.get(
                "aggregate_performance_written"
            ),
            "evaluation_report_written": settlement.get(
                "evaluation_report_written"
            ),
            "orders_or_trading_actions_performed": settlement.get(
                "orders_or_trading_actions_performed"
            ),
        }
        valid_settlement = {"updated", "no_new_settlements"}
        if settlement_status not in valid_settlement:
            _add_alert(
                alerts,
                code="polymarket_market_settlement_failed",
                severity="critical",
                component="polymarket_market_settlement_ledger",
                summary=f"Polymarket market settlement status is {settlement_status}",
            )
        else:
            invalid = (
                any(value is None for value in settlement_counts.values())
                or settlement_counts["records_added"]
                > settlement_counts["ledger_records"]
                or settlement_counts["covered_market_records"]
                > settlement_counts["ledger_records"]
                or settlement_counts["economically_executable_records"]
                > settlement_counts["covered_market_records"]
                or (
                    settlement_counts["ledger_records"] > 0
                    and not _is_lowercase_sha256(settlement_head)
                )
                or (
                    settlement_counts["ledger_records"] == 0
                    and settlement_head is not None
                )
            )
            if invalid:
                _add_alert(
                    alerts,
                    code="polymarket_market_settlement_receipt_invalid",
                    severity="critical",
                    component="polymarket_market_settlement_ledger",
                    summary="Polymarket market settlement returned inconsistent counts",
                )
            if settlement.get("settlement_config_sha256") != research_config.get(
                "settlement_config_sha256"
            ):
                _add_alert(
                    alerts,
                    code="polymarket_market_settlement_identity_mismatch",
                    severity="critical",
                    component="polymarket_market_settlement_ledger",
                    summary="Polymarket market settlement configuration changed",
                )
            if (
                settlement.get("aggregate_performance_written") is not False
                or settlement.get("evaluation_report_written") is not False
                or settlement.get("orders_or_trading_actions_performed") is not False
            ):
                _add_alert(
                    alerts,
                    code="premature_or_unsafe_polymarket_market_output",
                    severity="critical",
                    component="polymarket_market_settlement_ledger",
                    summary="Market settlement violated anti-peeking or no-trading guardrails",
                )

        evaluation_config = research_config.get("evaluation_program", {})
        readiness = result.get("polymarket_market_evaluation_readiness")
        if not isinstance(evaluation_config, Mapping) or not evaluation_config.get(
            "enabled", False
        ):
            checks["polymarket_market_evaluation_readiness"] = {"status": "disabled"}
        else:
            if not isinstance(readiness, Mapping):
                readiness = {}
            readiness_status = str(readiness.get("status", "missing"))
            checks["polymarket_market_evaluation_readiness"] = {
                "status": readiness_status,
                "expected_evaluation_config_sha256": evaluation_config.get(
                    "evaluation_config_sha256"
                ),
                "observed_evaluation_config_sha256": readiness.get(
                    "evaluation_config_sha256"
                ),
                "ledger_records": readiness.get("ledger_records"),
                "horizons": readiness.get("horizons"),
                "performance_statistics_exposed": readiness.get(
                    "performance_statistics_exposed"
                ),
                "automatic_evaluation_execution": readiness.get(
                    "automatic_evaluation_execution"
                ),
                "report_written": readiness.get("report_written"),
            }
            valid_readiness = {
                "locked_insufficient_evidence",
                "ready_for_explicit_one_shot_evaluation",
                "report_already_exists",
            }
            if readiness_status not in valid_readiness:
                _add_alert(
                    alerts,
                    code="polymarket_market_evaluation_readiness_failed",
                    severity="critical",
                    component="polymarket_market_evaluation_readiness",
                    summary=f"Polymarket evaluation readiness is {readiness_status}",
                )
            else:
                if (
                    readiness.get("performance_statistics_exposed") is not False
                    or readiness.get("automatic_evaluation_execution") is not False
                    or readiness.get("explicit_one_shot_command_required") is not True
                ):
                    _add_alert(
                        alerts,
                        code="polymarket_market_evaluation_readiness_unsafe",
                        severity="critical",
                        component="polymarket_market_evaluation_readiness",
                        summary="Polymarket readiness exposed performance or auto-ran evaluation",
                    )
                if readiness.get(
                    "evaluation_config_sha256"
                ) != evaluation_config.get("evaluation_config_sha256"):
                    _add_alert(
                        alerts,
                        code="polymarket_market_evaluation_identity_mismatch",
                        severity="critical",
                        component="polymarket_market_evaluation_readiness",
                        summary="Polymarket evaluation configuration changed",
                    )
                readiness_records = _nonnegative_int(readiness.get("ledger_records"))
                if readiness_records != settlement_counts["ledger_records"]:
                    _add_alert(
                        alerts,
                        code="polymarket_market_evaluation_ledger_count_mismatch",
                        severity="critical",
                        component="polymarket_market_evaluation_readiness",
                        summary="Polymarket readiness and settlement ledger counts differ",
                    )
                if readiness_status == "ready_for_explicit_one_shot_evaluation":
                    _add_alert(
                        alerts,
                        code="polymarket_market_evaluation_ready",
                        severity="warning",
                        component="polymarket_market_evaluation_readiness",
                        summary="Frozen Polymarket report minimums are met; explicit evaluation is ready",
                    )

    player_config = publication_config.get("confirmed_lineup_player_shadow", {})
    if not isinstance(player_config, Mapping) or not player_config.get(
        "enabled", False
    ):
        checks["confirmed_lineup_player_shadow"] = {"status": "disabled"}
    else:
        player_shadow = result.get("confirmed_lineup_player_shadow")
        if not isinstance(player_shadow, Mapping):
            player_shadow = {}
        player_status = str(player_shadow.get("status", "missing"))
        player_records = _nonnegative_int(player_shadow.get("prediction_records"))
        player_added = _nonnegative_int(player_shadow.get("records_added"))
        checks["confirmed_lineup_player_shadow"] = {
            "status": player_status,
            "expected_model_version": player_config.get("model_version"),
            "observed_model_version": player_shadow.get("model_version"),
            "expected_logical_model_sha256": player_config.get(
                "logical_model_sha256"
            ),
            "observed_logical_model_sha256": player_shadow.get(
                "logical_model_sha256"
            ),
            "expected_config_sha256": player_config.get("config_sha256"),
            "observed_config_sha256": player_shadow.get("config_sha256"),
            "prediction_records": player_records,
            "records_added": player_added,
            "champion_replacement_authorized": player_shadow.get(
                "champion_replacement_authorized"
            ),
        }
        valid_player_statuses = {"written", "no_eligible_confirmed_lineups"}
        if player_status not in valid_player_statuses:
            _add_alert(
                alerts,
                code="confirmed_lineup_player_shadow_failed",
                severity="critical",
                component="confirmed_lineup_player_shadow",
                summary=f"Confirmed-lineup player shadow status is {player_status}",
            )
        if player_status in valid_player_statuses:
            if (
                player_shadow.get("model_version")
                != player_config.get("model_version")
                or player_shadow.get("logical_model_sha256")
                != player_config.get("logical_model_sha256")
                or player_shadow.get("config_sha256")
                != player_config.get("config_sha256")
            ):
                _add_alert(
                    alerts,
                    code="confirmed_lineup_player_model_identity_mismatch",
                    severity="critical",
                    component="confirmed_lineup_player_shadow",
                    summary="Player shadow identity differs from frozen configuration",
                )
            if (
                player_records is None
                or player_added is None
                or player_added > player_records
            ):
                _add_alert(
                    alerts,
                    code="confirmed_lineup_player_receipt_invalid",
                    severity="critical",
                    component="confirmed_lineup_player_shadow",
                    summary="Player shadow returned inconsistent record counts",
                )
            if player_shadow.get("champion_replacement_authorized") is not False:
                _add_alert(
                    alerts,
                    code="confirmed_lineup_player_unsafe_activation",
                    severity="critical",
                    component="confirmed_lineup_player_shadow",
                    summary="Unvalidated player shadow attempted champion activation",
                )

    shadow_config = publication_config.get("shadow_score_grid", {})
    if not isinstance(shadow_config, Mapping) or not shadow_config.get("enabled", False):
        checks["shadow_score_grid"] = {"status": "disabled"}
        return
    shadow = result.get("shadow_score_grid")
    if not isinstance(shadow, Mapping):
        shadow = {}
    shadow_status = str(shadow.get("status", "missing"))
    expected_shadow_version = str(shadow_config.get("model_version", ""))
    expected_shadow_hash = str(shadow_config.get("logical_model_sha256", ""))
    shadow_minimum = int(shadow_config.get("minimum_prediction_rows", 1))
    shadow_rows = _nonnegative_int(shadow.get("prediction_rows"))
    checks["shadow_score_grid"] = {
        "status": shadow_status,
        "expected_model_version": expected_shadow_version,
        "observed_model_version": shadow.get("model_version"),
        "expected_logical_model_sha256": expected_shadow_hash,
        "observed_logical_model_sha256": shadow.get("logical_model_sha256"),
        "prediction_rows": shadow_rows,
        "minimum_prediction_rows": shadow_minimum,
    }
    if shadow_status != "written_to_persistent_shadow_store":
        _add_alert(
            alerts,
            code="shadow_score_grid_failed",
            severity="critical",
            component="shadow_score_grid",
            summary=f"Shadow score-grid status is {shadow_status}",
        )
    if shadow_status == "written_to_persistent_shadow_store":
        if shadow.get("model_version") != expected_shadow_version or shadow.get(
            "logical_model_sha256"
        ) != expected_shadow_hash:
            _add_alert(
                alerts,
                code="shadow_model_identity_mismatch",
                severity="critical",
                component="shadow_score_grid",
                summary="Shadow model identity differs from frozen configuration",
            )
        if shadow_rows is None or shadow_rows < shadow_minimum:
            _add_alert(
                alerts,
                code="shadow_prediction_rows_below_minimum",
                severity="critical",
                component="shadow_score_grid",
                summary="Shadow score grid contains fewer rows than configured",
            )
        parent_rows = _nonnegative_int(result.get("prediction_rows"))
        if parent_rows is not None and shadow_rows is not None and shadow_rows != parent_rows:
            _add_alert(
                alerts,
                code="shadow_parent_row_count_mismatch",
                severity="critical",
                component="shadow_score_grid",
                summary="Shadow and champion prediction-row counts differ",
            )
    else:
        shadow_error = str(shadow.get("error", ""))
        if "mismatch" in shadow_error:
            _add_alert(
                alerts,
                code="shadow_model_identity_mismatch",
                severity="critical",
                component="shadow_score_grid",
                summary="Shadow generation failed an identity check",
            )
        if "below_minimum" in shadow_error:
            _add_alert(
                alerts,
                code="shadow_prediction_rows_below_minimum",
                severity="critical",
                component="shadow_score_grid",
                summary="Shadow candidate contains fewer rows than configured",
            )
    settlement_config = shadow_config.get("settlement_ledger", {})
    if not isinstance(settlement_config, Mapping) or not settlement_config.get(
        "enabled", False
    ):
        checks["prospective_settlement_ledger"] = {"status": "disabled"}
        return
    settlement = result.get("prospective_settlement_ledger")
    if not isinstance(settlement, Mapping):
        settlement = {}
    settlement_status = str(settlement.get("status", "missing"))
    settlement_counts = {
        key: _nonnegative_int(settlement.get(key))
        for key in (
            "records_added",
            "ledger_records",
            "pending_forecasts",
            "ineligible_results",
            "reviewed_exclusions",
        )
    }
    ledger_head = settlement.get("ledger_head_sha256")
    checks["prospective_settlement_ledger"] = {
        "status": settlement_status,
        **settlement_counts,
        "ledger_head_sha256": ledger_head,
        "performance_aggregates_written": settlement.get(
            "performance_aggregates_written"
        ),
        "gate_decision_written": settlement.get("gate_decision_written"),
    }
    if settlement_status not in {"updated", "no_new_settlements"}:
        _add_alert(
            alerts,
            code="prospective_settlement_ledger_failed",
            severity="critical",
            component="prospective_settlement_ledger",
            summary=f"Prospective settlement ledger status is {settlement_status}",
        )
    if settlement_status in {"updated", "no_new_settlements"} and (
        settlement.get("performance_aggregates_written") is not False
        or settlement.get("gate_decision_written") is not False
    ):
        _add_alert(
            alerts,
            code="premature_prospective_evaluation_output",
            severity="critical",
            component="prospective_settlement_ledger",
            summary="Settlement process reported premature aggregate or gate output",
        )
    if settlement_status in {"updated", "no_new_settlements"}:
        ledger_records = settlement_counts["ledger_records"]
        invalid_receipt = (
            any(value is None for value in settlement_counts.values())
            or settlement_counts["records_added"] > ledger_records
            or (ledger_records > 0 and not _is_lowercase_sha256(ledger_head))
            or (ledger_records == 0 and ledger_head is not None)
        )
        if invalid_receipt:
            _add_alert(
                alerts,
                code="prospective_settlement_receipt_invalid",
                severity="critical",
                component="prospective_settlement_ledger",
                summary="Settlement process returned internally inconsistent counts or chain head",
            )
    evaluation_config = settlement_config.get("evaluation_program", {})
    if not isinstance(evaluation_config, Mapping) or not evaluation_config.get(
        "enabled", False
    ):
        checks["prospective_evaluation_readiness"] = {"status": "disabled"}
        return
    readiness = result.get("prospective_evaluation_readiness")
    if not isinstance(readiness, Mapping):
        readiness = {}
    readiness_status = str(readiness.get("status", "missing"))
    checks["prospective_evaluation_readiness"] = {
        "status": readiness_status,
        "expected_evaluation_config_sha256": evaluation_config.get(
            "evaluation_config_sha256"
        ),
        "observed_evaluation_config_sha256": readiness.get(
            "evaluation_config_sha256"
        ),
        "ledger_records": readiness.get("ledger_records"),
        "first_full_calendar_month": readiness.get("first_full_calendar_month"),
        "latest_matured_calendar_month": readiness.get(
            "latest_matured_calendar_month"
        ),
        "deterministic_evaluation_cutoff_month": readiness.get(
            "deterministic_evaluation_cutoff_month"
        ),
        "horizons": readiness.get("horizons"),
        "performance_statistics_exposed": readiness.get(
            "performance_statistics_exposed"
        ),
        "automatic_decision_execution": readiness.get(
            "automatic_decision_execution"
        ),
        "decision_written": readiness.get("decision_written"),
    }
    valid_readiness_statuses = {
        "locked_insufficient_evidence",
        "ready_for_explicit_one_shot_evaluation",
        "decision_already_exists",
    }
    if readiness_status not in valid_readiness_statuses:
        _add_alert(
            alerts,
            code="prospective_evaluation_readiness_failed",
            severity="critical",
            component="prospective_evaluation_readiness",
            summary=f"Prospective evaluation readiness status is {readiness_status}",
        )
    if readiness_status in valid_readiness_statuses and (
        readiness.get("performance_statistics_exposed") is not False
        or readiness.get("automatic_decision_execution") is not False
        or readiness.get("explicit_one_shot_command_required") is not True
    ):
        _add_alert(
            alerts,
            code="prospective_evaluation_readiness_unsafe",
            severity="critical",
            component="prospective_evaluation_readiness",
            summary="Readiness output violated the frozen anti-peeking policy",
        )
    if readiness_status in valid_readiness_statuses and readiness.get(
        "evaluation_config_sha256"
    ) != evaluation_config.get("evaluation_config_sha256"):
        _add_alert(
            alerts,
            code="prospective_evaluation_config_identity_mismatch",
            severity="critical",
            component="prospective_evaluation_readiness",
            summary="Readiness evaluator configuration differs from frozen collector identity",
        )
    if readiness_status in valid_readiness_statuses:
        readiness_records = _nonnegative_int(readiness.get("ledger_records"))
        if (
            readiness_records is None
            or readiness_records != settlement_counts["ledger_records"]
        ):
            _add_alert(
                alerts,
                code="prospective_evaluation_ledger_count_mismatch",
                severity="critical",
                component="prospective_evaluation_readiness",
                summary="Readiness and settlement disagree on ledger row count",
            )
    if readiness_status == "ready_for_explicit_one_shot_evaluation":
        _add_alert(
            alerts,
            code="prospective_evaluation_ready",
            severity="warning",
            component="prospective_evaluation_readiness",
            summary="Frozen evidence minimum is met; explicit one-shot evaluation is ready",
        )


def _evaluate_volume(
    *,
    alerts: list[dict[str, object]],
    checks: dict[str, object],
    data_path: Path,
    disk_usage: Callable[[Path], Any],
    warning_percent: float,
    critical_percent: float,
) -> None:
    usage = disk_usage(data_path)
    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    if total <= 0 or used < 0 or free < 0:
        raise OperationalAlertError("filesystem returned invalid volume capacity")
    used_percent = used * 100.0 / total
    checks["persistent_volume"] = {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": round(used_percent, 3),
        "warning_percent": warning_percent,
        "critical_percent": critical_percent,
    }
    if used_percent >= critical_percent:
        _add_alert(
            alerts,
            code="persistent_volume_critical",
            severity="critical",
            component="persistent_volume",
            summary="Persistent volume usage is at or above the critical threshold",
        )
    elif used_percent >= warning_percent:
        _add_alert(
            alerts,
            code="persistent_volume_warning",
            severity="warning",
            component="persistent_volume",
            summary="Persistent volume usage is at or above the warning threshold",
        )


def _write_status_and_transitions(directory: Path, status: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    current_path = directory / "current.json"
    previous = _read_json_object(current_path)
    previous_alerts = {
        str(item.get("code")): item
        for item in previous.get("alerts", [])
        if isinstance(item, dict) and item.get("code")
    }
    current_alerts = {
        str(item["code"]): item
        for item in status["alerts"]
        if isinstance(item, dict)
    }
    events: list[dict[str, object]] = []
    generated_at = status["generated_at"]
    for code, alert in current_alerts.items():
        old = previous_alerts.get(code)
        if old is None:
            events.append({"recorded_at": generated_at, "event": "opened", **alert})
        elif old.get("severity") != alert.get("severity"):
            events.append({"recorded_at": generated_at, "event": "updated", **alert})
    for code, alert in previous_alerts.items():
        if code not in current_alerts:
            events.append(
                {
                    "recorded_at": generated_at,
                    "event": "resolved",
                    "code": code,
                    "severity": alert.get("severity"),
                    "component": alert.get("component"),
                    "summary": alert.get("summary"),
                }
            )
    _atomic_json_write(current_path, status)
    if events:
        event_path = directory / "events.jsonl"
        with event_path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalAlertError("existing operational status is unreadable") from error
    if not isinstance(value, dict):
        raise OperationalAlertError("existing operational status is not an object")
    return value


def _latest_successful_as_of(path: Path) -> datetime | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OperationalAlertError("publication receipt could not be read") from error
    latest = None
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("status") != "uploaded":
            continue
        candidate = _optional_timestamp(value.get("as_of"))
        if candidate is not None and (latest is None or candidate > latest):
            latest = candidate
    return latest


def _add_alert(
    alerts: list[dict[str, object]],
    *,
    code: str,
    severity: str,
    component: str,
    summary: str,
) -> None:
    alerts.append(
        {
            "code": code,
            "severity": severity,
            "component": component,
            "summary": summary,
        }
    )


def _optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_lowercase_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise OperationalAlertError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise OperationalAlertError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise OperationalAlertError(f"{name} must be a positive integer")
    return parsed


def _percentage(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise OperationalAlertError(f"{name} must be between 0 and 100") from error
    if not 0 < parsed <= 100:
        raise OperationalAlertError(f"{name} must be between 0 and 100")
    return parsed


def _inside_root(root: Path, value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise OperationalAlertError("configured operations path must stay inside root")
    return root / path


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OperationalAlertError("watchdog time must have a timezone")
    return value.astimezone(timezone.utc)
