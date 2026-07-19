#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.targets import load_regulation_target_exclusions
from soccer_bot.datasets.timing import build_first_team_score_targets
from soccer_bot.modeling.score_grid import read_rich_rate_predictions
from soccer_bot.modeling.timing import (
    FirstScoreObservation,
    baseline_first_team_probabilities,
    dump_first_score_model,
    first_score_model_sha256,
    first_team_probabilities,
    fit_first_score_timing_model,
    load_first_score_config,
)
from soccer_bot.modeling.walk_forward import (
    block_bootstrap_interval,
    comparison_seed,
)


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    rich_root = feature_root / "regulation_walk_forward_v1" / "rich_rate_v1"
    parser = argparse.ArgumentParser(
        description=(
            "Fit the frozen first-team-to-score challenger and write a "
            "retrospective audit without claiming promotion."
        )
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "models" / "first_score_timing_v1.json",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=(
            ROOT / "config" / "models" / "regulation_score_exclusions_v1.json"
        ),
    )
    parser.add_argument(
        "--features", type=Path, default=feature_root / "features.parquet"
    )
    parser.add_argument(
        "--development-predictions",
        type=Path,
        default=rich_root / "validation_predictions.parquet",
    )
    parser.add_argument(
        "--later-predictions",
        type=Path,
        default=rich_root / "promoted_evaluation" / "predictions.parquet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "models" / "first_score_timing_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_first_score_config(args.config)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        target_build = build_first_team_score_targets(
            connection,
            reviewed_result_exclusions=load_regulation_target_exclusions(
                args.exclusions
            ),
        )
    finally:
        connection.close()
    targets = {row.fixture_id: row for row in target_build.targets}
    earliest = datetime(1970, 1, 1, tzinfo=timezone.utc)
    development_rates = read_rich_rate_predictions(
        args.development_predictions,
        feature_path=args.features,
        kickoff_start=earliest,
        kickoff_end=config.training_kickoff_end_exclusive,
    )
    later_rates = read_rich_rate_predictions(
        args.later_predictions,
        feature_path=args.features,
        kickoff_start=earliest,
        kickoff_end=config.training_kickoff_end_exclusive,
    )
    development = _join(development_rates, targets)
    later = _join(later_rates, targets)
    all_rows = _unique([*development, *later])

    # This is a historical audit only. It is useful evidence, but the later
    # period was already visible when this recipe was created and therefore
    # cannot approve the candidate.
    development_end = min(row.kickoff for row in later)
    audit_config = replace(
        config,
        training_kickoff_end_exclusive=development_end,
        minimum_fit_fixtures=min(config.minimum_fit_fixtures, 200),
    )
    audit_model = fit_first_score_timing_model(development, audit_config)
    retrospective = _evaluate(audit_model, later, config)

    model = fit_first_score_timing_model(all_rows, config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    model_path = args.output_dir / "model.json"
    temporary = args.output_dir / ".model.json.tmp"
    dump_first_score_model(model, temporary, created_at=created_at)
    os.replace(temporary, model_path)
    report = {
        "report_version": "first_score_timing_retrospective_v1",
        "created_at": created_at.isoformat(),
        "status": config.status,
        "retrospective_promotion_claim": False,
        "reason": (
            "The evaluation period was already observable before the recipe "
            "was frozen; only the prospective holdout can approve this model."
        ),
        "prospective_holdout_start": config.prospective_holdout_start.isoformat(),
        "target_audit": {
            "safe_targets": len(target_build.targets),
            "excluded_issue_counts": target_build.issue_counts,
        },
        "joined_rows": {
            "development": len(development),
            "later": len(later),
            "all_unique": len(all_rows),
        },
        "development_fit_end_exclusive": development_end.isoformat(),
        "retrospective_evaluation": retrospective,
        "all_history_fit": [asdict(row) for row in model.horizons],
    }
    report_path = args.output_dir / "report.json"
    _atomic_write_json(report_path, report)
    manifest = {
        "artifact_version": "first_score_timing_manifest_v1",
        "created_at": created_at.isoformat(),
        "retrospective_promotion_claim": False,
        "logical_model_sha256": first_score_model_sha256(model),
        "sources": {
            "warehouse": _source(args.warehouse),
            "configuration": _source(args.config),
            "target_exclusions": _source(args.exclusions),
            "features": _source(args.features),
            "development_predictions": _source(args.development_predictions),
            "later_predictions": _source(args.later_predictions),
        },
        "artifacts": {
            "model": _source(model_path),
            "report": _source(report_path),
        },
    }
    manifest_path = args.output_dir / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "model": str(model_path.resolve()),
                "report": str(report_path.resolve()),
                "manifest": str(manifest_path.resolve()),
                "logical_model_sha256": manifest["logical_model_sha256"],
                "retrospective_promotion_claim": False,
                "safe_targets": len(target_build.targets),
                "joined_rows": len(all_rows),
                "horizons": [asdict(row) for row in model.horizons],
            },
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    )
    return 0


