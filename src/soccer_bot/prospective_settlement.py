from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import duckdb

from soccer_bot.contracts import ScoreGrid, load_contract_registry
from soccer_bot.datasets.targets import load_regulation_target_exclusions
from soccer_bot.modeling.score_grid_shadow import (
    load_score_grid_prospective_gate,
    load_score_grid_shadow_model,
    predict_coherent_score_grid,
    predict_parent_preserving_poisson_grid,
    score_grid_shadow_sha256,
)
from soccer_bot.prospective_evidence import load_forecast_evidence


class ProspectiveSettlementError(RuntimeError):
    """Raised when a prospective result cannot be joined without ambiguity."""


def update_prospective_settlement_ledger(
    *,
    root: Path,
    warehouse_path: Path,
    evidence_directory: Path,
    model_path: Path,
    gate_path: Path,
    settlement_config_path: Path,
    output_directory: Path,
    settled_at: datetime,
) -> dict[str, object]:
    settled_at = _utc(settled_at)
    config = _read_object(settlement_config_path)
    model = load_score_grid_shadow_model(model_path)
    gate = load_score_grid_prospective_gate(gate_path, model=model)
    _validate_config(config, model=model, gate=gate)
    contract_path = _inside_root(root, config["contract_registry"])
    exclusion_path = _inside_root(root, config["reviewed_result_exclusions"])
    _validate_frozen_artifacts(
        config,
        contract_path=contract_path,
        exclusion_path=exclusion_path,
        gate_path=gate_path,
        model_path=model_path,
    )
    load_contract_registry(contract_path)
    exclusions = load_regulation_target_exclusions(exclusion_path)
    evidence = load_forecast_evidence(evidence_directory)
    _validate_evidence_set(evidence, model=model, gate=gate)

    output_directory.mkdir(parents=True, exist_ok=True)
    ledger_path = output_directory / "ledger.jsonl"
    existing, head_hash = _read_ledger(ledger_path, config=config)
    existing_pairs = {
        (str(record["fixture_id"]), str(record["information_state"])): str(
            record["evidence_key"]
        )
        for record in existing
    }
    for item in evidence:
        pair = (str(item["fixture_id"]), str(item["information_state"]))
        if pair in existing_pairs and existing_pairs[pair] != item["evidence_key"]:
            raise ProspectiveSettlementError(
                "settled pairing key has different forecast evidence"
            )
    unsettled = [
        item
        for item in evidence
        if (str(item["fixture_id"]), str(item["information_state"]))
        not in existing_pairs
    ]
    outcomes = _load_outcomes(warehouse_path, {str(item["fixture_id"]) for item in unsettled})

    pending = 0
    ineligible = 0
    reviewed_excluded = 0
    new_records = []
    for item in sorted(
        unsettled,
        key=lambda value: (
            _timestamp(value["prediction"]["kickoff"]),
            str(value["fixture_id"]),
            str(value["information_state"]),
        ),
    ):
        outcome = outcomes.get(str(item["fixture_id"]))
        if outcome is None:
            raise ProspectiveSettlementError(
                f"forecast fixture is missing from eligibility view: {item['fixture_id']}"
            )
        if not outcome["valid_final_observations"]:
            if outcome["final_observation_count"]:
                raise ProspectiveSettlementError(
                    f"final result has invalid regulation score: {item['fixture_id']}"
                )
            if outcome["eligible_result_models"] is not True:
                ineligible += 1
                continue
            pending += 1
            continue
        scores = {
            (row["home_goals"], row["away_goals"])
            for row in outcome["valid_final_observations"]
        }
        fixture_id = str(item["fixture_id"])
        exclusion = exclusions.get(fixture_id)
        if exclusion is not None:
            if scores != set(exclusion.observed_scores) or len(scores) < 2:
                raise ProspectiveSettlementError(
                    f"reviewed exclusion changed for fixture {fixture_id}"
                )
            reviewed_excluded += 1
            continue
        if len(scores) != 1:
            raise ProspectiveSettlementError(
                f"conflicting final regulation scores for fixture {fixture_id}"
            )
        if outcome["eligible_result_models"] is not True:
            ineligible += 1
            continue
        home_goals, away_goals = next(iter(scores))
        record = _settlement_record(
            evidence=item,
            outcome=outcome,
            home_goals=home_goals,
            away_goals=away_goals,
            config=config,
            model=model,
            gate=gate,
            contract_registry_sha256=_file_sha256(contract_path),
            reviewed_exclusions_sha256=_file_sha256(exclusion_path),
            model_artifact_sha256=_file_sha256(model_path),
            settlement_config_sha256=_file_sha256(settlement_config_path),
            gate_file_sha256=_file_sha256(gate_path),
            settled_at=settled_at,
        )
        record["previous_record_sha256"] = head_hash
        record["record_sha256"] = _logical_sha256(record)
        head_hash = str(record["record_sha256"])
        new_records.append(record)

    if new_records:
        _atomic_append_jsonl(ledger_path, new_records)
    all_records, verified_head = _read_ledger(ledger_path, config=config)
    if verified_head != head_hash:
        raise ProspectiveSettlementError("ledger head hash changed after append")
    manifest = {
        "manifest_version": "regulation_score_grid_v3_settlement_manifest_v1",
        "generated_at": settled_at.isoformat(),
        "ledger_version": config["ledger_version"],
        "model_version": model.model_version,
        "logical_model_sha256": score_grid_shadow_sha256(model),
        "prospective_gate_version": gate["gate_version"],
        "settlement_config_sha256": _file_sha256(settlement_config_path),
        "ledger_records": len(all_records),
        "ledger_head_sha256": verified_head,
        "ledger_file_sha256": _file_sha256(ledger_path) if ledger_path.exists() else None,
        "performance_aggregates_written": False,
        "gate_decision_written": False,
    }
    _atomic_json_write(output_directory / "manifest.json", manifest)
    return {
        "status": "updated" if new_records else "no_new_settlements",
        "records_added": len(new_records),
        "ledger_records": len(all_records),
        "pending_forecasts": pending,
        "ineligible_results": ineligible,
        "reviewed_exclusions": reviewed_excluded,
        "ledger_head_sha256": verified_head,
        "performance_aggregates_written": False,
        "gate_decision_written": False,
    }


