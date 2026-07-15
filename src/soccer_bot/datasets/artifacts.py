from __future__ import annotations

from collections import Counter
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import duckdb

from soccer_bot.datasets.features import RegulationFeatureRow, feature_rows_sha256


class DatasetArtifactError(RuntimeError):
    """Raised when a frozen modeling dataset cannot be written or verified."""


def write_regulation_feature_artifact(
    rows: list[RegulationFeatureRow],
    *,
    output_dir: Path,
    warehouse_path: Path,
    source_files: dict[str, Path],
) -> dict:
    """Write deterministic feature rows to Parquet and an evidence manifest."""

    if not rows:
        raise DatasetArtifactError("Cannot freeze an empty feature dataset")
    ordered = sorted(
        rows,
        key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
    )
    keys = [(row.fixture_id, row.information_state) for row in ordered]
    if len(keys) != len(set(keys)):
        raise DatasetArtifactError(
            "Feature dataset must contain one row per fixture and information state"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / "features.parquet"
    manifest_path = output_dir / "manifest.json"
    json_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="features-",
        dir=output_dir,
        delete=False,
    )
    json_path = Path(json_handle.name)
    temporary_parquet = output_dir / f".{parquet_path.name}.tmp"
    try:
        with json_handle:
            for row in ordered:
                value = asdict(row)
                value["prediction_at"] = row.prediction_at.timestamp()
                value["kickoff"] = row.kickoff.timestamp()
                json_handle.write(
                    json.dumps(
                        value,
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
                    ORDER BY kickoff, fixture_id, information_state
                ) TO {_sql_literal(temporary_parquet)}
                (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary_parquet, parquet_path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary_parquet.unlink(missing_ok=True)

    verification = _verify_parquet(parquet_path)
    if verification["rows"] != len(ordered):
        raise DatasetArtifactError(
            f"Parquet row count changed: expected={len(ordered)}, "
            f"actual={verification['rows']}"
        )

    warehouse_stat = warehouse_path.stat()
    manifest = {
        "artifact_version": "regulation_team_state_dataset_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_version": ordered[0].feature_version,
        "eligibility_flag": "eligible_result_models",
        "warehouse_snapshot": {
            "path": str(warehouse_path.resolve()),
            "size_bytes": warehouse_stat.st_size,
            "modified_at": datetime.fromtimestamp(
                warehouse_stat.st_mtime, timezone.utc
            ).isoformat(),
        },
        "source_files": {
            name: {
                "path": str(path),
                "sha256": _file_sha256(path),
            }
            for name, path in sorted(source_files.items())
        },
        "dataset": {
            "path": str(parquet_path.resolve()),
            "sha256": _file_sha256(parquet_path),
            "logical_rows_sha256": feature_rows_sha256(ordered),
            "rows": len(ordered),
            "fixtures": len({row.fixture_id for row in ordered}),
            "horizon_rows": dict(
                sorted(Counter(row.information_state for row in ordered).items())
            ),
            "kickoff_start": min(row.kickoff for row in ordered).isoformat(),
            "kickoff_end": max(row.kickoff for row in ordered).isoformat(),
            "columns": [field.name for field in fields(RegulationFeatureRow)],
            "verification": verification,
        },
    }
    _atomic_write_json(manifest_path, manifest)
    return manifest


def read_regulation_feature_artifact(path: Path) -> list[RegulationFeatureRow]:
    """Load a frozen feature Parquet file in its canonical row order."""

    connection = duckdb.connect(":memory:")
    try:
        relation = connection.execute(
            f"""
            SELECT *
            FROM read_parquet({_sql_literal(path)})
            ORDER BY kickoff, fixture_id, information_state
            """
        )
        names = [item[0] for item in relation.description]
        expected = [field.name for field in fields(RegulationFeatureRow)]
        if names != expected:
            raise DatasetArtifactError(
                f"Unexpected feature schema: expected={expected}, actual={names}"
            )
        values = []
        identifier_columns = {
            "feature_version",
            "fixture_id",
            "information_state",
            "competition_id",
            "season_id",
            "home_team_id",
            "away_team_id",
        }
        for row in relation.fetchall():
            value = dict(zip(names, row, strict=True))
            for column in identifier_columns:
                if value[column] is not None:
                    value[column] = str(value[column])
            values.append(RegulationFeatureRow(**value))
        return values
    finally:
        connection.close()


def _verify_parquet(path: Path) -> dict:
    connection = duckdb.connect(":memory:")
    try:
        row = connection.execute(
            f"""
            SELECT
                count(*) AS rows,
                count(DISTINCT fixture_id || '|' || information_state) AS unique_keys,
                min(prediction_at) AS prediction_start,
                max(prediction_at) AS prediction_end
            FROM read_parquet({_sql_literal(path)})
            """
        ).fetchone()
    finally:
        connection.close()
    if row[0] != row[1]:
        raise DatasetArtifactError("Frozen Parquet contains duplicate logical rows")
    return {
        "rows": row[0],
        "unique_keys": row[1],
        "prediction_start": row[2].isoformat(),
        "prediction_end": row[3].isoformat(),
    }


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
