#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.modeling.score_grid import (
    confirmation_gate,
    evaluate_score_grid_window,
    evaluation_rows_sha256,
    load_score_grid_research_config,
    read_rich_rate_predictions,
    select_candidate,
)


def parse_args() -> argparse.Namespace:
    feature_root = ROOT / "data" / "features" / "regulation_team_state_v1"
    rich_root = feature_root / "regulation_walk_forward_v1" / "rich_rate_v1"
    parser = argparse.ArgumentParser(
        description=(
            "Research a coherent regulation score-grid challenger without "
            "reading the already-opened final-test period."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "models" / "regulation_score_grid_v2.json",
    )
    parser.add_argument(
        "--features",
        type=Path,
        default=feature_root / "features.parquet",
    )
    parser.add_argument(
        "--development-validation-predictions",
        type=Path,
        default=rich_root / "validation_predictions.parquet",
    )
    parser.add_argument(
        "--promoted-development-predictions",
        type=Path,
        default=rich_root / "promoted_evaluation" / "predictions.parquet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=rich_root / "score_grid_v2",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_score_grid_research_config(args.config)
    sources = {
        "development_validation_predictions": (
            args.development_validation_predictions
        ),
        "promoted_development_predictions": (
            args.promoted_development_predictions
        ),
    }
    windows = {window.purpose: window for window in config.windows}
    if set(windows) != {
        "candidate_family_selection",
        "selected_family_confirmation",
    }:
        raise RuntimeError("Exactly one selection and confirmation window are required")

    selection_window = windows["candidate_family_selection"]
    selection_rows = read_rich_rate_predictions(
        sources[selection_window.source_key],
        feature_path=args.features,
        kickoff_start=selection_window.fit_start_inclusive,
        kickoff_end=selection_window.validation_end_exclusive,
    )
    selection_fits, selection_evaluations, selection_summary = (
        evaluate_score_grid_window(
            selection_rows,
            window=selection_window,
            candidates=config.candidates,
            config=config,
        )
    )
    selection = select_candidate(selection_summary, config)

    confirmation_fits = []
    confirmation_evaluations = []
    confirmation_summary = {"metrics": [], "paired_model_comparisons": []}
    if selection["selected_model"] is None:
        gate = {
            "selected_model": None,
            "confirmation_gate_passed": False,
            "failures": ["selection_gate_failed"],
            "production_status": "research_candidate_not_selected",
        }
    else:
        selected_candidates = tuple(
            candidate
            for candidate in config.candidates
            if candidate.model_key == selection["selected_model"]
        )
        if len(selected_candidates) != 1:
            raise RuntimeError("Selected candidate is not uniquely configured")
        confirmation_window = windows["selected_family_confirmation"]
        confirmation_rows = read_rich_rate_predictions(
            sources[confirmation_window.source_key],
            feature_path=args.features,
            kickoff_start=confirmation_window.fit_start_inclusive,
            kickoff_end=confirmation_window.validation_end_exclusive,
        )
        (
            confirmation_fits,
            confirmation_evaluations,
            confirmation_summary,
        ) = evaluate_score_grid_window(
            confirmation_rows,
            window=confirmation_window,
            candidates=selected_candidates,
            config=config,
        )
        gate = confirmation_gate(
            confirmation_summary, selection["selected_model"], config
        )

    all_fits = [*selection_fits, *confirmation_fits]
    all_evaluations = [*selection_evaluations, *confirmation_evaluations]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    evaluations_path = args.output_dir / "evaluation_rows.parquet"
    report_path = args.output_dir / "report.json"
    manifest_path = args.output_dir / "manifest.json"
    _write_evaluations(all_evaluations, evaluations_path)
    report = {
        "model_version": config.model_version,
        "research_status": config.research_status,
        "opened_final_test_accessed": False,
        "forbidden_kickoff_start": config.forbidden_kickoff_start.isoformat(),
        "support": {
            "poisson_tail_tolerance": config.poisson_tail_tolerance,
            "minimum_max_goals_per_team": config.minimum_max_goals,
            "maximum_max_goals_per_team": config.maximum_max_goals,
            "probability_floor": config.probability_floor,
        },
        "controls": {
            "score_distribution_baseline": config.baseline_model_key,
            "moneyline_baseline": config.moneyline_control_model_key,
            "moneyline_control_fit": (
                "temperature_fitted_only_on_each_window_fit_half"
            ),
        },
        "fits": [asdict(fit) for fit in all_fits],
        "selection": {
            "window": _window_dict(selection_window),
            "decision": selection,
            "evaluation": selection_summary,
        },
        "confirmation": {
            "window": _window_dict(windows["selected_family_confirmation"]),
            "gate": gate,
            "evaluation": confirmation_summary,
        },
        "artifact": {
            "evaluation_rows": str(evaluations_path.resolve()),
            "evaluation_rows_count": len(all_evaluations),
            "evaluation_rows_sha256": _file_sha256(evaluations_path),
            "logical_evaluation_rows_sha256": evaluation_rows_sha256(
                all_evaluations
            ),
        },
    }
    _atomic_write_json(report_path, report)
    manifest = {
        "artifact_version": "regulation_score_grid_research_v2",
        "model_version": config.model_version,
        "research_status": config.research_status,
        "opened_final_test_accessed": False,
        "source_files": {
            "configuration": _source(args.config),
            "frozen_features": _source(args.features),
            **{key: _source(path) for key, path in sorted(sources.items())},
        },
        "artifacts": {
            "evaluation_rows": _source(evaluations_path),
            "report": _source(report_path),
        },
        "selection": selection,
        "confirmation_gate": gate,
    }
    _atomic_write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "model_version": config.model_version,
                "selected_model": selection["selected_model"],
                "selection_gate_passed": selection["selection_gate_passed"],
                "confirmation_gate_passed": gate["confirmation_gate_passed"],
                "production_status": gate["production_status"],
                "report": str(report_path.resolve()),
                "manifest": str(manifest_path.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _write_evaluations(rows, path: Path) -> None:
    if not rows:
        raise RuntimeError("Cannot write an empty score-grid evaluation")
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="score-grid-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(handle.name)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with handle:
            for row in rows:
                value = asdict(row)
                value["prediction_at"] = row.prediction_at.timestamp()
                value["kickoff"] = row.kickoff.timestamp()
                handle.write(
                    json.dumps(value, separators=(",", ":"), allow_nan=False)
                    + "\n"
                )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (
                        to_timestamp(prediction_at) AS prediction_at,
                        to_timestamp(kickoff) AS kickoff
                    )
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY kickoff, fixture_id, information_state, model_key
                ) TO {_sql_literal(temporary)} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary, path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _window_dict(window) -> dict:
    value = asdict(window)
    for key in (
        "fit_start_inclusive",
        "fit_end_exclusive",
        "validation_start_inclusive",
        "validation_end_exclusive",
    ):
        value[key] = value[key].isoformat()
    return value


def _source(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": _file_sha256(path)}


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
