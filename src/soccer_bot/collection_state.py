from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from .database import json_text
from .loaders import canonical_api_football_status


RETRYABLE_CHECKPOINT_STATES = frozenset(
    {"pending", "incomplete", "failed", "rate_limited"}
)
STOPPING_CHECKPOINT_STATES = frozenset(
    {"succeeded", "terminal", "skipped_with_reason", "skipped"}
)

FACT_COMPONENTS = (
    "result",
    "lineups",
    "team_statistics",
    "player_statistics",
    "events",
    "identity_linking",
)

REQUIRED_FACT_COMPONENTS = (
    "result",
    "lineups",
    "team_statistics",
    "player_statistics",
    "events",
)

COMPONENT_REQUIRED_FOR_TERMINAL = {
    "result": True,
    "lineups": True,
    "team_statistics": True,
    "player_statistics": True,
    "events": True,
    "identity_linking": False,
    "pregame_lineup_capture": False,
    "pregame_market_capture": False,
    "correction_refresh_24h": True,
    "correction_refresh_72h": True,
}


@dataclass(frozen=True)
class ValidationResult:
    state: str
    reason_code: str | None
    details: dict[str, Any]
    last_raw_artifact_id: str | None = None


def checkpoint_is_stopping(status: str | None) -> bool:
    return status in STOPPING_CHECKPOINT_STATES


def checkpoint_is_retryable(status: str | None) -> bool:
    return status in RETRYABLE_CHECKPOINT_STATES


def component_is_terminally_done(state: str | None) -> bool:
    return state in {"complete", "unavailable", "missed", "terminal"}


def component_for_job_type(job_type: str) -> str | None:
    if job_type in {"correction_refresh_24h", "correction_refresh_72h"}:
        return job_type
    if job_type == "lineup_snapshot":
        return "pregame_lineup_capture"
    if job_type in {"prekick_snapshot", "market_snapshot"}:
        return "pregame_market_capture"
    if job_type == "market_after_lineup" or job_type.startswith("market_t_minus_"):
        return "pregame_market_capture"
    if job_type.startswith("lineup"):
        return "lineups"
    if job_type.startswith("postmatch"):
        return None
    return None


def _fixture_context(connection, fixture_id: str) -> tuple[str, str, str, datetime | None]:
    row = connection.execute(
        """
        SELECT home_team_id, away_team_id, coalesce(status, ''), scheduled_kickoff
        FROM fixture WHERE fixture_id = ?
        """,
        [fixture_id],
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown fixture: {fixture_id}")
    return row[0], row[1], row[2], row[3]


def latest_fixture_status(connection, fixture_id: str, source_code: str) -> str:
    row = connection.execute(
        """
        SELECT canonical_status
        FROM fixture_schedule_observation
        WHERE fixture_id = ? AND source_code = ?
        ORDER BY retrieved_at DESC, schedule_observation_id DESC
        LIMIT 1
        """,
        [fixture_id, source_code],
    ).fetchone()
    if row:
        return row[0]

    row = connection.execute(
        "SELECT coalesce(status, '') FROM fixture WHERE fixture_id = ?",
        [fixture_id],
    ).fetchone()
    status = row[0] if row else ""
    if status == "completed":
        return "final"
    if status == "administrative_result_unplayed":
        return "administrative_result"
    return canonical_api_football_status(status)


def _postmatch_started(
    status: str, kickoff: datetime | None, now: datetime | None
) -> bool:
    if status in {"final", "postponed", "cancelled", "abandoned", "administrative_result"}:
        return True
    if kickoff is None or now is None:
        return status in {"live", "delayed", "suspended", "unknown"}
    return now.astimezone(timezone.utc) >= kickoff.astimezone(timezone.utc)


def _result(
    state: str,
    reason_code: str | None,
    details: dict[str, Any],
    raw_artifact_id: str | None = None,
) -> ValidationResult:
    return ValidationResult(state, reason_code, details, raw_artifact_id)


def _unavailable_evidence(
    connection, fixture_id: str, rule_code: str
) -> ValidationResult | None:
    row = connection.execute(
        """
        SELECT raw_artifact_id, details
        FROM data_quality_issue
        WHERE internal_entity_id = ? AND rule_code = ? AND status = 'open'
        ORDER BY detected_at DESC, issue_id DESC
        LIMIT 1
        """,
        [fixture_id, rule_code],
    ).fetchone()
    if not row:
        return None
    details = row[1]
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}
    if not isinstance(details, dict):
        details = {}
    return _result("unavailable", rule_code, details, row[0])


