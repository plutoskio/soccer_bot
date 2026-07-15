from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping

from soccer_bot.modeling.production import (
    champion_model_sha256,
    load_regulation_champion,
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
    return {
        "status": "uploaded",
        "as_of": snapshot["as_of"],
        "model_version": snapshot["model_version"],
        "snapshot_version": snapshot["snapshot_version"],
        "prediction_rows": len(predictions),
        "fixture_count": len({row["fixture_id"] for row in predictions}),
        "prediction_rows_sha256": snapshot["prediction_rows_sha256"],
    }


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