def _settlement_record(
    *,
    evidence: Mapping[str, object],
    outcome: Mapping[str, object],
    home_goals: int,
    away_goals: int,
    config: Mapping[str, object],
    model,
    gate: Mapping[str, object],
    contract_registry_sha256: str,
    reviewed_exclusions_sha256: str,
    model_artifact_sha256: str,
    settlement_config_sha256: str,
    gate_file_sha256: str,
    settled_at: datetime,
) -> dict[str, object]:
    prediction = evidence["prediction"]
    if not isinstance(prediction, Mapping):
        raise ProspectiveSettlementError("forecast prediction is not an object")
    stored_grid = _parse_grid(prediction.get("score_grid"))
    stored_hash = _grid_sha256(stored_grid.probabilities)
    if stored_hash != prediction.get("score_grid_sha256"):
        raise ProspectiveSettlementError("stored score-grid hash mismatch")
    parent_moneyline = prediction.get("parent_moneyline")
    if not isinstance(parent_moneyline, Mapping):
        raise ProspectiveSettlementError("parent moneyline is not an object")
    candidate = predict_coherent_score_grid(
        expected_home_goals=float(prediction["expected_home_goals"]),
        expected_away_goals=float(prediction["expected_away_goals"]),
        parent_moneyline=parent_moneyline,
        information_state=str(evidence["information_state"]),
        model=model,
    )
    if _grid_sha256(candidate.probabilities) != stored_hash:
        raise ProspectiveSettlementError("recomputed shadow grid differs from evidence")
    baseline = predict_parent_preserving_poisson_grid(
        expected_home_goals=float(prediction["expected_home_goals"]),
        expected_away_goals=float(prediction["expected_away_goals"]),
        parent_moneyline=parent_moneyline,
        information_state=str(evidence["information_state"]),
        model=model,
    )
    floor = float(config["probability_floor"])
    candidate_metrics = _score_metrics(candidate, home_goals, away_goals, floor)
    baseline_metrics = _score_metrics(baseline, home_goals, away_goals, floor)
    for metrics, grid in (
        (candidate_metrics, candidate),
        (baseline_metrics, baseline),
    ):
        grid_moneyline = grid.moneyline()
        metrics["maximum_absolute_parent_moneyline_difference"] = max(
            abs(grid_moneyline[key] - float(parent_moneyline[key]))
            for key in ("home_win", "draw", "away_win")
        )
    deltas = {
        key: candidate_metrics[key] - baseline_metrics[key]
        for key in candidate_metrics
        if key.endswith(("_log_loss", "_brier", "_rps"))
    }
    kickoff = _timestamp(prediction["kickoff"])
    prediction_at = _timestamp(prediction["prediction_at"])
    snapshot_as_of = _timestamp(evidence["first_snapshot_as_of"])
    snapshot_created_at = _timestamp(evidence["first_snapshot_created_at"])
    current_kickoff = _utc(outcome["scheduled_kickoff"])
    result_rows = outcome["valid_final_observations"]
    earliest_result_retrieval = min(_utc(row["retrieved_at"]) for row in result_rows)
    latest_result_retrieval = max(_utc(row["retrieved_at"]) for row in result_rows)
    checks = {
        "eligible_result_models": outcome["eligible_result_models"] is True,
        "kickoff_at_or_after_prospective_holdout": kickoff
        >= model.prospective_holdout_start,
        "current_kickoff_matches_prediction": current_kickoff == kickoff,
        "prediction_at_or_before_first_snapshot_as_of": prediction_at <= snapshot_as_of,
        "snapshot_creation_at_or_after_snapshot_as_of": snapshot_created_at
        >= snapshot_as_of,
        "prediction_horizon_matches_information_state": _horizon_matches(
            str(evidence["information_state"]), prediction_at, kickoff
        ),
        "first_snapshot_as_of_before_kickoff": snapshot_as_of < kickoff,
        "first_snapshot_created_before_kickoff": snapshot_created_at < kickoff,
        "result_retrieved_after_forecast_creation": earliest_result_retrieval
        > snapshot_created_at,
        "result_retrieved_after_kickoff": earliest_result_retrieval > kickoff,
        "settlement_run_at_or_after_all_result_retrievals": settled_at
        >= latest_result_retrieval,
        "shadow_model_identity_matches": evidence["model_version"]
        == model.model_version
        and evidence["logical_model_sha256"] == score_grid_shadow_sha256(model),
        "prospective_gate_identity_matches": evidence["prospective_gate_version"]
        == gate["gate_version"],
        "stored_score_grid_hash_matches": True,
        "recomputed_shadow_grid_matches": True,
    }
    return {
        "ledger_version": config["ledger_version"],
        "evidence_key": evidence["evidence_key"],
        "fixture_id": evidence["fixture_id"],
        "competition_id": outcome["competition_id"],
        "season_id": outcome["season_id"],
        "information_state": evidence["information_state"],
        "prediction_at": prediction_at.isoformat(),
        "kickoff": kickoff.isoformat(),
        "first_snapshot_as_of": snapshot_as_of.isoformat(),
        "first_snapshot_created_at": snapshot_created_at.isoformat(),
        "settled_at": settled_at.isoformat(),
        "model_version": model.model_version,
        "logical_model_sha256": score_grid_shadow_sha256(model),
        "prospective_gate_version": gate["gate_version"],
        "settlement_config_sha256": settlement_config_sha256,
        "prospective_gate_file_sha256": gate_file_sha256,
        "contract_registry_sha256": contract_registry_sha256,
        "reviewed_result_exclusions_sha256": reviewed_exclusions_sha256,
        "shadow_model_artifact_sha256": model_artifact_sha256,
        "forecast_provenance": {
            "evidence_record_sha256": evidence["evidence_record_sha256"],
            "evidence_file_sha256": evidence["evidence_file_sha256"],
            "snapshot_logical_sha256": evidence["snapshot_logical_sha256"],
            "score_grid_sha256": stored_hash,
            "parent_snapshot_source": evidence.get("sources", {}).get(
                "parent_snapshot"
            )
            if isinstance(evidence.get("sources"), Mapping)
            else None,
        },
        "realized_regulation_score": {
            "home_goals": home_goals,
            "away_goals": away_goals,
            "result": _result(home_goals, away_goals),
            "total_goals": home_goals + away_goals,
            "goal_difference": home_goals - away_goals,
            "both_teams_to_score": home_goals > 0 and away_goals > 0,
        },
        "result_provenance": {
            "agreeing_source_codes": sorted({row["source_code"] for row in result_rows}),
            "observation_ids": sorted({row["observation_id"] for row in result_rows}),
            "raw_artifact_ids": sorted(
                {row["raw_artifact_id"] for row in result_rows if row["raw_artifact_id"]}
            ),
            "earliest_retrieved_at": earliest_result_retrieval.isoformat(),
            "latest_retrieved_at": latest_result_retrieval.isoformat(),
            "eligibility_reason_codes": outcome["reason_codes"],
        },
        "integrity_checks": checks,
        "eligible_for_prospective_gate": all(checks.values()),
        "metrics": {
            "candidate": candidate_metrics,
            "baseline": baseline_metrics,
            "candidate_minus_baseline": deltas,
        },
        "reference_contract_settlements": {
            "candidate": _reference_settlements(candidate, home_goals, away_goals, config),
            "baseline": _reference_settlements(baseline, home_goals, away_goals, config),
        },
    }


