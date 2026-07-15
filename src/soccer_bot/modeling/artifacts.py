from __future__ import annotations

from collections import Counter
from dataclasses import asdict, fields
import hashlib
import json
import os
from pathlib import Path
import tempfile

import duckdb

from soccer_bot.modeling.walk_forward import (
    WalkForwardPrediction,
    prediction_rows_sha256,
)


class EvaluationArtifactError(RuntimeError):
    """Raised when prediction artifacts cannot be written or verified."""


def read_walk_forward_predictions(path: Path) -> list[WalkForwardPrediction]:
    connection = duckdb.connect(":memory:")
    try:
        relation = connection.execute(
            f"""
            SELECT * FROM read_parquet({_sql_literal(path)})
            ORDER BY prediction_at, fixture_id, information_state, model_key
            """
        )
        names = [item[0] for item in relation.description]
        expected = {field.name for field in fields(WalkForwardPrediction)}
        if set(names) != expected:
            raise EvaluationArtifactError(
                f"Unexpected prediction schema: expected={sorted(expected)}, "
                f"actual={sorted(names)}"
            )
        string_columns = {
            "evaluation_version",
            "model_key",
            "feature_version",
            "fixture_id",
            "information_state",
            "fold_key",
            "result",
        }
        values = []
        for row in relation.fetchall():
            value = dict(zip(names, row, strict=True))
            for column in string_columns:
                value[column] = str(value[column])
            values.append(WalkForwardPrediction(**value))
        return values
    finally:
        connection.close()


def write_walk_forward_artifacts(
    predictions: list[WalkForwardPrediction],
    summary: dict,
    *,
    output_dir: Path,
    source_files: dict[str, Path],
) -> dict:
    if not predictions:
        raise EvaluationArtifactError("Cannot persist an empty evaluation")
    ordered = sorted(
        predictions,
        key=lambda row: (
            row.prediction_at,
            row.fixture_id,
            row.information_state,
            row.model_key,
        ),
    )
    keys = [
        (row.fixture_id, row.information_state, row.model_key) for row in ordered
    ]
    if len(keys) != len(set(keys)):
        raise EvaluationArtifactError("Evaluation contains duplicate prediction keys")

    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = output_dir / "predictions.parquet"
    report_path = output_dir / "report.json"
    manifest_path = output_dir / "manifest.json"
    _write_predictions_parquet(ordered, predictions_path)
    verification = _verify_predictions(predictions_path)
    if verification["rows"] != len(ordered):
        raise EvaluationArtifactError("Prediction Parquet row count changed")
    _atomic_write_json(report_path, summary)

    manifest = {
        "artifact_version": "regulation_walk_forward_evaluation_v1",
        "evaluation_version": ordered[0].evaluation_version,
        "source_files": {
            name: {"path": str(path), "sha256": _file_sha256(path)}
            for name, path in sorted(source_files.items())
        },
        "predictions": {
            "path": str(predictions_path.resolve()),
            "sha256": _file_sha256(predictions_path),
            "logical_rows_sha256": prediction_rows_sha256(ordered),
            "rows": len(ordered),
            "fixtures": len({row.fixture_id for row in ordered}),
            "models": dict(sorted(Counter(row.model_key for row in ordered).items())),
            "horizons": dict(
                sorted(Counter(row.information_state for row in ordered).items())
            ),
            "folds": dict(sorted(Counter(row.fold_key for row in ordered).items())),
            "verification": verification,
        },
        "report": {
            "path": str(report_path.resolve()),
            "sha256": _file_sha256(report_path),
        },
    }
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _write_predictions_parquet(
    predictions: list[WalkForwardPrediction], path: Path
) -> None:
    json_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="predictions-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(json_handle.name)
    temporary_parquet = path.with_name(f".{path.name}.tmp")
    try:
        with json_handle:
            for prediction in predictions:
                value = asdict(prediction)
                value["prediction_at"] = prediction.prediction_at.timestamp()
                value["kickoff"] = prediction.kickoff.timestamp()
                json_handle.write(
                    json.dumps(
                        value,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
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
                    ORDER BY prediction_at, fixture_id, information_state, model_key
                ) TO {_sql_literal(temporary_parquet)}
                (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary_parquet, path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary_parquet.unlink(missing_ok=True)


def _verify_predictions(path: Path) -> dict:
    connection = duckdb.connect(":memory:")
    try:
        row = connection.execute(
            f"""
            SELECT
                count(*),
                count(DISTINCT fixture_id || '|' || information_state || '|' || model_key),
                min(prediction_at),
                max(prediction_at)
            FROM read_parquet({_sql_literal(path)})
            """
        ).fetchone()
    finally:
        connection.close()
    if row[0] != row[1]:
        raise EvaluationArtifactError("Prediction Parquet contains duplicate keys")
    return {
        "rows": row[0],
        "unique_keys": row[1],
        "prediction_start": row[2].isoformat(),
        "prediction_end": row[3].isoformat(),
    }


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
