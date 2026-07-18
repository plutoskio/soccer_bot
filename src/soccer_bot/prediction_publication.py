from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping

from soccer_bot.modeling.production import (
    champion_model_sha256,
    load_regulation_champion,
)
from soccer_bot.modeling.player_hierarchy import (
    load_confirmed_lineup_player_model,
    player_model_sha256,
)
from soccer_bot.modeling.score_grid_shadow import (
    load_score_grid_prospective_gate,
    load_score_grid_shadow_model,
    score_grid_shadow_sha256,
)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class PredictionPublicationError(RuntimeError):
    """Raised when a candidate snapshot is unsafe to publish."""


def run_prediction_publication(
    *,
    root: Path,
    warehouse_path: Path,
    collector_config: dict,
    environment: Mapping[str, str],
    as_of: datetime,
    health_severity: str,
    command_runner: CommandRunner = subprocess.run,
) -> dict[str, object]:
    """Generate and atomically publish one guarded application snapshot.

    This function is deliberately failure-isolated from collection. It returns
    a sanitized status, writes an append-only report, and never raises after
    configuration has passed startup validation. S3 publication is invoked only
    after the local candidate passes independent structural and provenance
    checks, so a rejected candidate cannot replace the last valid object.
    """

    config = collector_config.get("prediction_publication", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}
    as_of = _utc(as_of)
    if health_severity == "blocking":
        result = {
            "status": "skipped",
            "reason": "blocking_collector_health",
            "as_of": as_of.isoformat(),
        }
        _try_write_report(root, config, result)
        return result

    try:
        result = _generate_validate_publish(
            root=root,
            warehouse_path=warehouse_path,
            config=config,
            environment=environment,
            as_of=as_of,
            command_runner=command_runner,
        )
    except Exception as error:
        result = {
            "status": "failed",
            "as_of": as_of.isoformat(),
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }
    _try_write_report(root, config, result)
    return result


