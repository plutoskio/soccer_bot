from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .database import json_text


@dataclass(frozen=True)
class HealthReport:
    report_date: date
    severity: str
    metrics: dict[str, object]
    markdown_path: Path | None
    blocking_reason: str | None = None


def generate_health_report(
    connection,
    *,
    config: dict,
    collection_run_id: str,
    now: datetime,
    report_directory: Path | None = None,
    discovery_start_date: date | None = None,
    discovery_end_date: date | None = None,
) -> HealthReport:
    zone = ZoneInfo(config["timezone"])
    local_date = now.astimezone(zone).date()
    discovery = config["discovery"]
    start_date = discovery_start_date or (
        local_date - timedelta(days=int(discovery["recovery_days"]))
    )
    end_date = discovery_end_date or (
        local_date + timedelta(days=int(discovery["planning_days"]))
    )
    expected_dates = [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]
    discovery_rows = connection.execute(
        """
        SELECT job_key,status,metadata,last_attempt_at
        FROM collection_checkpoint
        WHERE source_code='api_football' AND job_type='fixture_discovery'
        """
    ).fetchall()
    success_dates: dict[str, datetime | None] = {}
    failed_dates: set[str] = set()
    for job_key, status, metadata, attempted_at in discovery_rows:
        value = metadata
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        target = value.get("target_date") or value.get("date") if isinstance(value, dict) else None
        if not target:
            parts = str(job_key).split(":")
            target = parts[2] if len(parts) > 2 else None
        if not target:
            continue
        if status == "succeeded":
            previous = success_dates.get(str(target))
            if previous is None or (attempted_at and attempted_at > previous):
                success_dates[str(target)] = attempted_at
        elif status in {"failed", "rate_limited"}:
            failed_dates.add(str(target))
    expected_strings = [value.isoformat() for value in expected_dates]
    missing_dates = [value for value in expected_strings if value not in success_dates]

    lifecycle = dict(connection.execute(
        """
        WITH ranked AS (
          SELECT fixture_id,canonical_status,
                 row_number() OVER (
                   PARTITION BY fixture_id ORDER BY retrieved_at DESC,
                   schedule_observation_id DESC
                 ) AS rank
          FROM fixture_schedule_observation WHERE source_code='api_football'
        )
        SELECT coalesce(r.canonical_status,f.status,'unknown'),count(*)
        FROM fixture f LEFT JOIN ranked r ON r.fixture_id=f.fixture_id AND r.rank=1
        WHERE CAST(timezone(?,f.scheduled_kickoff) AS DATE) BETWEEN ? AND ?
        GROUP BY 1 ORDER BY 1
        """,
        [config["timezone"], start_date, end_date],
    ).fetchall())
    components = {
        code: {state: count for state, count in states}
        for code, states in _group_component_rows(connection).items()
    }
    checkpoints = dict(connection.execute(
        "SELECT status,count(*) FROM collection_checkpoint GROUP BY status ORDER BY status"
    ).fetchall())
    unresolved = connection.execute(
        """
        SELECT count(*) FROM source_entity_map
        WHERE source_code='api_football_lineup' AND entity_type='player'
          AND (review_status IN ('pending','needs_review')
               OR match_method='unresolved_alias' OR confidence <= 0)
        """
    ).fetchone()[0]
    api_day_start = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    api_day_end = api_day_start + timedelta(days=1)
    api_calls = connection.execute(
        """
        SELECT count(*) FROM raw_artifact
        WHERE source_code='api_football' AND retrieved_at>=? AND retrieved_at<?
          AND resource_name!='status'
        """,
        [api_day_start, api_day_end],
    ).fetchone()[0]
    provider_remaining = _provider_remaining(connection, api_day_start, api_day_end)
    market_metrics = connection.execute(
        """
        SELECT
          count(DISTINCT fixture_id) FILTER (WHERE fixture_id IS NOT NULL),
          (SELECT count(*) FROM orderbook_snapshot
           WHERE retrieved_at>=? AND retrieved_at<?),
          (SELECT count(*) FROM orderbook_snapshot
           WHERE retrieved_at>=? AND retrieved_at<? AND cadence_stage IS NOT NULL)
        FROM prediction_market_event
        """,
        [api_day_start, api_day_end, api_day_start, api_day_end],
    ).fetchone()
    metrics = {
        "discovery": {
            "expected_dates": expected_strings,
            "fresh_dates": sorted(set(expected_strings).intersection(success_dates)),
            "missing_dates": missing_dates,
            "failed_dates": sorted(set(expected_strings).intersection(failed_dates)),
            "last_successful_retrieval": (
                max(value for value in success_dates.values() if value).isoformat()
                if any(success_dates.values())
                else None
            ),
        },
        "fixture_lifecycle": lifecycle,
        "components": components,
        "unresolved_player_identities": unresolved,
        "checkpoints": checkpoints,
        "api_football": {
            "calls_used": api_calls,
            "daily_limit": int(config["api_football"]["daily_limit"]),
            "reserve_calls": int(config["api_football"]["reserve_calls"]),
            "inferred_remaining": max(
                0, int(config["api_football"]["daily_limit"]) - api_calls
            ),
            "provider_reported_remaining": provider_remaining,
        },
        "polymarket": {
            "linked_fixtures": market_metrics[0],
            "snapshots_today": market_metrics[1],
            "staged_snapshots_today": market_metrics[2],
        },
    }
    invalid_required_components = dict(
        connection.execute(
            """
            SELECT component_code,count(*) FROM fixture_collection_component
            WHERE state='invalid' AND required_for_fixture_terminal
            GROUP BY component_code ORDER BY component_code
            """
        ).fetchall()
    )
    nonblocking_invalid_components = set(
        config.get("health", {}).get("nonblocking_invalid_components", [])
    )
    publication_blocking_components = {
        component: count
        for component, count in invalid_required_components.items()
        if component not in nonblocking_invalid_components
    }
    metrics["invalid_required_components"] = invalid_required_components
    metrics["publication_blocking_invalid_components"] = (
        publication_blocking_components
    )
    if publication_blocking_components:
        severity = "blocking"
        blocking_reason = "invalid_required_components"
    elif (
        invalid_required_components
        or missing_dates
        or unresolved
        or any(state in checkpoints for state in ("failed", "rate_limited", "incomplete"))
        or any(
            state in {"retryable", "missed", "unavailable"}
            for states in components.values() for state in states
        )
    ):
        severity = "warning"
        blocking_reason = None
    else:
        severity = "healthy"
        blocking_reason = None

    markdown_path = None
    if report_directory is not None:
        report_directory.mkdir(parents=True, exist_ok=True)
        markdown_path = report_directory / f"COLLECTOR_HEALTH_{local_date.isoformat()}.md"
        temporary = markdown_path.with_suffix(".md.tmp")
        temporary.write_text(
            render_health_markdown(local_date, severity, metrics, blocking_reason),
            encoding="utf-8",
        )
        temporary.replace(markdown_path)
    connection.execute(
        """
        INSERT INTO collection_health_report (
            report_date,generated_at,collection_run_id,severity,metrics,
            markdown_path,blocking_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (report_date) DO UPDATE SET
            generated_at=excluded.generated_at,
            collection_run_id=excluded.collection_run_id,
            severity=excluded.severity,
            metrics=excluded.metrics,
            markdown_path=excluded.markdown_path,
            blocking_reason=excluded.blocking_reason
        """,
        [
            local_date, now, collection_run_id, severity, json_text(metrics),
            str(markdown_path) if markdown_path else None, blocking_reason,
        ],
    )
    return HealthReport(
        local_date, severity, metrics, markdown_path, blocking_reason
    )