def _score_metrics(
    grid: ScoreGrid, home_goals: int, away_goals: int, floor: float
) -> dict[str, float]:
    probabilities = grid.probabilities
    home = _marginal(probabilities, lambda h, _a: h)
    away = _marginal(probabilities, lambda _h, a: a)
    totals = _marginal(probabilities, lambda h, a: h + a)
    differences = _marginal(probabilities, lambda h, a: h - a)
    moneyline = grid.moneyline()
    btts = grid.both_teams_to_score()
    result = _result(home_goals, away_goals)
    actual_btts = "yes" if home_goals > 0 and away_goals > 0 else "no"

    def loss(probability: float) -> float:
        return -math.log(max(probability, floor))

    return {
        "exact_score_probability": probabilities.get((home_goals, away_goals), 0.0),
        "home_goals_probability": home.get(home_goals, 0.0),
        "away_goals_probability": away.get(away_goals, 0.0),
        "total_goals_probability": totals.get(home_goals + away_goals, 0.0),
        "goal_difference_probability": differences.get(home_goals - away_goals, 0.0),
        "both_teams_to_score_probability": btts["yes"],
        "exact_score_log_loss": loss(probabilities.get((home_goals, away_goals), 0.0)),
        "home_goals_log_loss": loss(home.get(home_goals, 0.0)),
        "away_goals_log_loss": loss(away.get(away_goals, 0.0)),
        "total_goals_log_loss": loss(totals.get(home_goals + away_goals, 0.0)),
        "goal_difference_log_loss": loss(
            differences.get(home_goals - away_goals, 0.0)
        ),
        "moneyline_log_loss": loss(moneyline[result]),
        "moneyline_brier": math.fsum(
            (probability - float(key == result)) ** 2
            for key, probability in moneyline.items()
        ),
        "both_teams_to_score_log_loss": loss(btts[actual_btts]),
        "both_teams_to_score_brier": (
            btts["yes"] - float(actual_btts == "yes")
        ) ** 2,
        "total_goals_rps": _rps(totals, home_goals + away_goals),
        "goal_difference_rps": _rps(differences, home_goals - away_goals),
    }