def validate_result(
    connection, fixture_id: str, source_code: str = "api_football"
) -> ValidationResult:
    _, _, fixture_status, _ = _fixture_context(connection, fixture_id)
    status = latest_fixture_status(connection, fixture_id, source_code)
    if status in {"postponed", "cancelled", "abandoned", "administrative_result"}:
        return _result("terminal", status, {"canonical_status": status})

    rows = connection.execute(
        """
        SELECT home_score_regulation, away_score_regulation, result_status,
               raw_artifact_id, retrieved_at
        FROM fixture_result_observation
        WHERE fixture_id = ? AND source_code = ?
        ORDER BY retrieved_at DESC, observation_id DESC
        """,
        [fixture_id, source_code],
    ).fetchall()
    valid = next(
        (
            row for row in rows
            if row[2] == "final"
            and row[0] is not None and row[1] is not None
            and row[0] >= 0 and row[1] >= 0
        ),
        None,
    )
    final_status = status == "final" or fixture_status == "completed"
    if valid and final_status:
        return _result(
            "complete",
            None,
            {
                "canonical_status": status,
                "home_score_regulation": valid[0],
                "away_score_regulation": valid[1],
            },
            valid[3],
        )
    if rows and final_status:
        return _result(
            "invalid",
            "invalid_final_result",
            {"canonical_status": status, "result_rows": len(rows)},
        )
    if final_status:
        return _result(
            "retryable",
            "missing_final_result",
            {"canonical_status": status, "result_rows": len(rows)},
        )
    return _result(
        "pending",
        "waiting_for_final",
        {"canonical_status": status, "result_rows": len(rows)},
    )


