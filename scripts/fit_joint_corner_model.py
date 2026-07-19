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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.artifacts import read_corner_feature_artifact
from soccer_bot.modeling.corners import (
    CANDIDATES,
    corner_joint_probability,
    corner_model_sha256,
    corner_total_distribution,
    dump_corner_model,
    fit_joint_corner_model,
    load_corner_model_config,
)
from soccer_bot.modeling.walk_forward import (
    block_bootstrap_interval,
    comparison_seed,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fit frozen joint-corner candidates and record retrospective "
            "development evidence without claiming promotion."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "models" / "joint_corners_v1.json",
    )
    parser.add_argument(
        "--features",
        type=Path,
        default=(
            ROOT / "data" / "features" / "corner_team_state_v1" / "features.parquet"
        ),
    )
    parser.add_argument(
        "--feature-manifest",
        type=Path,
        default=(
            ROOT / "data" / "features" / "corner_team_state_v1" / "manifest.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "models" / "joint_corners_v1",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_corner_model_config(args.config)
    rows = read_corner_feature_artifact(args.features)
    rows = [row for row in rows if row.kickoff < config.training_kickoff_end_exclusive]
    development_end = datetime(2024, 7, 1, tzinfo=timezone.utc)
    development = [row for row in rows if row.kickoff < development_end]
    later = [row for row in rows if row.kickoff >= development_end]
    audit_config = replace(
        config,
        training_kickoff_end_exclusive=development_end,
        minimum_fit_fixtures=min(config.minimum_fit_fixtures, 500),
    )
    audit_model = fit_joint_corner_model(development, audit_config)
    retrospective = _evaluate(audit_model, later, config)
    selected_candidate = _select(retrospective, config.information_states)

    model = fit_joint_corner_model(rows, config)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    model_path = args.output_dir / "model.json"
    temporary = args.output_dir / ".model.json.tmp"
    dump_corner_model(model, temporary, created_at=created_at)
    os.replace(temporary, model_path)
    report = {
        "report_version": "joint_corners_retrospective_v1",
        "created_at": created_at.isoformat(),
        "status": config.status,
        "retrospective_promotion_claim": False,
        "reason": (
            "The later evaluation period was visible before this recipe was "
            "frozen. It can select a forward challenger but cannot approve it."
        ),
        "development_fit_end_exclusive": development_end.isoformat(),
        "prospective_holdout_start": config.prospective_holdout_start.isoformat(),
        "selected_forward_candidate": selected_candidate,
        "retrospective_evaluation": retrospective,
        "all_history_fit": [asdict(row) for row in model.horizons],
    }
    report_path = args.output_dir / "report.json"
    _atomic_write_json(report_path, report)
    manifest = {
        "artifact_version": "joint_corner_model_manifest_v1",
        "created_at": created_at.isoformat(),
        "logical_model_sha256": corner_model_sha256(model),
        "retrospective_promotion_claim": False,
        "selected_forward_candidate": selected_candidate,
        "sources": {
            "configuration": _source(args.config),
            "features": _source(args.features),
            "feature_manifest": _source(args.feature_manifest),
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
                "selected_forward_candidate": selected_candidate,
                "retrospective_promotion_claim": False,
                "training_rows": len(rows),
                "horizons": [asdict(row) for row in model.horizons],
            },
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    )
    return 0


def _evaluate(model, rows, config) -> dict:
    output = {}
    for information_state in config.information_states:
        values = [row for row in rows if row.information_state == information_state]
        if not values:
            continue
        metrics = {}
        losses_by_candidate = {}
        rps_by_candidate = {}
        line_data = {}
        for candidate in CANDIDATES:
            losses = []
            total_rps = []
            line_probabilities = {8.5: [], 9.5: [], 10.5: []}
            for row in values:
                probability = corner_joint_probability(
                    model,
                    candidate=candidate,
                    information_state=information_state,
                    expected_home_corners=row.expected_home_corners,
                    expected_away_corners=row.expected_away_corners,
                    home_corners=row.home_corners,
                    away_corners=row.away_corners,
                )
                losses.append(-math.log(max(probability, config.probability_floor)))
                distribution = corner_total_distribution(
                    model,
                    config,
                    candidate=candidate,
                    information_state=information_state,
                    expected_home_corners=row.expected_home_corners,
                    expected_away_corners=row.expected_away_corners,
                )
                actual_total = row.home_corners + row.away_corners
                cumulative = 0.0
                rps = 0.0
                for threshold, value in enumerate(distribution[:-1]):
                    cumulative += value
                    rps += (cumulative - float(actual_total <= threshold)) ** 2
                total_rps.append(rps)
                for line in line_probabilities:
                    cutoff = int(math.floor(line))
                    under_or_equal = math.fsum(distribution[: cutoff + 1])
                    line_probabilities[line].append(
                        (1.0 - under_or_equal, float(actual_total > line))
                    )
            losses_by_candidate[candidate] = losses
            rps_by_candidate[candidate] = total_rps
            line_data[candidate] = {
                str(line): {
                    "mean_predicted_over_probability": math.fsum(item[0] for item in pairs) / len(pairs),
                    "observed_over_rate": math.fsum(item[1] for item in pairs) / len(pairs),
                }
                for line, pairs in line_probabilities.items()
            }
            metrics[candidate] = {
                "fixtures": len(values),
                "mean_joint_corner_log_loss": math.fsum(losses) / len(losses),
                "mean_total_corner_rps": math.fsum(total_rps) / len(total_rps),
                "line_calibration": line_data[candidate],
            }
        comparisons = {}
        baseline_losses = losses_by_candidate["independent_poisson"]
        baseline_rps = rps_by_candidate["independent_poisson"]
        for candidate in CANDIDATES[1:]:
            blocks = defaultdict(list)
            for row, candidate_loss, baseline_loss in zip(
                values,
                losses_by_candidate[candidate],
                baseline_losses,
                strict=True,
            ):
                blocks[(row.kickoff.year, row.kickoff.month)].append(
                    candidate_loss - baseline_loss
                )
            lower, upper, probability = block_bootstrap_interval(
                blocks,
                replicates=config.bootstrap_replicates,
                seed=comparison_seed(
                    config.bootstrap_seed,
                    information_state,
                    candidate,
                    "joint_corner_log_loss",
                ),
            )
            comparisons[candidate] = {
                "mean_joint_log_loss_delta_vs_poisson": math.fsum(
                    candidate_loss - baseline_loss
                    for candidate_loss, baseline_loss in zip(
                        losses_by_candidate[candidate], baseline_losses, strict=True
                    )
                )
                / len(values),
                "mean_total_rps_delta_vs_poisson": math.fsum(
                    candidate_rps - base_rps
                    for candidate_rps, base_rps in zip(
                        rps_by_candidate[candidate], baseline_rps, strict=True
                    )
                )
                / len(values),
                "paired_month_block_bootstrap_95_lower": lower,
                "paired_month_block_bootstrap_95_upper": upper,
                "bootstrap_probability_candidate_is_better": probability,
                "calendar_month_blocks": len(blocks),
            }
        output[information_state] = {
            "metrics": metrics,
            "comparisons_to_poisson": comparisons,
        }
    return output


def _select(report: dict, information_states: tuple[str, ...]) -> str:
    eligible = []
    for candidate in CANDIDATES[1:]:
        deltas = [
            report[state]["comparisons_to_poisson"][candidate][
                "mean_joint_log_loss_delta_vs_poisson"
            ]
            for state in information_states
        ]
        if all(delta < 0 for delta in deltas):
            rps = math.fsum(
                report[state]["metrics"][candidate]["mean_total_corner_rps"]
                for state in information_states
            )
            eligible.append((math.fsum(deltas), rps, candidate))
    if not eligible:
        return "independent_poisson"
    return min(eligible)[2]


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