def _reference_settlements(
    grid: ScoreGrid,
    home_goals: int,
    away_goals: int,
    config: Mapping[str, object],
) -> dict[str, object]:
    actual = ScoreGrid({(home_goals, away_goals): 1.0})
    line_config = config["reference_lines"]
    totals = {}
    for line in _lines(line_config["total_goals"]):
        key = _line_key(line)
        totals[key] = {}
        for side in ("over", "under"):
            forecast = grid.total_goals(line=line, selection=side)
            realized = actual.total_goals(line=line, selection=side)
            totals[key][side] = {
                "forecast": forecast.probabilities,
                "realized_outcome": _certain_outcome(realized.probabilities),
            }
    handicaps = {}
    for line in _lines(line_config["goal_handicap"]):
        key = _line_key(line)
        handicaps[key] = {}
        for team in ("home", "away"):
            forecast = grid.goal_handicap(team=team, line=line)
            realized = actual.goal_handicap(team=team, line=line)
            handicaps[key][team] = {
                "forecast": forecast.probabilities,
                "realized_outcome": _certain_outcome(realized.probabilities),
            }
    return {"total_goals": totals, "goal_handicap": handicaps}


def _load_outcomes(path: Path, fixture_ids: set[str]) -> dict[str, dict[str, object]]:
    if not fixture_ids:
        return {}
    connection = duckdb.connect(str(path), read_only=True)
    try:
        values: dict[str, dict[str, object]] = {}
        ordered_ids = sorted(fixture_ids)
        for offset in range(0, len(ordered_ids), 500):
            chunk = ordered_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT
                    f.fixture_id, f.competition_id, f.season_id,
                    f.scheduled_kickoff, e.eligible_result_models,
                    e.reason_codes, r.observation_id, r.source_code,
                    r.raw_artifact_id, r.retrieved_at,
                    r.home_score_regulation, r.away_score_regulation,
                    r.result_status
                FROM fixture f
                JOIN fixture_model_eligibility e USING (fixture_id)
                LEFT JOIN fixture_result_observation r USING (fixture_id)
                WHERE f.fixture_id IN ({placeholders})
                ORDER BY f.fixture_id, r.retrieved_at, r.observation_id
                """,
                chunk,
            ).fetchall()
            for row in rows:
                item = values.setdefault(
                    row[0],
                    {
                        "competition_id": row[1],
                        "season_id": row[2],
                        "scheduled_kickoff": row[3],
                        "eligible_result_models": row[4],
                        "reason_codes": _json_value(row[5]),
                        "final_observation_count": 0,
                        "valid_final_observations": [],
                    },
                )
                if row[12] == "final":
                    item["final_observation_count"] += 1
                if (
                    row[12] == "final"
                    and isinstance(row[10], int)
                    and isinstance(row[11], int)
                    and row[10] >= 0
                    and row[11] >= 0
                ):
                    item["valid_final_observations"].append(
                        {
                            "observation_id": row[6],
                            "source_code": row[7],
                            "raw_artifact_id": row[8],
                            "retrieved_at": row[9],
                            "home_goals": row[10],
                            "away_goals": row[11],
                        }
                    )
        return values
    finally:
        connection.close()


def _read_ledger(
    path: Path, *, config: Mapping[str, object]
) -> tuple[list[dict[str, object]], str | None]:
    if not path.exists():
        return [], None
    records = []
    previous = None
    seen_evidence = set()
    seen_pairs = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProspectiveSettlementError(
                f"ledger line {line_number} is invalid JSON"
            ) from error
        if not isinstance(record, dict):
            raise ProspectiveSettlementError(f"ledger line {line_number} is not an object")
        if record.get("ledger_version") != config["ledger_version"]:
            raise ProspectiveSettlementError("ledger version mismatch")
        if record.get("previous_record_sha256") != previous:
            raise ProspectiveSettlementError("ledger hash chain is broken")
        claimed = record.get("record_sha256")
        expected = _logical_sha256(
            {key: value for key, value in record.items() if key != "record_sha256"}
        )
        if claimed != expected:
            raise ProspectiveSettlementError("ledger record hash mismatch")
        evidence_key = record.get("evidence_key")
        pair = (record.get("fixture_id"), record.get("information_state"))
        if evidence_key in seen_evidence or pair in seen_pairs:
            raise ProspectiveSettlementError("duplicate prospective pairing key in ledger")
        seen_evidence.add(evidence_key)
        seen_pairs.add(pair)
        previous = str(claimed)
        records.append(record)
    return records, previous


def _parse_grid(value: object) -> ScoreGrid:
    if not isinstance(value, list) or not value:
        raise ProspectiveSettlementError("score grid is missing")
    probabilities = {}
    for cell in value:
        if not isinstance(cell, Mapping):
            raise ProspectiveSettlementError("score-grid cell is not an object")
        score = (cell.get("home_goals"), cell.get("away_goals"))
        if score in probabilities:
            raise ProspectiveSettlementError("duplicate score-grid cell")
        probabilities[score] = cell.get("probability")
    return ScoreGrid(probabilities)


def _marginal(probabilities, key_function) -> dict[int, float]:
    values: dict[int, float] = defaultdict(float)
    for (home, away), probability in probabilities.items():
        values[key_function(home, away)] += probability
    return dict(values)


def _rps(distribution: Mapping[int, float], observed: int) -> float:
    cumulative = 0.0
    score = 0.0
    for threshold in range(min(distribution), max(distribution)):
        cumulative += distribution.get(threshold, 0.0)
        score += (cumulative - float(observed <= threshold)) ** 2
    return score


def _lines(specification: Mapping[str, object]) -> list[Decimal]:
    minimum = Decimal(str(specification["minimum"]))
    maximum = Decimal(str(specification["maximum"]))
    increment = Decimal(str(specification["increment"]))
    if increment <= 0 or minimum > maximum:
        raise ProspectiveSettlementError("invalid reference-line range")
    count = (maximum - minimum) / increment
    if count != count.to_integral_value() or count > 100:
        raise ProspectiveSettlementError("reference-line range is not finite and aligned")
    return [minimum + index * increment for index in range(int(count) + 1)]


def _line_key(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _certain_outcome(probabilities: Mapping[str, float]) -> str:
    matches = [key for key, value in probabilities.items() if math.isclose(value, 1.0)]
    if len(matches) != 1:
        raise ProspectiveSettlementError("realized settlement is not deterministic")
    return matches[0]


def _result(home_goals: int, away_goals: int) -> str:
    return "home_win" if home_goals > away_goals else "draw" if home_goals == away_goals else "away_win"


def _grid_sha256(probabilities: Mapping[tuple[int, int], float]) -> str:
    body = json.dumps(
        [[score[0], score[1], probability] for score, probability in sorted(probabilities.items())],
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_config(config: Mapping[str, object], *, model, gate) -> None:
    if config.get("status") != "frozen_before_first_predicted_fixture_result":
        raise ProspectiveSettlementError("settlement recipe is not frozen")
    if config.get("model_version") != model.model_version:
        raise ProspectiveSettlementError("settlement model version mismatch")
    if config.get("logical_model_sha256") != score_grid_shadow_sha256(model):
        raise ProspectiveSettlementError("settlement model hash mismatch")
    if config.get("prospective_gate_version") != gate["gate_version"]:
        raise ProspectiveSettlementError("settlement gate version mismatch")
    if config.get("pairing_key") != ["fixture_id", "information_state"]:
        raise ProspectiveSettlementError("settlement pairing key changed")
    if config.get("canonical_forecast_policy") != "first_valid_immutable_evidence_per_pair":
        raise ProspectiveSettlementError("settlement forecast policy changed")
    floor = float(config.get("probability_floor", 0))
    if not 0 < floor < 1:
        raise ProspectiveSettlementError("settlement probability floor is invalid")
    reporting = config.get("reporting_policy")
    if not isinstance(reporting, Mapping) or reporting.get(
        "write_aggregate_performance_before_gate"
    ) is not False or reporting.get("write_gate_decision_before_minimum_evidence") is not False:
        raise ProspectiveSettlementError("premature performance reporting is enabled")
    integrity = config.get("integrity")
    required_integrity = (
        "require_result_model_eligibility",
        "require_current_kickoff_matches_prediction",
        "require_prediction_at_or_before_first_snapshot_as_of",
        "require_snapshot_creation_at_or_after_snapshot_as_of",
        "require_prediction_horizon_matches_information_state",
        "require_first_snapshot_as_of_before_kickoff",
        "require_first_snapshot_created_before_kickoff",
        "require_final_result_retrieved_after_forecast_creation",
        "require_settlement_run_at_or_after_all_result_retrievals",
        "require_score_grid_hash_match",
        "require_recomputed_shadow_grid_match",
        "append_only_hash_chain",
        "never_rewrite_settled_records",
    )
    if not isinstance(integrity, Mapping) or any(
        integrity.get(key) is not True for key in required_integrity
    ):
        raise ProspectiveSettlementError("required settlement integrity rule is disabled")
    references = config.get("reference_lines")
    if not isinstance(references, Mapping) or set(references) != {
        "total_goals",
        "goal_handicap",
    }:
        raise ProspectiveSettlementError("reference-line configuration is invalid")
    for value in references.values():
        if not isinstance(value, Mapping):
            raise ProspectiveSettlementError("reference-line range is not an object")
        _lines(value)


def _validate_frozen_artifacts(
    config: Mapping[str, object],
    *,
    contract_path: Path,
    exclusion_path: Path,
    gate_path: Path,
    model_path: Path,
) -> None:
    expected = config.get("frozen_artifact_sha256")
    if not isinstance(expected, Mapping):
        raise ProspectiveSettlementError("frozen artifact hashes are missing")
    actual = {
        "contract_registry": _file_sha256(contract_path),
        "reviewed_result_exclusions": _file_sha256(exclusion_path),
        "prospective_gate": _file_sha256(gate_path),
        "shadow_model_artifact": _file_sha256(model_path),
    }
    if set(expected) != set(actual):
        raise ProspectiveSettlementError("frozen artifact hash registry changed")
    for name, digest in actual.items():
        if expected[name] != digest:
            raise ProspectiveSettlementError(f"frozen {name} hash mismatch")


def _validate_evidence_set(
    evidence: list[Mapping[str, object]], *, model, gate: Mapping[str, object]
) -> None:
    expected_model_hash = score_grid_shadow_sha256(model)
    pairs = set()
    for item in evidence:
        pair = (str(item["fixture_id"]), str(item["information_state"]))
        if pair in pairs:
            raise ProspectiveSettlementError(
                "multiple forecast evidence files exist for one pairing key"
            )
        pairs.add(pair)
        if (
            item.get("model_version") != model.model_version
            or item.get("logical_model_sha256") != expected_model_hash
            or item.get("prospective_gate_version") != gate["gate_version"]
            or _timestamp(item.get("prospective_holdout_start"))
            != model.prospective_holdout_start
        ):
            raise ProspectiveSettlementError("forecast evidence identity mismatch")


def _horizon_matches(
    information_state: str, prediction_at: datetime, kickoff: datetime
) -> bool:
    expected_hours = {
        "pre_lineup_24h_v1": 24,
        "pre_lineup_72h_clean_v1": 72,
    }
    hours = expected_hours.get(information_state)
    return hours is not None and kickoff - prediction_at == timedelta(hours=hours)


def _atomic_append_jsonl(path: Path, records: list[Mapping[str, object]]) -> None:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise ProspectiveSettlementError("ledger does not end at a record boundary")
    appended = b"".join(
        (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        for record in records
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


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProspectiveSettlementError(f"{path.name} is not an object")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
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
        raise ProspectiveSettlementError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ProspectiveSettlementError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ProspectiveSettlementError("datetime lacks timezone")
    return value.astimezone(timezone.utc)


def _inside_root(root: Path, value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise ProspectiveSettlementError("configured settlement path must stay inside root")
    return root / path
