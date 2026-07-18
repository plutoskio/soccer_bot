from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping, Sequence


CHAMPION_PREDICTION_HASH_FIELDS = (
    "model_version",
    "fixture_id",
    "information_state",
    "prediction_at",
    "kickoff",
    "competition_id",
    "season_id",
    "home_team_id",
    "away_team_id",
    "expected_home_goals",
    "expected_away_goals",
    "raw_home_win_probability",
    "raw_draw_probability",
    "raw_away_win_probability",
    "home_win_probability",
    "draw_probability",
    "away_win_probability",
    "home_history_matches",
    "away_history_matches",
    "home_xg_history",
    "away_xg_history",
    "home_shots_history",
    "away_shots_history",
    "warnings",
)

CHAMPION_ISSUANCE_HASH_FIELDS = (
    "issued_at",
    "issuance_status",
    "issuance_policy_version",
    "availability_policy_version",
    "immutable_prediction_sha256",
)


def champion_prediction_rows_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    """Hash the stable model-output fields, excluding display-only fixture metadata."""

    values = []
    for index, row in enumerate(rows):
        missing = [
            field for field in CHAMPION_PREDICTION_HASH_FIELDS if field not in row
        ]
        if missing:
            raise ValueError(
                f"Prediction row {index} is missing hash fields: {', '.join(missing)}"
            )
        value = {field: row[field] for field in CHAMPION_PREDICTION_HASH_FIELDS}
        value["prediction_at"] = _canonical_timestamp(
            value["prediction_at"], f"prediction row {index} prediction_at"
        )
        value["kickoff"] = _canonical_timestamp(
            value["kickoff"], f"prediction row {index} kickoff"
        )
        if "source_max_retrieved_at" in row:
            source_retrieved_at = row["source_max_retrieved_at"]
            value["source_max_retrieved_at"] = (
                _canonical_timestamp(
                    source_retrieved_at,
                    f"prediction row {index} source_max_retrieved_at",
                )
                if source_retrieved_at is not None
                else None
            )
        for field in CHAMPION_ISSUANCE_HASH_FIELDS:
            if field in row:
                value[field] = row[field]
        if "issued_at" in value:
            value["issued_at"] = _canonical_timestamp(
                value["issued_at"], f"prediction row {index} issued_at"
            )
        values.append(value)
    body = json.dumps(
        values, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_timestamp(value: object, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be an ISO timestamp") from error
    else:
        raise ValueError(f"{field} must be an ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()