def validate_lineups(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
    now: datetime | None = None,
    schedule_observation_id: str | None = None,
    schedule_kickoff: datetime | None = None,
) -> ValidationResult:
    home_team_id, away_team_id, fixture_status, kickoff = _fixture_context(
        connection, fixture_id
    )
    status = latest_fixture_status(connection, fixture_id, source_code)
    lineup_query = """
        SELECT ls.raw_artifact_id, ls.team_id,
               bool_and(ls.is_complete),
               count(*) FILTER (WHERE lp.selection_role = 'starter'),
               count(DISTINCT lp.player_id) FILTER (
                   WHERE lp.selection_role = 'starter'
               )
        FROM lineup_snapshot ls
        LEFT JOIN lineup_player lp USING (lineup_snapshot_id)
        WHERE ls.fixture_id = ? AND ls.source_code = ?
          AND ls.lineup_type = 'confirmed'
          AND ls.raw_artifact_id IS NOT NULL
    """
    lineup_params: list[object] = [fixture_id, source_code]
    if schedule_observation_id is not None:
        lineup_query += " AND ls.schedule_observation_id = ?"
        lineup_params.append(schedule_observation_id)
    if schedule_kickoff is not None:
        lineup_query += """
            AND EXISTS (
                SELECT 1
                FROM fixture_schedule_observation fso
                WHERE fso.schedule_observation_id = ls.schedule_observation_id
                  AND fso.scheduled_kickoff = ?
            )
        """
        lineup_params.append(schedule_kickoff)
    lineup_query += """
        GROUP BY ls.raw_artifact_id, ls.team_id
        ORDER BY ls.raw_artifact_id, ls.team_id
    """
    rows = connection.execute(lineup_query, lineup_params).fetchall()
    if not rows:
        state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
        return _result(state, "missing_lineups", {"artifacts": 0})

    artifact_rows: dict[str, list[tuple]] = {}
    for row in rows:
        artifact_rows.setdefault(row[0], []).append(row)
    duplicate_query = """
            SELECT ls.raw_artifact_id, lp.player_id
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            WHERE ls.fixture_id = ? AND ls.source_code = ?
              AND ls.lineup_type = 'confirmed'
              AND ls.raw_artifact_id IS NOT NULL
              AND lp.selection_role = 'starter'
    """
    duplicate_params: list[object] = [fixture_id, source_code]
    if schedule_observation_id is not None:
        duplicate_query += " AND ls.schedule_observation_id = ?"
        duplicate_params.append(schedule_observation_id)
    if schedule_kickoff is not None:
        duplicate_query += """
              AND EXISTS (
                  SELECT 1
                  FROM fixture_schedule_observation fso
                  WHERE fso.schedule_observation_id = ls.schedule_observation_id
                    AND fso.scheduled_kickoff = ?
              )
        """
        duplicate_params.append(schedule_kickoff)
    duplicate_query += """
            GROUP BY ls.raw_artifact_id, lp.player_id
            HAVING count(DISTINCT ls.team_id) > 1
    """
    cross_team_duplicates = {
        row[0]
        for row in connection.execute(duplicate_query, duplicate_params).fetchall()
    }
    expected_teams = {home_team_id, away_team_id}
    invalid_artifacts = 0
    for artifact_id, artifact in artifact_rows.items():
        represented = {row[1] for row in artifact}
        valid = (
            represented == expected_teams
            and artifact_id not in cross_team_duplicates
            and all(
                row[2] and row[3] == 11 and row[4] == 11
                for row in artifact
            )
        )
        if valid:
            return _result(
                "complete",
                None,
                {
                    "artifact_count": len(artifact_rows),
                    "represented_teams": 2,
                    "starter_counts": {team: 11 for team in expected_teams},
                },
                artifact_id,
            )
        invalid_artifacts += 1

    state = "invalid" if invalid_artifacts else "retryable"
    reason = "invalid_lineups" if state == "invalid" else "missing_complete_lineups"
    return _result(
        state,
        reason,
        {"artifact_count": len(artifact_rows), "invalid_artifacts": invalid_artifacts},
    )


def validate_team_statistics(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
    now: datetime | None = None,
) -> ValidationResult:
    home_team_id, away_team_id, status, kickoff = _fixture_context(connection, fixture_id)
    rows = connection.execute(
        """
        SELECT raw_artifact_id, team_id, count(*),
               min(shots), min(shots_on_target), min(corners),
               min(possession_pct), min(passes), min(accurate_passes)
        FROM team_match_stat_observation
        WHERE fixture_id = ? AND source_code = ? AND period = 'regulation'
          AND raw_artifact_id IS NOT NULL
        GROUP BY raw_artifact_id, team_id
        ORDER BY raw_artifact_id, team_id
        """,
        [fixture_id, source_code],
    ).fetchall()
    unavailable = _unavailable_evidence(
        connection, fixture_id, "api_team_stats_unavailable"
    )
    if not rows:
        if unavailable:
            return unavailable
        state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
        return _result(state, "missing_team_statistics", {"artifacts": 0})

    expected_teams = {home_team_id, away_team_id}
    by_artifact: dict[str, list[tuple]] = {}
    for row in rows:
        by_artifact.setdefault(row[0], []).append(row)
    invalid = 0
    incomplete = 0
    for artifact_id, artifact in by_artifact.items():
        represented = {row[1] for row in artifact}
        if represented != expected_teams or len(artifact) != 2:
            invalid += 1
            continue
        artifact_valid = True
        for row in artifact:
            _, _, block_count, shots, shots_on_target, corners, possession, passes, accurate = row
            if block_count != 1:
                artifact_valid = False
                invalid += 1
                break
            if shots is None or shots_on_target is None or corners is None:
                artifact_valid = False
                incomplete += 1
                break
            if shots < 0 or shots_on_target < 0 or corners < 0:
                artifact_valid = False
                invalid += 1
                break
            if shots_on_target > shots:
                artifact_valid = False
                invalid += 1
                break
            if possession is not None and not 0 <= possession <= 100:
                artifact_valid = False
                invalid += 1
                break
            if passes is not None and passes < 0:
                artifact_valid = False
                invalid += 1
                break
            if accurate is not None and accurate < 0:
                artifact_valid = False
                invalid += 1
                break
            if passes is not None and accurate is not None and accurate > passes:
                artifact_valid = False
                invalid += 1
                break
        if artifact_valid:
            return _result(
                "complete",
                None,
                {"artifact_count": len(by_artifact), "represented_teams": 2},
                artifact_id,
            )

    if invalid:
        return _result(
            "invalid",
            "invalid_team_statistics",
            {"artifact_count": len(by_artifact), "invalid_artifacts": invalid},
        )
    if unavailable:
        return unavailable
    state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
    return _result(
        state,
        "incomplete_team_statistics",
        {"artifact_count": len(by_artifact), "incomplete_artifacts": incomplete},
    )