def _join(rate_rows, targets) -> list[FirstScoreObservation]:
    values = []
    for row in rate_rows:
        target = targets.get(str(row.fixture_id))
        if target is None:
            continue
        values.append(
            FirstScoreObservation(
                fixture_id=str(row.fixture_id),
                information_state=row.information_state,
                prediction_at=row.prediction_at,
                kickoff=row.kickoff,
                outcome=target.outcome,
                expected_home_goals=row.expected_home_goals,
                expected_away_goals=row.expected_away_goals,
            )
        )
    return values


def _unique(rows: list[FirstScoreObservation]) -> list[FirstScoreObservation]:
    values = {}
    for row in rows:
        key = (row.fixture_id, row.information_state)
        if key in values:
            raise RuntimeError(f"First-score input sources overlap at {key}")
        values[key] = row
    return list(values.values())


def _evaluate(model, rows: list[FirstScoreObservation], config) -> dict:
    grouped = {}
    for information_state in config.information_states:
        values = sorted(
            (row for row in rows if row.information_state == information_state),
            key=lambda row: (row.kickoff, row.fixture_id),
        )
        if not values:
            continue
        baseline_losses = []
        candidate_losses = []
        baseline_briers = []
        candidate_briers = []
        blocks = defaultdict(list)
        for row in values:
            baseline = baseline_first_team_probabilities(
                row.expected_home_goals, row.expected_away_goals
            )
            candidate = first_team_probabilities(
                model,
                information_state=information_state,
                expected_home_goals=row.expected_home_goals,
                expected_away_goals=row.expected_away_goals,
            )
            base_loss = -math.log(max(baseline[row.outcome], config.probability_floor))
            candidate_loss = -math.log(
                max(candidate[row.outcome], config.probability_floor)
            )
            base_brier = math.fsum(
                (baseline[key] - float(key == row.outcome)) ** 2
                for key in baseline
            )
            candidate_brier = math.fsum(
                (candidate[key] - float(key == row.outcome)) ** 2
                for key in candidate
            )
            baseline_losses.append(base_loss)
            candidate_losses.append(candidate_loss)
            baseline_briers.append(base_brier)
            candidate_briers.append(candidate_brier)
            blocks.setdefault((row.kickoff.year, row.kickoff.month), []).append(
                candidate_loss - base_loss
            )
        lower, upper, probability = block_bootstrap_interval(
            blocks,
            replicates=config.bootstrap_replicates,
            seed=comparison_seed(
                config.bootstrap_seed, information_state, "first_score_log_loss"
            ),
        )
        grouped[information_state] = {
            "fixtures": len(values),
            "calendar_month_blocks": len(blocks),
            "baseline_mean_log_loss": math.fsum(baseline_losses) / len(values),
            "candidate_mean_log_loss": math.fsum(candidate_losses) / len(values),
            "mean_log_loss_delta_candidate_minus_baseline": math.fsum(
                candidate - baseline
                for candidate, baseline in zip(
                    candidate_losses, baseline_losses, strict=True
                )
            )
            / len(values),
            "paired_month_block_bootstrap_95_lower": lower,
            "paired_month_block_bootstrap_95_upper": upper,
            "bootstrap_probability_candidate_is_better": probability,
            "baseline_mean_brier": math.fsum(baseline_briers) / len(values),
            "candidate_mean_brier": math.fsum(candidate_briers) / len(values),
        }
    return grouped


def _source(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": _file_sha256(path)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
