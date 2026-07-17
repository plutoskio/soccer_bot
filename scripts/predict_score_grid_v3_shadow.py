#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.score_grid_shadow import (
    load_score_grid_prospective_gate,
    load_score_grid_shadow_model,
    predict_coherent_score_grid,
    score_grid_shadow_sha256,
)
from soccer_bot.prospective_evidence import (
    materialize_legacy_evidence,
    materialize_snapshot_evidence,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a non-production coherent score-grid shadow snapshot from "
            "the incumbent champion moneyline snapshot."
        )
    )
    parser.add_argument(
        "--parent-snapshot",
        type=Path,
        default=ROOT / "data" / "predictions" / "regulation_champion_v1" / "latest.json",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT / "data" / "models" / "regulation_score_grid_v3_shadow" / "model.json",
    )
    parser.add_argument(
        "--prospective-gate",
        type=Path,
        default=(
            ROOT
            / "config"
            / "models"
            / "regulation_score_grid_v3_prospective_gate.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "predictions" / "regulation_score_grid_v3_shadow",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    parent = json.loads(args.parent_snapshot.read_text(encoding="utf-8"))
    model = load_score_grid_shadow_model(args.model)
    gate = load_score_grid_prospective_gate(args.prospective_gate, model=model)
    if parent.get("model_version") != model.parent_moneyline_model_version:
        raise RuntimeError("Parent snapshot and shadow model versions differ")
    as_of = _aware_datetime(parent["as_of"])
    if as_of < model.recipe_frozen_at:
        raise RuntimeError(
            "Parent snapshot predates the frozen score-grid shadow recipe"
        )
    created_at = datetime.now(timezone.utc)
    records = []
    excluded_pre_holdout = []
    for prediction in parent.get("predictions", []):
        kickoff = _aware_datetime(prediction["kickoff"])
        if kickoff < model.prospective_holdout_start:
            excluded_pre_holdout.append(
                {
                    "fixture_id": prediction["fixture_id"],
                    "information_state": prediction["information_state"],
                    "kickoff": kickoff.isoformat(),
                    "reason": "kickoff_before_frozen_prospective_holdout",
                }
            )
            continue
        if created_at >= kickoff:
            excluded_pre_holdout.append(
                {
                    "fixture_id": prediction["fixture_id"],
                    "information_state": prediction["information_state"],
                    "kickoff": kickoff.isoformat(),
                    "reason": "shadow_generation_not_strictly_before_kickoff",
                }
            )
            continue
        parent_moneyline = {
            "home_win": prediction["home_win_probability"],
            "draw": prediction["draw_probability"],
            "away_win": prediction["away_win_probability"],
        }
        score_grid = predict_coherent_score_grid(
            expected_home_goals=prediction["expected_home_goals"],
            expected_away_goals=prediction["expected_away_goals"],
            parent_moneyline=parent_moneyline,
            information_state=prediction["information_state"],
            model=model,
        )
        probabilities = score_grid.probabilities
        implied_moneyline = score_grid.moneyline()
        for outcome in ("home_win", "draw", "away_win"):
            if not math.isclose(
                implied_moneyline[outcome], parent_moneyline[outcome], abs_tol=1e-10
            ):
                raise RuntimeError("Shadow snapshot changed parent moneyline")
        records.append(
            {
                "fixture_id": prediction["fixture_id"],
                "information_state": prediction["information_state"],
                "prediction_at": prediction["prediction_at"],
                "kickoff": prediction["kickoff"],
                "fixture": prediction.get("fixture"),
                "expected_home_goals": prediction["expected_home_goals"],
                "expected_away_goals": prediction["expected_away_goals"],
                "parent_moneyline": parent_moneyline,
                "implied_moneyline": implied_moneyline,
                "both_teams_to_score": score_grid.both_teams_to_score(),
                "home_goal_distribution": _marginal(probabilities, "home"),
                "away_goal_distribution": _marginal(probabilities, "away"),
                "total_goal_distribution": _marginal(probabilities, "total"),
                "goal_difference_distribution": _marginal(
                    probabilities, "difference"
                ),
                "top_exact_scores": [
                    {
                        "home_goals": score[0],
                        "away_goals": score[1],
                        "probability": probability,
                    }
                    for score, probability in sorted(
                        probabilities.items(), key=lambda item: item[1], reverse=True
                    )[:15]
                ],
                "score_grid": [
                    {
                        "home_goals": score[0],
                        "away_goals": score[1],
                        "probability": probability,
                    }
                    for score, probability in sorted(probabilities.items())
                ],
                "score_grid_sha256": _grid_sha256(probabilities),
                "warnings": [
                    "prospective_shadow_not_for_production_betting",
                    "retrospective_performance_not_estimated",
                ],
            }
        )
    snapshot = {
        "snapshot_version": "regulation_score_grid_v3_shadow_snapshot_v1",
        "created_at": created_at.isoformat(),
        "as_of": as_of.astimezone(timezone.utc).isoformat(),
        "model_version": model.model_version,
        "model_status": model.status,
        "parent_model_version": model.parent_moneyline_model_version,
        "logical_model_sha256": score_grid_shadow_sha256(model),
        "prospective_holdout_start": model.prospective_holdout_start.isoformat(),
        "prospective_gate_version": gate["gate_version"],
        "retrospective_performance_claim": False,
        "supported_shadow_outputs": [
            "exact_score",
            "moneyline",
            "goal_handicap",
            "total_goals",
            "team_total_goals",
            "both_teams_to_score",
        ],
        "predictions": records,
        "audit": {
            "parent_prediction_rows": len(parent.get("predictions", [])),
            "shadow_prediction_rows": len(records),
            "excluded_pre_holdout": excluded_pre_holdout,
        },
        "sources": {
            "parent_snapshot": _source(args.parent_snapshot),
            "shadow_model": _source(args.model),
            "prospective_gate": _source(args.prospective_gate),
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    latest_path = args.output_dir / "latest.json"
    legacy = materialize_legacy_evidence(args.output_dir)
    evidence = materialize_snapshot_evidence(
        output_directory=args.output_dir,
        snapshot=snapshot,
    )
    _atomic_write_json(latest_path, snapshot)
    print(
        json.dumps(
            {
                "as_of": snapshot["as_of"],
                "parent_prediction_rows": snapshot["audit"][
                    "parent_prediction_rows"
                ],
                "shadow_prediction_rows": len(records),
                "excluded_pre_holdout": len(excluded_pre_holdout),
                "latest": str(latest_path.resolve()),
                "legacy_snapshots_imported": legacy["legacy_snapshots"],
                "legacy_evidence_added": legacy["new_evidence"],
                "new_evidence": evidence["new_evidence"],
                "existing_evidence": evidence["existing_evidence"],
                "evidence_receipt": evidence["receipt_path"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _marginal(
    probabilities: dict[tuple[int, int], float], kind: str
) -> list[dict]:
    values: dict[int, float] = {}
    for (home_goals, away_goals), probability in probabilities.items():
        if kind == "home":
            key = home_goals
        elif kind == "away":
            key = away_goals
        elif kind == "total":
            key = home_goals + away_goals
        elif kind == "difference":
            key = home_goals - away_goals
        else:
            raise ValueError(f"Unsupported marginal: {kind}")
        values[key] = values.get(key, 0.0) + probability
    return [
        {"value": key, "probability": value}
        for key, value in sorted(values.items())
    ]


def _grid_sha256(probabilities: dict[tuple[int, int], float]) -> str:
    body = json.dumps(
        [
            [score[0], score[1], probability]
            for score, probability in sorted(probabilities.items())
        ],
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _write_immutable_json(path: Path, value: dict) -> None:
    body = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError(
                f"Immutable shadow snapshot already exists with different bytes: {path}"
            )
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


def _aware_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Snapshot timestamps must be timezone-aware")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