def _generate_validate_publish(
    *,
    root: Path,
    warehouse_path: Path,
    config: dict,
    environment: Mapping[str, str],
    as_of: datetime,
    command_runner: CommandRunner,
) -> dict[str, object]:
    required_environment = (
        "SOCCER_SNAPSHOT_S3_BUCKET",
        "SOCCER_SNAPSHOT_S3_ENDPOINT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    missing = [key for key in required_environment if not environment.get(key)]
    if missing:
        raise PredictionPublicationError(
            "missing_required_environment:" + ",".join(sorted(missing))
        )

    model_path = _inside_root(root, config["model_path"])
    output_directory = _inside_root(root, config["output_directory"])
    expected_model_version = str(config["model_version"])
    expected_logical_hash = str(config["logical_model_sha256"])
    model = load_regulation_champion(model_path)
    if model.model_version != expected_model_version:
        raise PredictionPublicationError("model_version_mismatch")
    if champion_model_sha256(model) != expected_logical_hash:
        raise PredictionPublicationError("logical_model_hash_mismatch")

    timeout = int(config.get("timeout_seconds", 240))
    subprocess_environment = dict(os.environ)
    subprocess_environment.update(environment)
    generation = command_runner(
        [
            sys.executable,
            str(root / "scripts" / "predict_upcoming_regulation.py"),
            "--warehouse",
            str(warehouse_path),
            "--model",
            str(model_path),
            "--as-of",
            as_of.isoformat(),
            "--output-dir",
            str(output_directory),
        ],
        cwd=root,
        env=subprocess_environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if generation.returncode:
        raise PredictionPublicationError(
            f"prediction_generation_exit_{generation.returncode}"
        )

    snapshot_path = output_directory / "latest.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    _validate_candidate(
        snapshot,
        expected_as_of=as_of,
        expected_model_version=expected_model_version,
        expected_logical_hash=expected_logical_hash,
        minimum_prediction_rows=int(config.get("minimum_prediction_rows", 1)),
    )

    publication = command_runner(
        [
            sys.executable,
            str(root / "scripts" / "publish_prediction_snapshot.py"),
            "--snapshot",
            str(snapshot_path),
        ],
        cwd=root,
        env=subprocess_environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if publication.returncode:
        raise PredictionPublicationError(
            f"snapshot_publication_exit_{publication.returncode}"
        )
    try:
        publication_result = json.loads(publication.stdout)
    except json.JSONDecodeError as error:
        raise PredictionPublicationError("invalid_publication_receipt") from error
    if publication_result.get("status") != "uploaded":
        raise PredictionPublicationError("publication_not_confirmed")

    predictions = snapshot["predictions"]
    market_evidence_result = _try_capture_polymarket_market_evidence(
        root=root,
        warehouse_path=warehouse_path,
        snapshot_path=snapshot_path,
        config=config.get("polymarket_market_evidence", {}),
        command_runner=command_runner,
        environment=subprocess_environment,
        default_timeout=timeout,
        captured_at=datetime.now(timezone.utc),
    )
    shadow_result = _try_generate_shadow_score_grid(
        root=root,
        parent_snapshot_path=snapshot_path,
        parent_snapshot=snapshot,
        config=config.get("shadow_score_grid", {}),
        command_runner=command_runner,
        environment=subprocess_environment,
        timeout=timeout,
    )
    player_shadow_result = _try_generate_confirmed_lineup_player_shadow(
        root=root,
        warehouse_path=warehouse_path,
        parent_snapshot_path=snapshot_path,
        config=config.get("confirmed_lineup_player_shadow", {}),
        command_runner=command_runner,
        environment=subprocess_environment,
        timeout=timeout,
        as_of=datetime.now(timezone.utc),
    )
    settlement_result = _try_update_prospective_settlement(
        root=root,
        warehouse_path=warehouse_path,
        shadow_config=config.get("shadow_score_grid", {}),
        shadow_result=shadow_result,
        command_runner=command_runner,
        environment=subprocess_environment,
        default_timeout=timeout,
        # Settlement is an outcome-side action performed after collection.
        # Its audit timestamp must be the actual invocation time, not the
        # champion snapshot's earlier information cutoff.
        settled_at=datetime.now(timezone.utc),
    )
    readiness_result = _try_update_prospective_evaluation_readiness(
        root=root,
        shadow_config=config.get("shadow_score_grid", {}),
        settlement_result=settlement_result,
        command_runner=command_runner,
        environment=subprocess_environment,
        default_timeout=timeout,
        as_of=datetime.now(timezone.utc),
    )
    market_settlement_result = _try_update_polymarket_market_settlement(
        root=root,
        evidence_config=config.get("polymarket_market_evidence", {}),
        score_settlement_config=config.get("shadow_score_grid", {}).get(
            "settlement_ledger", {}
        ),
        market_evidence_result=market_evidence_result,
        score_settlement_result=settlement_result,
        command_runner=command_runner,
        environment=subprocess_environment,
        default_timeout=timeout,
        settled_at=datetime.now(timezone.utc),
    )
    market_readiness_result = _try_update_polymarket_market_readiness(
        root=root,
        evidence_config=config.get("polymarket_market_evidence", {}),
        market_settlement_result=market_settlement_result,
        command_runner=command_runner,
        environment=subprocess_environment,
        default_timeout=timeout,
        as_of=datetime.now(timezone.utc),
    )
    return {
        "status": "uploaded",
        "as_of": snapshot["as_of"],
        "model_version": snapshot["model_version"],
        "logical_model_sha256": snapshot["logical_model_sha256"],
        "snapshot_version": snapshot["snapshot_version"],
        "prediction_rows": len(predictions),
        "fixture_count": len({row["fixture_id"] for row in predictions}),
        "prediction_rows_sha256": snapshot["prediction_rows_sha256"],
        "polymarket_market_evidence": market_evidence_result,
        "shadow_score_grid": shadow_result,
        "confirmed_lineup_player_shadow": player_shadow_result,
        "prospective_settlement_ledger": settlement_result,
        "prospective_evaluation_readiness": readiness_result,
        "polymarket_market_settlement_ledger": market_settlement_result,
        "polymarket_market_evaluation_readiness": market_readiness_result,
    }


def _try_generate_confirmed_lineup_player_shadow(
    *,
    root: Path,
    warehouse_path: Path,
    parent_snapshot_path: Path,
    config: dict,
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    timeout: int,
    as_of: datetime,
) -> dict[str, object]:
    if not config.get("enabled", False):
        return {"status": "disabled"}
    try:
        model_path = _inside_root(root, config["model_path"])
        config_path = _inside_root(root, config["config_path"])
        output_directory = _inside_root(root, config["output_directory"])
        model = load_confirmed_lineup_player_model(model_path)
        expected_hash = str(config["logical_model_sha256"])
        expected_config_hash = str(config["config_sha256"])
        if model.model_version != str(config["model_version"]):
            raise PredictionPublicationError("player_shadow_model_version_mismatch")
        if player_model_sha256(model) != expected_hash:
            raise PredictionPublicationError("player_shadow_model_hash_mismatch")
        if _file_sha256(config_path) != expected_config_hash:
            raise PredictionPublicationError("player_shadow_config_hash_mismatch")
        execution = command_runner(
            [
                sys.executable,
                str(root / "scripts" / "predict_confirmed_lineup_player_shadow.py"),
                "--warehouse",
                str(warehouse_path),
                "--base-snapshot",
                str(parent_snapshot_path),
                "--model",
                str(model_path),
                "--config",
                str(config_path),
                "--as-of",
                _utc(as_of).isoformat(),
                "--output-dir",
                str(output_directory),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"player_shadow_generation_exit_{execution.returncode}"
            )
        try:
            receipt = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError("invalid_player_shadow_receipt") from error
        if receipt.get("status") not in {
            "written",
            "no_eligible_confirmed_lineups",
        }:
            raise PredictionPublicationError("player_shadow_write_not_confirmed")
        if receipt.get("model_version") != model.model_version:
            raise PredictionPublicationError("player_shadow_receipt_version_mismatch")
        if receipt.get("logical_model_sha256") != expected_hash:
            raise PredictionPublicationError("player_shadow_receipt_hash_mismatch")
        if receipt.get("config_sha256") != expected_config_hash:
            raise PredictionPublicationError("player_shadow_receipt_config_hash_mismatch")
        for key in ("prediction_records", "records_added"):
            if (
                isinstance(receipt.get(key), bool)
                or not isinstance(receipt.get(key), int)
                or receipt[key] < 0
            ):
                raise PredictionPublicationError("invalid_player_shadow_counts")
        if receipt["records_added"] > receipt["prediction_records"]:
            raise PredictionPublicationError("invalid_player_shadow_counts")
        return {
            "status": receipt["status"],
            "model_version": model.model_version,
            "logical_model_sha256": expected_hash,
            "config_sha256": expected_config_hash,
            "prediction_records": receipt["prediction_records"],
            "records_added": receipt["records_added"],
            "champion_replacement_authorized": False,
        }
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _try_capture_polymarket_market_evidence(
    *,
    root: Path,
    warehouse_path: Path,
    snapshot_path: Path,
    config: dict,
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    default_timeout: int,
    captured_at: datetime,
) -> dict[str, object]:
    if not config.get("enabled", False):
        return {"status": "disabled"}
    try:
        policy_path = _inside_root(root, config["policy_path"])
        output_directory = _inside_root(root, config["output_directory"])
        expected_policy_hash = str(config["policy_sha256"])
        execution = command_runner(
            [
                sys.executable,
                str(root / "scripts" / "capture_polymarket_market_evidence.py"),
                "--warehouse",
                str(warehouse_path),
                "--snapshot",
                str(snapshot_path),
                "--policy",
                str(policy_path),
                "--expected-policy-sha256",
                expected_policy_hash,
                "--output-dir",
                str(output_directory),
                "--captured-at",
                captured_at.isoformat(),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", default_timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"polymarket_market_evidence_exit_{execution.returncode}"
            )
        try:
            result = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError(
                "invalid_polymarket_market_evidence_receipt"
            ) from error
        if result.get("status") not in {"updated", "no_new_evidence"}:
            raise PredictionPublicationError(
                "polymarket_market_evidence_not_confirmed"
            )
        if result.get("policy_sha256") != expected_policy_hash:
            raise PredictionPublicationError(
                "polymarket_market_evidence_policy_mismatch"
            )
        counts = {
            key: _validated_nonnegative_int(result.get(key), key)
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
        if (
            counts["new_evidence_records"] + counts["existing_evidence_records"]
            != counts["evidence_records"]
            or counts["evidence_records"] > counts["prediction_rows"]
            or counts["economically_executable_records"]
            > counts["evidence_records"]
            or counts["new_coverage_universe_records"]
            + counts["existing_coverage_universe_records"]
            != counts["coverage_universe_records"]
            or counts["coverage_universe_records"] != counts["prediction_rows"]
        ):
            raise PredictionPublicationError(
                "invalid_polymarket_market_evidence_counts"
            )
        _validate_polymarket_evidence_horizons(result.get("horizons"), counts)
        if (
            result.get("outcome_or_performance_fields_written") is not False
            or result.get("orders_or_trading_actions_performed") is not False
        ):
            raise PredictionPublicationError(
                "unsafe_polymarket_market_evidence_receipt"
            )
        return {
            "status": result["status"],
            "policy_version": result.get("policy_version"),
            "mapping_version": result.get("mapping_version"),
            "policy_sha256": result.get("policy_sha256"),
            **counts,
            "horizons": result.get("horizons"),
            "exclusion_counts": result.get("exclusion_counts"),
            "outcome_or_performance_fields_written": False,
            "orders_or_trading_actions_performed": False,
        }
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _validate_polymarket_evidence_horizons(
    value: object, counts: Mapping[str, int]
) -> None:
    if not isinstance(value, dict):
        raise PredictionPublicationError("polymarket_evidence_horizons_not_object")
    totals = [0, 0, 0]
    keys = (
        "prediction_rows",
        "complete_moneyline_mappings",
        "pre_cutoff_complete_books",
        "valid_bid_ask_books",
        "evidence_records",
        "economically_executable_records",
    )
    for bucket in value.values():
        if not isinstance(bucket, dict):
            raise PredictionPublicationError("polymarket_evidence_horizon_not_object")
        funnel = [_validated_nonnegative_int(bucket.get(key), key) for key in keys]
        if any(left < right for left, right in zip(funnel, funnel[1:])):
            raise PredictionPublicationError("polymarket_evidence_horizon_funnel_invalid")
        totals[0] += funnel[0]
        totals[1] += funnel[4]
        totals[2] += funnel[5]
    if totals != [
        counts["prediction_rows"],
        counts["evidence_records"],
        counts["economically_executable_records"],
    ]:
        raise PredictionPublicationError("polymarket_evidence_horizon_totals_invalid")


def _try_generate_shadow_score_grid(
    *,
    root: Path,
    parent_snapshot_path: Path,
    parent_snapshot: dict,
    config: dict,
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    timeout: int,
) -> dict[str, object]:
    if not config.get("enabled", False):
        return {"status": "disabled"}
    try:
        model_path = _inside_root(root, config["model_path"])
        gate_path = _inside_root(root, config["prospective_gate_path"])
        output_directory = _inside_root(root, config["output_directory"])
        model = load_score_grid_shadow_model(model_path)
        expected_hash = str(config["logical_model_sha256"])
        if model.model_version != str(config["model_version"]):
            raise PredictionPublicationError("shadow_model_version_mismatch")
        if score_grid_shadow_sha256(model) != expected_hash:
            raise PredictionPublicationError("shadow_logical_model_hash_mismatch")
        gate = load_score_grid_prospective_gate(gate_path, model=model)
        generation = command_runner(
            [
                sys.executable,
                str(root / "scripts" / "predict_score_grid_v3_shadow.py"),
                "--parent-snapshot",
                str(parent_snapshot_path),
                "--model",
                str(model_path),
                "--prospective-gate",
                str(gate_path),
                "--output-dir",
                str(output_directory),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if generation.returncode:
            raise PredictionPublicationError(
                f"shadow_generation_exit_{generation.returncode}"
            )
        shadow_path = output_directory / "latest.json"
        shadow = json.loads(shadow_path.read_text(encoding="utf-8"))
        _validate_shadow_candidate(
            shadow,
            parent_snapshot=parent_snapshot,
            expected_model_version=model.model_version,
            expected_model_hash=expected_hash,
            expected_gate_version=str(gate["gate_version"]),
            minimum_prediction_rows=int(config.get("minimum_prediction_rows", 1)),
        )
        rows = shadow["predictions"]
        return {
            "status": "written_to_persistent_shadow_store",
            "model_version": shadow["model_version"],
            "logical_model_sha256": shadow["logical_model_sha256"],
            "prospective_gate_version": shadow["prospective_gate_version"],
            "prediction_rows": len(rows),
            "fixture_count": len({row["fixture_id"] for row in rows}),
        }
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _try_update_prospective_settlement(
    *,
    root: Path,
    warehouse_path: Path,
    shadow_config: dict,
    shadow_result: Mapping[str, object],
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    default_timeout: int,
    settled_at: datetime,
) -> dict[str, object]:
    config = shadow_config.get("settlement_ledger", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}
    if shadow_result.get("status") != "written_to_persistent_shadow_store":
        return {"status": "skipped", "reason": "shadow_score_grid_not_written"}
    try:
        evidence_directory = _inside_root(
            root, Path(shadow_config["output_directory"]) / "evidence"
        )
        model_path = _inside_root(root, shadow_config["model_path"])
        gate_path = _inside_root(root, shadow_config["prospective_gate_path"])
        settlement_config = _inside_root(root, config["config_path"])
        output_directory = _inside_root(root, config["output_directory"])
        execution = command_runner(
            [
                sys.executable,
                str(root / "scripts" / "settle_score_grid_v3_prospective.py"),
                "--warehouse",
                str(warehouse_path),
                "--evidence-dir",
                str(evidence_directory),
                "--model",
                str(model_path),
                "--prospective-gate",
                str(gate_path),
                "--settlement-config",
                str(settlement_config),
                "--output-dir",
                str(output_directory),
                "--settled-at",
                settled_at.isoformat(),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", default_timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"prospective_settlement_exit_{execution.returncode}"
            )
        try:
            result = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError(
                "invalid_prospective_settlement_receipt"
            ) from error
        if result.get("status") not in {"updated", "no_new_settlements"}:
            raise PredictionPublicationError(
                "prospective_settlement_not_confirmed"
            )
        counts = {
            key: _validated_nonnegative_int(result.get(key), key)
            for key in (
                "records_added",
                "ledger_records",
                "pending_forecasts",
                "ineligible_results",
                "reviewed_exclusions",
            )
        }
        if counts["records_added"] > counts["ledger_records"]:
            raise PredictionPublicationError("invalid_prospective_settlement_counts")
        ledger_head = result.get("ledger_head_sha256")
        if counts["ledger_records"]:
            if not _is_lowercase_sha256(ledger_head):
                raise PredictionPublicationError("invalid_prospective_ledger_head")
        elif ledger_head is not None:
            raise PredictionPublicationError("unexpected_prospective_ledger_head")
        return {
            "status": result["status"],
            **counts,
            "ledger_head_sha256": ledger_head,
            "performance_aggregates_written": result.get(
                "performance_aggregates_written"
            ),
            "gate_decision_written": result.get("gate_decision_written"),
        }
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _validated_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PredictionPublicationError(f"invalid_prospective_{label}")
    return value


def _try_update_polymarket_market_settlement(
    *,
    root: Path,
    evidence_config: dict,
    score_settlement_config: dict,
    market_evidence_result: Mapping[str, object],
    score_settlement_result: Mapping[str, object],
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    default_timeout: int,
    settled_at: datetime,
) -> dict[str, object]:
    config = evidence_config.get("market_research", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}
    if market_evidence_result.get("status") not in {"updated", "no_new_evidence"}:
        return {"status": "skipped", "reason": "market_evidence_not_verified"}
    if score_settlement_result.get("status") not in {"updated", "no_new_settlements"}:
        return {"status": "skipped", "reason": "score_settlement_not_verified"}
    try:
        evidence_output = _inside_root(root, evidence_config["output_directory"])
        score_output = _inside_root(root, score_settlement_config["output_directory"])
        score_config_path = _inside_root(root, score_settlement_config["config_path"])
        policy_path = _inside_root(root, evidence_config["policy_path"])
        settlement_config_path = _inside_root(root, config["settlement_config_path"])
        expected_config_hash = str(config["settlement_config_sha256"])
        if _file_sha256(settlement_config_path) != expected_config_hash:
            raise PredictionPublicationError(
                "polymarket_market_settlement_config_hash_mismatch"
            )
        output_directory = _inside_root(root, config["output_directory"])
        execution = command_runner(
            [
                sys.executable,
                str(root / "scripts" / "settle_polymarket_regulation_research.py"),
                "--coverage-universe-dir",
                str(evidence_output / "coverage_universe"),
                "--evidence-dir",
                str(evidence_output / "evidence"),
                "--score-settlement-ledger",
                str(score_output / "ledger.jsonl"),
                "--score-settlement-config",
                str(score_config_path),
                "--market-policy",
                str(policy_path),
                "--settlement-config",
                str(settlement_config_path),
                "--output-dir",
                str(output_directory),
                "--settled-at",
                settled_at.isoformat(),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", default_timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"polymarket_market_settlement_exit_{execution.returncode}"
            )
        try:
            result = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError(
                "invalid_polymarket_market_settlement_receipt"
            ) from error
        if result.get("status") not in {"updated", "no_new_settlements"}:
            raise PredictionPublicationError(
                "polymarket_market_settlement_not_confirmed"
            )
        counts = {
            key: _validated_nonnegative_int(result.get(key), key)
            for key in (
                "records_added",
                "ledger_records",
                "covered_market_records",
                "economically_executable_records",
                "pending_coverage_records",
                "skipped_ineligible_records",
            )
        }
        if (
            counts["records_added"] > counts["ledger_records"]
            or counts["covered_market_records"] > counts["ledger_records"]
            or counts["economically_executable_records"]
            > counts["covered_market_records"]
        ):
            raise PredictionPublicationError(
                "invalid_polymarket_market_settlement_counts"
            )
        head = result.get("ledger_head_sha256")
        if (counts["ledger_records"] and not _is_lowercase_sha256(head)) or (
            not counts["ledger_records"] and head is not None
        ):
            raise PredictionPublicationError(
                "invalid_polymarket_market_settlement_head"
            )
        if (
            result.get("aggregate_performance_written") is not False
            or result.get("evaluation_report_written") is not False
            or result.get("orders_or_trading_actions_performed") is not False
        ):
            raise PredictionPublicationError(
                "unsafe_polymarket_market_settlement_receipt"
            )
        return {
            "status": result["status"],
            "settlement_config_sha256": expected_config_hash,
            **counts,
            "ledger_head_sha256": head,
            "aggregate_performance_written": False,
            "evaluation_report_written": False,
            "orders_or_trading_actions_performed": False,
        }
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _try_update_polymarket_market_readiness(
    *,
    root: Path,
    evidence_config: dict,
    market_settlement_result: Mapping[str, object],
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    default_timeout: int,
    as_of: datetime,
) -> dict[str, object]:
    settlement = evidence_config.get("market_research", {})
    config = settlement.get("evaluation_program", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}
    if market_settlement_result.get("status") not in {
        "updated",
        "no_new_settlements",
    }:
        return {"status": "skipped", "reason": "market_settlement_not_verified"}
    try:
        settlement_output = _inside_root(root, settlement["output_directory"])
        settlement_config_path = _inside_root(
            root, settlement["settlement_config_path"]
        )
        evaluation_config_path = _inside_root(root, config["config_path"])
        expected_hash = str(config["evaluation_config_sha256"])
        if _file_sha256(evaluation_config_path) != expected_hash:
            raise PredictionPublicationError(
                "polymarket_market_evaluation_config_hash_mismatch"
            )
        output_directory = _inside_root(root, config["output_directory"])
        execution = command_runner(
            [
                sys.executable,
                str(
                    root
                    / "scripts"
                    / "check_polymarket_regulation_evaluation_readiness.py"
                ),
                "--ledger",
                str(settlement_output / "ledger.jsonl"),
                "--settlement-config",
                str(settlement_config_path),
                "--evaluation-config",
                str(evaluation_config_path),
                "--output-dir",
                str(output_directory),
                "--as-of",
                as_of.isoformat(),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", default_timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"polymarket_market_readiness_exit_{execution.returncode}"
            )
        try:
            result = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError(
                "invalid_polymarket_market_readiness_receipt"
            ) from error
        if result.get("status") not in {
            "locked_insufficient_evidence",
            "ready_for_explicit_one_shot_evaluation",
            "report_already_exists",
        }:
            raise PredictionPublicationError(
                "polymarket_market_readiness_status_invalid"
            )
        if (
            result.get("performance_statistics_exposed") is not False
            or result.get("automatic_evaluation_execution") is not False
            or result.get("explicit_one_shot_command_required") is not True
            or result.get("evaluation_config_sha256") != expected_hash
            or _validated_nonnegative_int(result.get("ledger_records"), "ledger_records")
            != int(market_settlement_result["ledger_records"])
        ):
            raise PredictionPublicationError(
                "polymarket_market_readiness_anti_peeking_or_identity_failed"
            )
        return result
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _try_update_prospective_evaluation_readiness(
    *,
    root: Path,
    shadow_config: dict,
    settlement_result: Mapping[str, object],
    command_runner: CommandRunner,
    environment: Mapping[str, str],
    default_timeout: int,
    as_of: datetime,
) -> dict[str, object]:
    settlement_config = shadow_config.get("settlement_ledger", {})
    config = settlement_config.get("evaluation_program", {})
    if not config.get("enabled", False):
        return {"status": "disabled"}
    if settlement_result.get("status") not in {"updated", "no_new_settlements"}:
        return {"status": "skipped", "reason": "settlement_ledger_not_verified"}
    try:
        settlement_output = _inside_root(
            root, settlement_config["output_directory"]
        )
        model_path = _inside_root(root, shadow_config["model_path"])
        gate_path = _inside_root(root, shadow_config["prospective_gate_path"])
        settlement_config_path = _inside_root(root, settlement_config["config_path"])
        evaluation_config_path = _inside_root(root, config["config_path"])
        output_directory = _inside_root(root, config["output_directory"])
        execution = command_runner(
            [
                sys.executable,
                str(root / "scripts/check_score_grid_v3_evaluation_readiness.py"),
                "--ledger",
                str(settlement_output / "ledger.jsonl"),
                "--model",
                str(model_path),
                "--prospective-gate",
                str(gate_path),
                "--settlement-config",
                str(settlement_config_path),
                "--evaluation-config",
                str(evaluation_config_path),
                "--output-dir",
                str(output_directory),
                "--as-of",
                as_of.isoformat(),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=int(config.get("timeout_seconds", default_timeout)),
            check=False,
        )
        if execution.returncode:
            raise PredictionPublicationError(
                f"prospective_readiness_exit_{execution.returncode}"
            )
        try:
            result = json.loads(execution.stdout)
        except json.JSONDecodeError as error:
            raise PredictionPublicationError(
                "invalid_prospective_readiness_receipt"
            ) from error
        _validate_readiness_receipt(
            result,
            expected_ledger_records=int(settlement_result["ledger_records"]),
            expected_evaluation_config_sha256=str(
                config["evaluation_config_sha256"]
            ),
        )
        return result
    except Exception as error:
        return {
            "status": "failed",
            "error_type": type(error).__name__,
            "error": _safe_error(error),
        }


def _validate_readiness_receipt(
    value: object,
    *,
    expected_ledger_records: int,
    expected_evaluation_config_sha256: str,
) -> None:
    if not isinstance(value, dict):
        raise PredictionPublicationError("prospective_readiness_not_object")
    if value.get("status") not in {
        "locked_insufficient_evidence",
        "ready_for_explicit_one_shot_evaluation",
        "decision_already_exists",
    }:
        raise PredictionPublicationError("prospective_readiness_status_invalid")
    if (
        value.get("performance_statistics_exposed") is not False
        or value.get("automatic_decision_execution") is not False
        or value.get("explicit_one_shot_command_required") is not True
    ):
        raise PredictionPublicationError("prospective_readiness_anti_peeking_failed")
    if value.get("evaluation_config_sha256") != expected_evaluation_config_sha256:
        raise PredictionPublicationError("prospective_readiness_config_hash_mismatch")
    if _validated_nonnegative_int(value.get("ledger_records"), "ledger_records") != (
        expected_ledger_records
    ):
        raise PredictionPublicationError("prospective_readiness_ledger_count_mismatch")
    horizons = value.get("horizons")
    if not isinstance(horizons, dict) or set(horizons) != {
        "pre_lineup_24h_v1",
        "pre_lineup_72h_clean_v1",
    }:
        raise PredictionPublicationError("prospective_readiness_horizons_invalid")
    for counts in horizons.values():
        if not isinstance(counts, dict):
            raise PredictionPublicationError("prospective_readiness_counts_invalid")
        for key in (
            "eligible_settled_fixtures",
            "nonempty_mature_calendar_month_blocks",
            "competitions",
        ):
            _validated_nonnegative_int(counts.get(key), key)
    forbidden = (
        "log_loss",
        "brier",
        "rps",
        "mean_delta",
        "confidence",
        "bootstrap_interval",
        "candidate_minus_baseline",
    )
    serialized = json.dumps(value, sort_keys=True).lower()
    if any(term in serialized for term in forbidden):
        raise PredictionPublicationError("prospective_readiness_exposed_performance")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_lowercase_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_shadow_candidate(
    snapshot: object,
    *,
    parent_snapshot: dict,
    expected_model_version: str,
    expected_model_hash: str,
    expected_gate_version: str,
    minimum_prediction_rows: int,
) -> None:
    if not isinstance(snapshot, dict):
        raise PredictionPublicationError("shadow_snapshot_not_object")
    if snapshot.get("snapshot_version") != (
        "regulation_score_grid_v3_shadow_snapshot_v1"
    ):
        raise PredictionPublicationError("unexpected_shadow_snapshot_version")
    if snapshot.get("model_version") != expected_model_version:
        raise PredictionPublicationError("shadow_snapshot_model_version_mismatch")
    if snapshot.get("logical_model_sha256") != expected_model_hash:
        raise PredictionPublicationError("shadow_snapshot_model_hash_mismatch")
    if snapshot.get("prospective_gate_version") != expected_gate_version:
        raise PredictionPublicationError("shadow_snapshot_gate_version_mismatch")
    if snapshot.get("parent_model_version") != parent_snapshot.get("model_version"):
        raise PredictionPublicationError("shadow_snapshot_parent_version_mismatch")
    if _parse_timestamp(snapshot.get("as_of"), "shadow_as_of") != _parse_timestamp(
        parent_snapshot.get("as_of"), "parent_as_of"
    ):
        raise PredictionPublicationError("shadow_snapshot_as_of_mismatch")
    created_at = _parse_timestamp(snapshot.get("created_at"), "shadow_created_at")
    predictions = snapshot.get("predictions")
    if not isinstance(predictions, list) or len(predictions) < minimum_prediction_rows:
        raise PredictionPublicationError("shadow_snapshot_below_minimum_rows")
    keys = set()
    maximum_moneyline_difference = 0.0
    for row in predictions:
        if not isinstance(row, dict):
            raise PredictionPublicationError("shadow_prediction_not_object")
        key = (str(row.get("fixture_id", "")), str(row.get("information_state", "")))
        if not all(key) or key in keys:
            raise PredictionPublicationError("invalid_or_duplicate_shadow_key")
        keys.add(key)
        if _parse_timestamp(row.get("kickoff"), "shadow_kickoff") <= created_at:
            raise PredictionPublicationError("shadow_prediction_not_before_kickoff")
        parent_moneyline = row.get("parent_moneyline")
        implied_moneyline = row.get("implied_moneyline")
        if not isinstance(parent_moneyline, dict) or not isinstance(
            implied_moneyline, dict
        ):
            raise PredictionPublicationError("shadow_moneyline_not_object")
        for outcome in ("home_win", "draw", "away_win"):
            try:
                difference = abs(
                    float(parent_moneyline[outcome])
                    - float(implied_moneyline[outcome])
                )
            except (KeyError, TypeError, ValueError) as error:
                raise PredictionPublicationError(
                    "invalid_shadow_moneyline"
                ) from error
            maximum_moneyline_difference = max(
                maximum_moneyline_difference, difference
            )
        grid = row.get("score_grid")
        if not isinstance(grid, list) or not grid:
            raise PredictionPublicationError("shadow_score_grid_missing")
        parsed_cells = []
        try:
            for cell in grid:
                home = cell["home_goals"]
                away = cell["away_goals"]
                if (
                    isinstance(home, bool)
                    or not isinstance(home, int)
                    or isinstance(away, bool)
                    or not isinstance(away, int)
                    or home < 0
                    or away < 0
                ):
                    raise PredictionPublicationError("invalid_shadow_score")
                parsed_cells.append((home, away, float(cell["probability"])))
        except (KeyError, TypeError, ValueError) as error:
            raise PredictionPublicationError("invalid_shadow_score_cell") from error
        probabilities = [probability for _, _, probability in parsed_cells]
        if any(not math.isfinite(value) or value <= 0 for value in probabilities):
            raise PredictionPublicationError("invalid_shadow_score_probability")
        if not math.isclose(math.fsum(probabilities), 1.0, abs_tol=1e-10):
            raise PredictionPublicationError("shadow_score_grid_not_normalized")
        grid_moneyline = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        for home, away, probability in parsed_cells:
            outcome = (
                "home_win" if home > away else "draw" if home == away else "away_win"
            )
            grid_moneyline[outcome] += probability
        if any(
            abs(grid_moneyline[outcome] - float(implied_moneyline[outcome])) > 1e-10
            for outcome in grid_moneyline
        ):
            raise PredictionPublicationError(
                "shadow_grid_and_implied_moneyline_differ"
            )
    if maximum_moneyline_difference > 1e-10:
        raise PredictionPublicationError("shadow_moneyline_invariant_failed")


def _validate_candidate(
    snapshot: object,
    *,
    expected_as_of: datetime,
    expected_model_version: str,
    expected_logical_hash: str,
    minimum_prediction_rows: int,
) -> None:
    if not isinstance(snapshot, dict):
        raise PredictionPublicationError("snapshot_not_object")
    if snapshot.get("snapshot_version") != "upcoming_regulation_moneyline_snapshot_v2":
        raise PredictionPublicationError("unexpected_snapshot_version")
    if snapshot.get("model_version") != expected_model_version:
        raise PredictionPublicationError("snapshot_model_version_mismatch")
    if snapshot.get("logical_model_sha256") != expected_logical_hash:
        raise PredictionPublicationError("snapshot_logical_hash_mismatch")
    parsed_as_of = _parse_timestamp(snapshot.get("as_of"), "snapshot_as_of")
    if parsed_as_of != expected_as_of:
        raise PredictionPublicationError("snapshot_as_of_mismatch")
    predictions = snapshot.get("predictions")
    if not isinstance(predictions, list):
        raise PredictionPublicationError("snapshot_predictions_not_list")
    if len(predictions) < minimum_prediction_rows:
        raise PredictionPublicationError("snapshot_below_minimum_prediction_rows")
    keys: set[tuple[str, str]] = set()
    for row in predictions:
        if not isinstance(row, dict):
            raise PredictionPublicationError("snapshot_prediction_not_object")
        key = (str(row.get("fixture_id", "")), str(row.get("information_state", "")))
        if not all(key) or key in keys:
            raise PredictionPublicationError("invalid_or_duplicate_prediction_key")
        keys.add(key)
        if _parse_timestamp(row.get("kickoff"), "kickoff") <= parsed_as_of:
            raise PredictionPublicationError("snapshot_contains_started_fixture")
        if _parse_timestamp(row.get("prediction_at"), "prediction_at") > parsed_as_of:
            raise PredictionPublicationError("snapshot_contains_future_horizon")
    if not isinstance(snapshot.get("prediction_rows_sha256"), str) or len(
        snapshot["prediction_rows_sha256"]
    ) != 64:
        raise PredictionPublicationError("invalid_prediction_rows_hash")


def _write_report(root: Path, config: dict, result: dict[str, object]) -> None:
    report_directory = _inside_root(
        root, config.get("report_directory", "data/reports/predictions")
    )
    report_directory.mkdir(parents=True, exist_ok=True)
    path = report_directory / "publication.jsonl"
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _try_write_report(root: Path, config: dict, result: dict[str, object]) -> None:
    """Best-effort audit receipt without allowing reporting I/O to stop collection."""

    try:
        _write_report(root, config, result)
    except OSError:
        result["report_status"] = "failed"


def _inside_root(root: Path, value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise PredictionPublicationError("configured_path_must_stay_inside_root")
    return root / path


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise PredictionPublicationError(f"{field}_must_be_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PredictionPublicationError(f"{field}_must_be_timestamp") from error
    if parsed.tzinfo is None:
        raise PredictionPublicationError(f"{field}_must_have_timezone")
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PredictionPublicationError("as_of_must_have_timezone")
    return value.astimezone(timezone.utc)


def _safe_error(error: Exception) -> str:
    if isinstance(error, PredictionPublicationError):
        return str(error)[:240]
    return "unexpected_publication_failure"