def _player_row_invalid(row: tuple) -> bool:
    (
        _, _, _, minutes, goals, assists, shots, shots_on_target, passes,
        accurate_passes, pass_accuracy, rating, tackles, interceptions, duels,
        duels_won, dribbles_attempted, dribbles_successful, fouls_drawn,
        fouls_committed,
    ) = row
    if minutes is not None and (minutes < 0 or minutes > 130):
        return True
    for value in (
        goals, assists, shots, shots_on_target, passes, accurate_passes,
        tackles, interceptions, duels, duels_won, dribbles_attempted,
        dribbles_successful, fouls_drawn, fouls_committed,
    ):
        if value is not None and value < 0:
            return True
    if shots is not None and shots_on_target is not None and shots_on_target > shots:
        return True
    if passes is not None and accurate_passes is not None and accurate_passes > passes:
        return True
    if pass_accuracy is not None and not 0 <= pass_accuracy <= 100:
        return True
    if rating is not None and not 0 <= rating <= 10:
        return True
    if duels is not None and duels_won is not None and duels_won > duels:
        return True
    if (
        dribbles_attempted is not None
        and dribbles_successful is not None
        and dribbles_successful > dribbles_attempted
    ):
        return True
    return False


def validate_player_statistics(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
    now: datetime | None = None,
) -> ValidationResult:
    home_team_id, away_team_id, status, kickoff = _fixture_context(connection, fixture_id)
    rows = connection.execute(
        """
        SELECT raw_artifact_id, team_id, player_id, minutes_played,
               goals, assists, shots, shots_on_target, passes, accurate_passes,
               pass_accuracy_pct, rating, tackles, interceptions, duels, duels_won,
               dribbles_attempted, dribbles_successful, fouls_drawn, fouls_committed
        FROM player_match_stat_observation
        WHERE fixture_id = ? AND source_code = ? AND raw_artifact_id IS NOT NULL
        ORDER BY raw_artifact_id, player_id
        """,
        [fixture_id, source_code],
    ).fetchall()
    unavailable = _unavailable_evidence(
        connection, fixture_id, "api_player_stats_unavailable"
    )
    if not rows:
        if unavailable:
            return unavailable
        state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
        return _result(state, "missing_player_statistics", {"artifacts": 0})

    expected_teams = {home_team_id, away_team_id}
    by_artifact: dict[str, list[tuple]] = {}
    for row in rows:
        by_artifact.setdefault(row[0], []).append(row)
    invalid_artifacts = 0
    incomplete_artifacts = 0
    for artifact_id, artifact in by_artifact.items():
        teams = {row[1] for row in artifact}
        player_ids = [row[2] for row in artifact]
        participants = [row for row in artifact if row[3] is not None and row[3] > 0]
        invalid = any(_player_row_invalid(row) for row in artifact)
        duplicate_players = len(player_ids) != len(set(player_ids))
        wrong_team = not teams.issubset(expected_teams)
        participant_teams = {row[1] for row in participants}
        if invalid or duplicate_players or wrong_team:
            invalid_artifacts += 1
            continue
        if len(participants) < 22 or participant_teams != expected_teams:
            incomplete_artifacts += 1
            continue
        return _result(
            "complete",
            None,
            {
                "artifact_count": len(by_artifact),
                "participants": len(participants),
                "participant_teams": len(participant_teams),
            },
            artifact_id,
        )

    if invalid_artifacts:
        return _result(
            "invalid",
            "invalid_player_statistics",
            {"artifact_count": len(by_artifact), "invalid_artifacts": invalid_artifacts},
        )
    if unavailable:
        return unavailable
    state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
    return _result(
        state,
        "incomplete_player_statistics",
        {"artifact_count": len(by_artifact), "incomplete_artifacts": incomplete_artifacts},
    )