def _group_component_rows(connection) -> dict[str, list[tuple[str, int]]]:
    grouped: dict[str, list[tuple[str, int]]] = {}
    for code, state, count in connection.execute(
        """
        SELECT component_code,state,count(*)
        FROM fixture_collection_component GROUP BY ALL ORDER BY 1,2
        """
    ).fetchall():
        grouped.setdefault(code, []).append((state, count))
    return grouped


def _provider_remaining(connection, start: datetime, end: datetime) -> int | None:
    rows = connection.execute(
        """
        SELECT response_headers FROM raw_artifact
        WHERE source_code='api_football' AND retrieved_at>=? AND retrieved_at<?
        ORDER BY retrieved_at DESC
        """,
        [start, end],
    ).fetchall()
    for (headers,) in rows:
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                continue
        if not isinstance(headers, dict):
            continue
        for key in (
            "x-ratelimit-requests-remaining", "x-ratelimit-remaining"
        ):
            try:
                return int(headers[key])
            except (KeyError, TypeError, ValueError):
                pass
    return None


def render_health_markdown(
    report_date: date,
    severity: str,
    metrics: dict[str, object],
    blocking_reason: str | None = None,
) -> str:
    discovery = metrics["discovery"]
    api = metrics["api_football"]
    market = metrics["polymarket"]
    lines = [
        f"# Collector health — {report_date.isoformat()}",
        "",
        f"Severity: **{severity}**",
    ]
    if blocking_reason:
        lines.append(f"Blocking reason: `{blocking_reason}`")
    lines.extend([
        "",
        "## Discovery",
        "",
        f"- Expected dates: {len(discovery['expected_dates'])}",
        f"- Fresh dates: {len(discovery['fresh_dates'])}",
        f"- Missing dates: {len(discovery['missing_dates'])}",
        f"- Failed dates: {len(discovery['failed_dates'])}",
        "",
        "## Collection state",
        "",
        f"- Unresolved player identities: {metrics['unresolved_player_identities']}",
        f"- Checkpoints: `{json.dumps(metrics['checkpoints'], sort_keys=True)}`",
        f"- Components: `{json.dumps(metrics['components'], sort_keys=True)}`",
        "- Invalid required components: "
        f"`{json.dumps(metrics['invalid_required_components'], sort_keys=True)}`",
        "- Publication-blocking invalid components: "
        f"`{json.dumps(metrics['publication_blocking_invalid_components'], sort_keys=True)}`",
        "",
        "## Providers",
        "",
        f"- API-Football calls used: {api['calls_used']}",
        f"- API-Football inferred remaining: {api['inferred_remaining']}",
        f"- API-Football protected reserve: {api['reserve_calls']}",
        f"- Linked Polymarket fixtures: {market['linked_fixtures']}",
        f"- Staged Polymarket snapshots today: {market['staged_snapshots_today']}",
        "",
    ])
    return "\n".join(lines)