def events_processing_result(
    *,
    processed: bool,
    event_count: int | None,
    invalid_event_count: int = 0,
    raw_artifact_id: str | None = None,
) -> ValidationResult:
    if invalid_event_count:
        return _result(
            "invalid",
            "invalid_events",
            {
                "processed": processed,
                "event_count": event_count,
                "invalid_event_count": invalid_event_count,
            },
            raw_artifact_id,
        )
    if processed:
        return _result(
            "complete",
            None,
            {"processed": True, "event_count": event_count or 0},
            raw_artifact_id,
        )
    return _result("retryable", "events_not_processed", {"processed": False})


def validate_events(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
    now: datetime | None = None,
) -> ValidationResult:
    row = connection.execute(
        """
        SELECT state, details, last_raw_artifact_id
        FROM fixture_collection_component
        WHERE fixture_id = ? AND source_code = ? AND component_code = 'events'
        """,
        [fixture_id, source_code],
    ).fetchone()
    if row and row[1]:
        details = row[1]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {}
        if isinstance(details, dict) and details.get("processed"):
            return events_processing_result(
                processed=True,
                event_count=details.get("event_count", 0),
                invalid_event_count=details.get("invalid_event_count", 0),
                raw_artifact_id=row[2],
            )
        if row[0] == "invalid":
            return _result("invalid", "invalid_events", details or {}, row[2])
        if row[0] == "retryable":
            return _result("retryable", "events_not_processed", details or {}, row[2])
    _, _, status, kickoff = _fixture_context(connection, fixture_id)
    state = "retryable" if _postmatch_started(status, kickoff, now) else "pending"
    return _result(state, "events_not_processed", {"processed": False})


def validate_identity_linking(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
) -> ValidationResult:
    fixture_source = connection.execute(
        """
        SELECT source_entity_id FROM source_entity_map
        WHERE source_code = ? AND entity_type = 'fixture'
          AND internal_entity_id = ?
        LIMIT 1
        """,
        [source_code, fixture_id],
    ).fetchone()
    if not fixture_source:
        return _result("pending", "fixture_source_identity_missing", {})
    lineup_count = connection.execute(
        """
        SELECT count(*) FROM lineup_snapshot
        WHERE fixture_id = ? AND source_code = ? AND lineup_type = 'confirmed'
        """,
        [fixture_id, source_code],
    ).fetchone()[0]
    if not lineup_count:
        return _result("pending", "lineups_not_available", {})
    prefix = f"{fixture_source[0]}|%"
    rows = connection.execute(
        """
        SELECT review_status, match_method, confidence
        FROM source_entity_map
        WHERE source_code = 'api_football_lineup'
          AND entity_type = 'player'
          AND source_entity_id LIKE ?
        """,
        [prefix],
    ).fetchall()
    unresolved = [
        row for row in rows
        if row[0] in {"pending", "needs_review"}
        or row[1] == "unresolved_alias"
        or (row[2] is not None and row[2] <= 0)
    ]
    details = {"linked": len(rows) - len(unresolved), "unresolved": len(unresolved)}
    if unresolved:
        status = latest_fixture_status(connection, fixture_id, source_code)
        state = "terminal" if status in {
            "final", "cancelled", "abandoned", "administrative_result"
        } else "retryable"
        return _result(state, "unresolved_identity_warning", details)
    return _result("complete", None, details)


def validate_correction_refresh(
    *, request_succeeded: bool, facts_revalidated: bool
) -> ValidationResult:
    if request_succeeded and facts_revalidated:
        return _result("complete", None, {"facts_revalidated": True})
    if request_succeeded:
        return _result("retryable", "correction_facts_not_revalidated", {})
    return _result("retryable", "correction_request_failed", {})


def _persist_component(
    connection,
    *,
    fixture_id: str,
    source_code: str,
    component_code: str,
    result: ValidationResult,
    now: datetime,
    attempted: bool = False,
    required_for_fixture_terminal: bool | None = None,
) -> None:
    existing = connection.execute(
        """
        SELECT state, reason_code, details, last_raw_artifact_id
        FROM fixture_collection_component
        WHERE fixture_id = ? AND source_code = ? AND component_code = ?
        """,
        [fixture_id, source_code, component_code],
    ).fetchone()
    if existing and existing[0] == "unavailable" and result.state in {
        "pending", "retryable"
    }:
        details = existing[2]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {}
        result = ValidationResult(
            "unavailable",
            existing[1],
            details if isinstance(details, dict) else {},
            existing[3],
        )
    required = COMPONENT_REQUIRED_FOR_TERMINAL.get(component_code, True)
    if required_for_fixture_terminal is not None:
        required = required_for_fixture_terminal
    first_attempt = now if attempted else None
    last_attempt = now if attempted else None
    validated = now if component_is_terminally_done(result.state) else None
    connection.execute(
        """
        INSERT INTO fixture_collection_component (
            fixture_id, source_code, component_code, state,
            required_for_fixture_terminal, reason_code, details,
            first_attempt_at, last_attempt_at, validated_at,
            last_raw_artifact_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (fixture_id, source_code, component_code) DO UPDATE SET
            state = excluded.state,
            required_for_fixture_terminal = excluded.required_for_fixture_terminal,
            reason_code = excluded.reason_code,
            details = excluded.details,
            first_attempt_at = coalesce(
                fixture_collection_component.first_attempt_at,
                excluded.first_attempt_at
            ),
            last_attempt_at = coalesce(
                excluded.last_attempt_at,
                fixture_collection_component.last_attempt_at
            ),
            validated_at = excluded.validated_at,
            last_raw_artifact_id = coalesce(
                excluded.last_raw_artifact_id,
                fixture_collection_component.last_raw_artifact_id
            ),
            updated_at = excluded.updated_at
        """,
        [
            fixture_id,
            source_code,
            component_code,
            result.state,
            required,
            result.reason_code,
            json_text(result.details),
            first_attempt,
            last_attempt,
            validated,
            result.last_raw_artifact_id,
            now,
        ],
    )


def reconcile_fixture_components(
    connection,
    fixture_id: str,
    source_code: str = "api_football",
    now: datetime | None = None,
) -> dict[str, ValidationResult]:
    now = now or datetime.now(timezone.utc)
    results = {
        "result": validate_result(connection, fixture_id, source_code),
        "lineups": validate_lineups(connection, fixture_id, source_code, now),
        "team_statistics": validate_team_statistics(connection, fixture_id, source_code, now),
        "player_statistics": validate_player_statistics(connection, fixture_id, source_code, now),
        "events": validate_events(connection, fixture_id, source_code, now),
        "identity_linking": validate_identity_linking(connection, fixture_id, source_code),
    }
    for component_code, result in results.items():
        _persist_component(
            connection,
            fixture_id=fixture_id,
            source_code=source_code,
            component_code=component_code,
            result=result,
            now=now,
            required_for_fixture_terminal=component_code != "identity_linking",
        )
    return results


def record_component_result(
    connection,
    *,
    fixture_id: str,
    source_code: str,
    component_code: str,
    result: ValidationResult,
    now: datetime,
) -> None:
    _persist_component(
        connection,
        fixture_id=fixture_id,
        source_code=source_code,
        component_code=component_code,
        result=result,
        now=now,
        attempted=True,
        required_for_fixture_terminal=component_code != "identity_linking",
    )
