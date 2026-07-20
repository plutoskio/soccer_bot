from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Iterable
import uuid
from zoneinfo import ZoneInfo

from .collection_state import (
    ValidationResult,
    checkpoint_is_stopping,
    component_for_job_type,
    component_is_terminally_done,
    events_processing_result,
    latest_fixture_status,
    reconcile_fixture_components,
    record_component_result,
    validate_lineups,
    validate_events,
    validate_player_statistics,
    validate_result,
    validate_team_statistics,
)
from .collection_planner import (
    DiscoveryJob,
    discovery_date_from_checkpoint,
    discovery_date_window,
    discovery_job_for_date,
    fixture_refresh_job_key,
    lineup_stage_plans,
    market_stage_plans,
    postmatch_stage_plans,
    validate_collector_config,
)
from .database import Warehouse, json_text, normalized_name
from .http import HttpClient, HttpResponse
from .health import generate_health_report
from .loaders import RawCatalog, WarehouseLoader, metadata_artifact_id, parse_datetime
from .raw_store import RawArtifactStore
from .polymarket_contracts import (
    load_polymarket_contract_policy,
    refresh_polymarket_contract_mappings,
)
from .request_executor import (
    ProviderResponseError,
    RequestExecutionError,
    RequestExecutor,
)


FINAL_STATUSES = {"completed", "FT", "AET", "PEN"}


def chunks(values: list, size: int) -> Iterable[list]:
    if size <= 0:
        raise ValueError("Batch size must be positive")
    for index in range(0, len(values), size):
        yield values[index:index + size]


def lineup_stage(
    *,
    now: datetime,
    kickoff: datetime,
    lineup_complete: bool,
    primary_attempted: bool,
    retry_attempted: bool,
    first_check_minutes: int,
    retry_minutes: int,
) -> str | None:
    if lineup_complete or now >= kickoff:
        return None
    minutes_until = (kickoff - now).total_seconds() / 60
    if minutes_until > first_check_minutes:
        return None
    if not primary_attempted:
        return "lineup_primary"
    if minutes_until <= retry_minutes and not retry_attempted:
        return "lineup_retry"
    return None


def postmatch_stage(
    *,
    now: datetime,
    kickoff: datetime,
    data_complete: bool,
    primary_attempted: bool,
    retry_attempted: bool,
    first_check_minutes: int,
    retry_minutes: int,
) -> str | None:
    if data_complete:
        return None
    elapsed = (now - kickoff).total_seconds() / 60
    if elapsed < first_check_minutes:
        return None
    if not primary_attempted:
        return "postmatch_primary"
    if elapsed >= retry_minutes and not retry_attempted:
        return "postmatch_retry"
    return None


@dataclass(frozen=True)
class FixtureRecord:
    internal_id: str
    source_id: str
    kickoff: datetime
    status: str | None
    home_name: str
    away_name: str


@dataclass(frozen=True)
class DetailJob:
    job_key: str
    job_type: str
    fixture: FixtureRecord
    scheduled_for: datetime
    schedule_version: str | None = None
    schedule_observation_id: str | None = None
    capture_target_at: datetime | None = None
    capture_deadline_at: datetime | None = None


@dataclass(frozen=True)
class FixtureRefreshJob:
    job_key: str
    fixture: FixtureRecord
    reason: str
    slot: str
    scheduled_for: datetime


class Collector:
    API_FOOTBALL_URL = "https://v3.football.api-sports.io"
    POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

    def __init__(
        self,
        *,
        warehouse: Warehouse,
        raw_store: RawArtifactStore,
        http_client: HttpClient,
        api_key: str,
        config: dict,
        report_directory: Path | None = None,
    ) -> None:
        self.warehouse = warehouse
        self.connection = warehouse.connection
        self.raw_store = raw_store
        self.http = http_client
        self.api_key = api_key
        self.config = config
        self.report_directory = report_directory
        self.zone = ZoneInfo(config["timezone"])
        self.discovery_config = config.get("discovery", {})
        self.api_config = config["api_football"]
        self.polymarket_config = config["polymarket"]
        retry_config = config.get("retry", {})
        self.request_executor = RequestExecutor(
            maximum_attempts=int(retry_config.get("maximum_inline_attempts", 3)),
            maximum_inline_retry_seconds=float(
                retry_config.get("maximum_inline_retry_seconds", 5)
            ),
            backoff_base_seconds=float(retry_config.get("backoff_base_seconds", 1)),
            backoff_cap_seconds=float(retry_config.get("backoff_cap_seconds", 60)),
            jitter_seconds=float(retry_config.get("jitter_seconds", 0.25)),
        )
        competitions = config["competitions"]
        self.monitored_league_ids = {str(value) for value in competitions["league_ids"]}
        self.monitored_competition_keys = {
            self._competition_key(*value.split("|", 1))
            for value in competitions["competition_keys"]
        }
        identity_config = config.get("identity", {})
        self.loader = WarehouseLoader(
            warehouse,
            RawCatalog.__new__(RawCatalog),
            recent_team_lookback_days=int(
                identity_config.get("recent_team_lookback_days", 730)
            ),
            recent_team_max_candidates=int(
                identity_config.get("recent_team_max_candidates", 50)
            ),
        )
        self.last_api_request_at: float | None = None
        self.api_calls = 0
        self.polymarket_calls = 0
        self.current_run_id: str | None = None
        self.summary: dict[str, object] = {
            "planned_jobs": [],
            "executed_jobs": [],
            "selected_fixtures": 0,
            "linked_polymarket_events": 0,
        }

    def run(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        catch_up_days: int | None = None,
    ) -> dict[str, object]:
        validate_collector_config(self.config, catch_up_days)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        run_id = str(uuid.uuid4())
        self.current_run_id = run_id
        if not dry_run:
            self.connection.execute(
                """
                INSERT INTO collection_run (
                    collection_run_id, started_at, status, dry_run
                ) VALUES (?, ?, 'running', false)
                """,
                [run_id, now],
            )
        try:
            local_date = now.astimezone(self.zone).date()
            start_date, end_date, target_dates, past_days = self._discovery_window(
                local_date, catch_up_days
            )
            self.summary["discovery_window"] = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "past_days": past_days,
                "planning_days": (end_date - local_date).days,
            }
            discovery_jobs = self._plan_discovery_jobs(
                target_dates, local_date, now
            )
            self._execute_discovery_jobs(discovery_jobs, now, dry_run)
            fixtures = self._monitored_fixtures(
                local_date, start_date=start_date, end_date=end_date
            )
            fixture_refresh_jobs = self._plan_fixture_refresh_jobs(fixtures, now)
            self._execute_fixture_refresh_jobs(fixture_refresh_jobs, now, dry_run)
            if fixture_refresh_jobs and not dry_run:
                fixtures = self._monitored_fixtures(
                    local_date, start_date=start_date, end_date=end_date
                )
            self.summary["selected_fixtures"] = len(fixtures)
            self._discover_polymarket(local_date, fixtures, now, dry_run)
            if not dry_run:
                self._reconcile_fixture_components(fixtures, now)
                self._mark_expired_pregame_components(fixtures, now)
            detail_jobs = self._plan_detail_jobs(
                fixtures, now, mutate_state=not dry_run
            )
            self.summary["planned_jobs"].extend(job.job_key for job in detail_jobs)
            if not dry_run:
                self._execute_detail_jobs(detail_jobs, now)
                self._reconcile_fixture_components(fixtures, now)
                self._mark_expired_pregame_components(fixtures, now)
                self.summary["linked_polymarket_events"] = self._link_polymarket_events(fixtures)
                policy_path = (
                    Path(__file__).resolve().parents[2]
                    / self.polymarket_config["mapping_policy_path"]
                )
                mapping_policy, mapping_hash = load_polymarket_contract_policy(
                    policy_path
                )
                self.summary["polymarket_contract_mappings"] = (
                    refresh_polymarket_contract_mappings(
                        self.connection,
                        policy=mapping_policy,
                        policy_sha256=mapping_hash,
                        team_aliases=self.warehouse.team_aliases,
                        mapped_at=now,
                    )
                )
            self._refresh_closed_polymarket_events(fixtures, now, dry_run=dry_run)
            market_jobs = self._plan_market_jobs(fixtures, now)
            self.summary["planned_jobs"].extend(job.job_key for job in market_jobs)
            if not dry_run:
                # Keep audited cutoff captures and display-only live captures in
                # separate payloads so one token can never inherit the wrong
                # cadence metadata when both are due in the same cycle.
                self._execute_market_jobs(
                    [job for job in market_jobs if job.job_type != "market_live"],
                    now,
                )
                self._execute_market_jobs(
                    [job for job in market_jobs if job.job_type == "market_live"],
                    now,
                )
            self.summary["api_football_calls"] = self.api_calls
            self.summary["polymarket_calls"] = self.polymarket_calls
            if not dry_run:
                finished_at = datetime.now(timezone.utc)
                self.connection.execute(
                    """
                    UPDATE collection_run SET finished_at = ?, status = 'completed',
                        api_football_calls = ?, polymarket_calls = ?, summary = ?
                    WHERE collection_run_id = ?
                    """,
                    [finished_at, self.api_calls, self.polymarket_calls,
                     json_text(self.summary), run_id],
                )
                health = generate_health_report(
                    self.connection,
                    config=self.config,
                    collection_run_id=run_id,
                    now=finished_at,
                    report_directory=self.report_directory,
                    discovery_start_date=date.fromisoformat(
                        self.summary["discovery_window"]["start_date"]
                    ),
                    discovery_end_date=date.fromisoformat(
                        self.summary["discovery_window"]["end_date"]
                    ),
                )
                self.summary["health"] = {
                    "report_date": health.report_date.isoformat(),
                    "severity": health.severity,
                    "blocking_reason": health.blocking_reason,
                    "invalid_required_components": health.metrics.get(
                        "invalid_required_components", {}
                    ),
                    "publication_blocking_invalid_components": health.metrics.get(
                        "publication_blocking_invalid_components", {}
                    ),
                    "markdown_path": (
                        str(health.markdown_path) if health.markdown_path else None
                    ),
                }
                self.connection.execute(
                    "UPDATE collection_run SET summary=? WHERE collection_run_id=?",
                    [json_text(self.summary), run_id],
                )
            return self.summary
        except Exception as error:
            if not dry_run:
                self.connection.execute(
                    """
                    UPDATE collection_run SET finished_at = ?, status = 'failed',
                        api_football_calls = ?, polymarket_calls = ?, summary = ?,
                        error_message = ? WHERE collection_run_id = ?
                    """,
                    [datetime.now(timezone.utc), self.api_calls, self.polymarket_calls,
                     json_text(self.summary), f"{type(error).__name__}: {error}", run_id],
                )
            raise

    def _reconcile_fixture_components(
        self, fixtures: list[FixtureRecord], now: datetime
    ) -> None:
        for fixture in fixtures:
            results = reconcile_fixture_components(
                self.connection, fixture.internal_id, "api_football", now
            )
            self._reopen_checkpoint_fact_mismatches(fixture, results, now)

    def _reopen_checkpoint_fact_mismatches(
        self, fixture: FixtureRecord, results: dict[str, object], now: datetime
    ) -> None:
        rows = self.connection.execute(
            """
            SELECT job_key, status, job_type, metadata
            FROM collection_checkpoint
            WHERE fixture_id = ? OR fixture_source_id = ?
            """,
            [fixture.internal_id, fixture.source_id],
        ).fetchall()
        for job_key, status, job_type, metadata in rows:
            component = component_for_job_type(job_type)
            if component and component.startswith("correction_refresh_"):
                component_row = self.connection.execute(
                    """
                    SELECT state FROM fixture_collection_component
                    WHERE fixture_id=? AND source_code='api_football'
                      AND component_code=?
                    """,
                    [fixture.internal_id, component],
                ).fetchone()
                component_done = bool(
                    component_row
                    and component_is_terminally_done(component_row[0])
                )
                if component_done and status in {
                    "pending", "incomplete", "failed", "rate_limited"
                }:
                    self.connection.execute(
                        """
                        UPDATE collection_checkpoint
                        SET status='succeeded',completed_at=coalesce(completed_at,?),
                            next_attempt_at=NULL,terminal_reason=NULL,
                            last_error=NULL,last_run_id=?,updated_at=?
                        WHERE job_key=?
                        """,
                        [now, self.current_run_id, now, job_key],
                    )
                    continue
            if status != "succeeded":
                continue
            if component:
                if component and component.startswith("correction_refresh_"):
                    mismatch = not component_done
                else:
                    component_result = results.get(component)
                    mismatch = not component_result or not component_is_terminally_done(
                        component_result.state
                    )
            elif job_type.startswith("postmatch"):
                mismatch = any(
                    not component_is_terminally_done(results[name].state)
                    for name in (
                        "result", "lineups", "team_statistics",
                        "player_statistics", "events",
                    )
                )
            else:
                continue
            if not mismatch:
                continue
            details = metadata
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except json.JSONDecodeError:
                    details = {}
            if not isinstance(details, dict):
                details = {}
            details["checkpoint_fact_mismatch"] = True
            self.connection.execute(
                """
                UPDATE collection_checkpoint
                SET status = 'incomplete', completed_at = NULL,
                    next_attempt_at = ?, terminal_reason = NULL,
                    last_error = 'checkpoint_fact_mismatch',
                    metadata = ?, last_run_id = ?, updated_at = ?
                WHERE job_key = ?
                """,
                [now, json_text(details), self.current_run_id, now, job_key],
            )

    def _begin_attempts(
        self, jobs: list[DetailJob], source: str, now: datetime, metadata: dict
    ) -> dict[str, str]:
        attempt_ids: dict[str, str] = {}
        for job in jobs:
            attempt_id = self._begin_attempt_record(
                job.job_key,
                source,
                job.job_type,
                job.fixture.internal_id,
                now,
                metadata,
            )
            if attempt_id:
                attempt_ids[job.job_key] = attempt_id
        return attempt_ids

    def _begin_attempt_record(
        self,
        job_key: str,
        source: str,
        job_type: str,
        fixture_id: str | None,
        now: datetime,
        metadata: dict,
    ) -> str | None:
        if not self.current_run_id:
            return None
        number = self.connection.execute(
            """
            SELECT coalesce(max(attempt_number), 0) + 1
            FROM collection_attempt WHERE job_key = ?
            """,
            [job_key],
        ).fetchone()[0]
        attempt_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO collection_attempt (
                collection_attempt_id, job_key, collection_run_id,
                attempt_number, source_code, job_type, fixture_id,
                started_at, status, quota_cost, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, ?)
            """,
            [
                attempt_id, job_key, self.current_run_id, number,
                source, job_type, fixture_id, now, json_text(metadata),
            ],
        )
        return attempt_id

    def _http_attempt_recorder(
        self,
        source: str,
        resource: str,
        request_keys: list[str],
    ):
        request_id = str(uuid.uuid4())
        job_key = f"http:{source}:{resource}:{request_id}"

        def record(attempt, hook_value) -> None:
            if not self.current_run_id:
                return
            now = datetime.now(timezone.utc)
            raw_artifact_id = (
                hook_value.get("_raw_artifact_id")
                if isinstance(hook_value, dict)
                else None
            )
            self.connection.execute(
                """
                INSERT INTO collection_attempt (
                    collection_attempt_id,job_key,collection_run_id,
                    attempt_number,source_code,job_type,started_at,finished_at,
                    status,http_status,retry_after_seconds,quota_cost,
                    raw_artifact_id,error_class,error_message,metadata
                ) VALUES (?, ?, ?, ?, ?, 'http_request', ?, ?, ?, ?, ?, 1,
                          ?, ?, ?, ?)
                """,
                [
                    str(uuid.uuid4()), job_key, self.current_run_id,
                    attempt.number, source, now, now, attempt.classification,
                    attempt.http_status, attempt.retry_after_seconds,
                    raw_artifact_id,
                    None if attempt.classification == "succeeded" else attempt.classification,
                    None if attempt.classification == "succeeded" else "provider request did not succeed",
                    json_text({"resource": resource, "request_keys": request_keys}),
                ],
            )

        return record

    def _finish_attempts(
        self,
        attempt_ids: dict[str, str],
        *,
        status: str,
        finished_at: datetime,
        http_status: int | None = None,
        raw_artifact_id: str | None = None,
        retry_after_seconds: int | None = None,
        error: Exception | None = None,
        metadata: dict | None = None,
    ) -> None:
        if not attempt_ids:
            return
        error_class = type(error).__name__ if error else None
        error_message = str(error) if error else None
        for attempt_id in attempt_ids.values():
            self.connection.execute(
                """
                UPDATE collection_attempt
                SET finished_at = ?, status = ?, http_status = ?,
                    retry_after_seconds = ?, raw_artifact_id = ?,
                    error_class = ?, error_message = ?,
                    metadata = coalesce(?, metadata)
                WHERE collection_attempt_id = ?
                """,
                [
                    finished_at, status, http_status, retry_after_seconds,
                    raw_artifact_id,
                    error_class, error_message,
                    json_text(metadata) if metadata is not None else None,
                    attempt_id,
                ],
            )

    @staticmethod
    def _request_failure_disposition(
        error: Exception, now: datetime
    ) -> tuple[str, str, datetime | None, int | None, str | None, str | None]:
        if isinstance(error, RequestExecutionError):
            retry_after = error.retry_after_seconds
            raw_artifact_id = (
                error.hook_value.get("_raw_artifact_id")
                if isinstance(error.hook_value, dict)
                else None
            )
            if error.classification == "rate_limited":
                delay = retry_after if retry_after is not None else 60
                return (
                    "rate_limited", "rate_limited",
                    now + timedelta(seconds=delay), retry_after,
                    raw_artifact_id, None,
                )
            if error.classification == "permanent_error":
                return (
                    "terminal", "permanent_error", None, retry_after,
                    raw_artifact_id, "permanent_provider_error",
                )
            return (
                "failed", "retryable_error", now + timedelta(minutes=1),
                retry_after, raw_artifact_id, None,
            )
        return "failed", "retryable_error", now + timedelta(minutes=1), None, None, None

    @staticmethod
    def _fatal_request_error(error: Exception) -> bool:
        return isinstance(error, RequestExecutionError) and error.http_status in {401, 403}

    def _discovery_window(
        self, local_date: date, catch_up_days: int | None
    ) -> tuple[date, date, list[date], int]:
        configured_recovery = int(self.discovery_config.get("recovery_days", 14))
        planning_days = int(self.discovery_config.get("planning_days", 7))
        completed_frontier_days = self._completed_frontier_days(local_date)
        start_date, end_date, target_dates = discovery_date_window(
            local_date,
            recovery_days=configured_recovery,
            planning_days=planning_days,
            catch_up_days=catch_up_days,
            completed_frontier_days=completed_frontier_days,
        )
        return start_date, end_date, target_dates, (local_date - start_date).days

    def _completed_frontier_days(self, local_date: date) -> int | None:
        """Expand recovery to the latest monitored completed-fixture frontier.

        This intentionally recovers every date since the last observed model-
        relevant match after downtime, even when that exceeds the normal
        safety window.
        """
        rows = self.connection.execute(
            """
            SELECT f.scheduled_kickoff, cm.source_entity_id, c.country_code, c.name
            FROM fixture f
            LEFT JOIN competition c ON c.competition_id = f.competition_id
            LEFT JOIN source_entity_map cm
              ON cm.internal_entity_id = c.competition_id
             AND cm.source_code = 'api_football'
             AND cm.entity_type = 'competition'
            WHERE f.scheduled_kickoff IS NOT NULL
              AND f.status IN ('completed', 'FT', 'AET', 'PEN')
            """
        ).fetchall()
        latest: date | None = None
        for kickoff, league_id, country, name in rows:
            if not self._is_monitored_competition(league_id, country, name):
                continue
            fixture_date = kickoff.astimezone(self.zone).date()
            if fixture_date <= local_date and (latest is None or fixture_date > latest):
                latest = fixture_date
        if latest is None:
            return None
        return (local_date - latest).days

    def _plan_discovery_jobs(
        self, target_dates: list[date], today: date, now: datetime
    ) -> list[DiscoveryJob]:
        jobs: list[DiscoveryJob] = []
        for target_date in target_dates:
            job = discovery_job_for_date(
                target_date,
                today=today,
                now=now,
                zone=self.zone,
                today_tomorrow_hours=int(
                    self.discovery_config.get("today_tomorrow_refresh_hours", 6)
                ),
            )
            if job.cadence == "recovery":
                required = not self._successful_discovery_for_date(target_date)
            else:
                required = not self._checkpoint_succeeded(job.job_key)
            if required:
                jobs.append(job)
        return sorted(jobs, key=lambda job: (job.priority, job.target_date, job.job_key))

    def _successful_discovery_for_date(self, target_date: date) -> bool:
        rows = self.connection.execute(
            """
            SELECT job_key, metadata
            FROM collection_checkpoint
            WHERE source_code = 'api_football'
              AND job_type = 'fixture_discovery'
              AND status = 'succeeded'
            """
        ).fetchall()
        for job_key, metadata in rows:
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = None
            if discovery_date_from_checkpoint(job_key, metadata) == target_date:
                return True
        return False

    def _checkpoint_succeeded(self, job_key: str) -> bool:
        row = self.connection.execute(
            "SELECT status FROM collection_checkpoint WHERE job_key = ?", [job_key]
        ).fetchone()
        return bool(row and row[0] == "succeeded")

    def _execute_discovery_jobs(
        self, jobs: list[DiscoveryJob], now: datetime, dry_run: bool
    ) -> None:
        for job in jobs:
            try:
                self._discover_fixtures(job, now, dry_run)
            except Exception as error:
                if self._fatal_request_error(error):
                    raise
                self.summary.setdefault("warnings", []).append(
                    f"fixture discovery deferred for {job.target_date.isoformat()}"
                )

    def _discover_fixtures(
        self, job: DiscoveryJob, now: datetime, dry_run: bool
    ) -> None:
        job_key = job.job_key
        self.summary["planned_jobs"].append(job_key)
        if dry_run:
            return
        request_metadata = {
            "date": job.target_date.isoformat(),
            "target_date": job.target_date.isoformat(),
            "timezone": self.config["timezone"],
            "cadence": job.cadence,
            "slot": job.slot,
            "reason": job.reason,
        }
        attempt_id = self._begin_attempt_record(
            job_key, "api_football", "fixture_discovery", None, now, request_metadata
        )
        try:
            payload, item, status = self._api_get(
                "fixtures_by_date",
                "/fixtures",
                {"date": job.target_date.isoformat(), "timezone": self.config["timezone"]},
                now,
            )
            matches, selected = self._filtered_discovery_matches(payload)
            self._validate_discovery_matches(selected)
            filtered_payload = dict(payload)
            filtered_payload["response"] = selected
            metadata = {
                **request_metadata,
                "returned": len(matches),
                "selected": len(selected),
            }
            with self.warehouse.transaction():
                self.loader.load_api_football_payload(
                    filtered_payload, item, "fixtures_by_date"
                )
                self._record_checkpoint(
                    job_key,
                    "api_football",
                    "fixture_discovery",
                    None,
                    job.scheduled_for,
                    "succeeded",
                    status,
                    metadata,
                    priority=job.priority,
                )
        except Exception as error:
            checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                self._request_failure_disposition(error, now)
            )
            self._finish_attempts(
                {job_key: attempt_id} if attempt_id else {},
                status=attempt_status,
                finished_at=now,
                http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                retry_after_seconds=retry_after,
                raw_artifact_id=raw_id,
                error=error,
                metadata=request_metadata,
            )
            self._record_checkpoint(
                job_key,
                "api_football",
                "fixture_discovery",
                None,
                job.scheduled_for,
                checkpoint_status,
                error.http_status if isinstance(error, RequestExecutionError) else None,
                request_metadata,
                error=str(error),
                priority=job.priority,
                next_attempt_at=next_attempt,
                terminal_reason=terminal_reason,
            )
            raise
        self._finish_attempts(
            {job_key: attempt_id} if attempt_id else {},
            status="succeeded",
            finished_at=now,
            http_status=status,
            raw_artifact_id=item.get("_raw_artifact_id"),
            metadata=metadata,
        )
        self.summary["executed_jobs"].append(job_key)

    def _refresh_closed_polymarket_events(
        self,
        fixtures: list[FixtureRecord],
        now: datetime,
        *,
        dry_run: bool = False,
    ) -> None:
        fixture_ids = [fixture.internal_id for fixture in fixtures]
        if not fixture_ids:
            return
        placeholders = ",".join("?" for _ in fixture_ids)
        rows = self.connection.execute(
            f"""
            SELECT prediction_market_event_id,source_event_id,
                   coalesce(end_time,start_time),closed
            FROM prediction_market_event
            WHERE fixture_id IN ({placeholders})
              AND coalesce(end_time,start_time) <= ?
            """,
            [*fixture_ids, now],
        ).fetchall()
        for event_id, source_event_id, closure_time, closed in rows:
            job_key = f"polymarket:event_closed_refresh:{source_event_id}"
            if self._checkpoint_done(job_key):
                continue
            self.summary["planned_jobs"].append(job_key)
            if dry_run:
                continue
            attempt_id = self._begin_attempt_record(
                job_key, "polymarket_gamma", "event_closed_refresh",
                None, now, {"source_event_id": source_event_id},
            )
            try:
                payload, item, status = self._polymarket_get(
                    "soccer_events", f"/events/{source_event_id}", {}
                )
                if isinstance(payload, dict) and "events" not in payload:
                    payload = {"events": [payload]}
                self.loader.load_polymarket_payload("soccer_events", payload, item)
            except Exception as error:
                checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                    self._request_failure_disposition(error, now)
                )
                self._finish_attempts(
                    {job_key: attempt_id} if attempt_id else {},
                    status=attempt_status,
                    finished_at=datetime.now(timezone.utc),
                    http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                    retry_after_seconds=retry_after,
                    raw_artifact_id=raw_id,
                    error=error,
                )
                self._record_checkpoint(
                    job_key, "polymarket_gamma", "event_closed_refresh", None,
                    closure_time or now, checkpoint_status,
                    error.http_status if isinstance(error, RequestExecutionError) else None,
                    {"source_event_id": source_event_id}, error=str(error),
                    next_attempt_at=next_attempt,
                    terminal_reason=terminal_reason,
                )
                self.summary.setdefault("warnings", []).append(
                    f"Polymarket closed refresh deferred for event {source_event_id}"
                )
                continue
            self._record_checkpoint(
                job_key, "polymarket_gamma", "event_closed_refresh", None,
                closure_time or now, "succeeded", status,
                {"source_event_id": source_event_id, "was_closed": bool(closed)},
            )
            self._finish_attempts(
                {job_key: attempt_id} if attempt_id else {},
                status="succeeded", finished_at=datetime.now(timezone.utc),
                http_status=status, raw_artifact_id=item.get("_raw_artifact_id"),
            )
            self.summary["executed_jobs"].append(job_key)

    def _filtered_discovery_matches(
        self, payload: object
    ) -> tuple[list[dict], list[dict]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), list):
            raise RuntimeError("API-Football discovery response has no fixture list")
        if any(not isinstance(match, dict) for match in payload["response"]):
            raise RuntimeError("API-Football discovery response contains a non-object fixture")
        matches = list(payload["response"])
        self._validate_discovery_matches(matches)
        selected = [match for match in matches if self._is_monitored_match(match)]
        return matches, selected

    @staticmethod
    def _validate_discovery_matches(matches: list[dict]) -> None:
        for match in matches:
            fixture = match.get("fixture") or {}
            if fixture.get("id") is None:
                raise RuntimeError("API-Football discovery returned a fixture without an ID")
            if not fixture.get("date"):
                raise RuntimeError(
                    f"API-Football fixture {fixture['id']} has no scheduled date"
                )

    def _plan_fixture_refresh_jobs(
        self, fixtures: list[FixtureRecord], now: datetime
    ) -> list[FixtureRefreshJob]:
        jobs: list[FixtureRefreshJob] = []
        near_minutes = int(
            self.discovery_config.get("near_kickoff_refresh_minutes", 120)
        )
        status_hours = int(
            self.discovery_config.get("postponed_cancelled_refresh_hours", 6)
        )
        if status_hours <= 0 or status_hours > 24:
            raise ValueError("postponed_cancelled_refresh_hours must be between 1 and 24")
        local_now = now.astimezone(self.zone)
        status_slot = local_now.replace(
            hour=local_now.hour - (local_now.hour % status_hours),
            minute=0,
            second=0,
            microsecond=0,
        )
        for fixture in fixtures:
            minutes_until = (fixture.kickoff - now).total_seconds() / 60
            if 0 <= minutes_until <= near_minutes:
                slot = f"near_kickoff:{int(fixture.kickoff.timestamp())}"
                job_key = fixture_refresh_job_key(
                    fixture.source_id, reason="near_kickoff", slot=slot
                )
                if not self._checkpoint_succeeded(job_key):
                    jobs.append(
                        FixtureRefreshJob(
                            job_key, fixture, "near_kickoff", slot, now
                        )
                    )
            status = latest_fixture_status(
                self.connection, fixture.internal_id, "api_football"
            )
            if status in {"postponed", "cancelled", "suspended"}:
                slot = f"status:{status_slot.strftime('%Y-%m-%dT%H:%M')}"
                job_key = fixture_refresh_job_key(
                    fixture.source_id, reason=f"{status}_signal", slot=slot
                )
                if not self._checkpoint_succeeded(job_key):
                    jobs.append(
                        FixtureRefreshJob(job_key, fixture, f"{status}_signal", slot, now)
                    )
        return sorted(jobs, key=lambda job: (job.scheduled_for, job.job_key))

    def _execute_fixture_refresh_jobs(
        self, jobs: list[FixtureRefreshJob], now: datetime, dry_run: bool
    ) -> None:
        for job in jobs:
            self.summary["planned_jobs"].append(job.job_key)
            if dry_run:
                continue
            request_metadata = {
                "id": job.fixture.source_id,
                "fixture_source_id": job.fixture.source_id,
                "reason": job.reason,
                "slot": job.slot,
            }
            attempt_id = self._begin_attempt_record(
                job.job_key,
                "api_football",
                "fixture_refresh",
                job.fixture.internal_id,
                now,
                request_metadata,
            )
            try:
                payload, item, status = self._api_get(
                    "fixture_by_id", "/fixtures", {"id": job.fixture.source_id}, now
                )
                matches, selected = self._filtered_discovery_matches(payload)
                self._validate_discovery_matches(selected)
                if job.fixture.source_id not in {
                    str((match.get("fixture") or {}).get("id")) for match in selected
                }:
                    raise RuntimeError(
                        f"API-Football fixture refresh omitted fixture {job.fixture.source_id}"
                    )
                filtered_payload = dict(payload)
                filtered_payload["response"] = selected
                metadata = {
                    **request_metadata,
                    "returned": len(matches),
                    "selected": len(selected),
                }
                with self.warehouse.transaction():
                    self.loader.load_api_football_payload(
                        filtered_payload, item, "fixture_by_id"
                    )
                    self._record_checkpoint(
                        job.job_key,
                        "api_football",
                        "fixture_refresh",
                        job.fixture.source_id,
                        job.scheduled_for,
                        "succeeded",
                        status,
                        metadata,
                        fixture_id=job.fixture.internal_id,
                        priority=1,
                    )
            except Exception as error:
                checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                    self._request_failure_disposition(error, now)
                )
                self._finish_attempts(
                    {job.job_key: attempt_id} if attempt_id else {},
                    status=attempt_status,
                    finished_at=now,
                    http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                    retry_after_seconds=retry_after,
                    raw_artifact_id=raw_id,
                    error=error,
                    metadata=request_metadata,
                )
                self._record_checkpoint(
                    job.job_key,
                    "api_football",
                    "fixture_refresh",
                    job.fixture.source_id,
                    job.scheduled_for,
                    checkpoint_status,
                    error.http_status if isinstance(error, RequestExecutionError) else None,
                    request_metadata,
                    error=str(error),
                    fixture_id=job.fixture.internal_id,
                    priority=1,
                    next_attempt_at=next_attempt,
                    terminal_reason=terminal_reason,
                )
                if self._fatal_request_error(error):
                    raise
                self.summary.setdefault("warnings", []).append(
                    f"fixture refresh deferred for {job.fixture.source_id}"
                )
                continue
            self._finish_attempts(
                {job.job_key: attempt_id} if attempt_id else {},
                status="succeeded",
                finished_at=now,
                http_status=status,
                raw_artifact_id=item.get("_raw_artifact_id"),
                metadata=metadata,
            )
            self.summary["executed_jobs"].append(job.job_key)

    def _discover_polymarket(
        self,
        local_date: date,
        fixtures: list[FixtureRecord],
        now: datetime,
        dry_run: bool,
    ) -> None:
        if not fixtures:
            return
        matchday = any(
            fixture.kickoff.astimezone(self.zone).date() == local_date
            for fixture in fixtures
        )
        interval = int(
            self.polymarket_config[
                "discovery_matchday_minutes" if matchday else "discovery_hourly_minutes"
            ]
        )
        local_now = now.astimezone(self.zone)
        slot_minute = local_now.minute - (local_now.minute % interval)
        slot = local_now.replace(minute=slot_minute, second=0, microsecond=0)
        job_key = (
            f"polymarket:event_discovery:{interval}m:"
            f"{slot.strftime('%Y-%m-%dT%H:%M')}"
        )
        if self._checkpoint_done(job_key):
            return
        self.summary["planned_jobs"].append(job_key)
        if dry_run:
            return
        start = datetime.combine(local_date, datetime_time.min, self.zone).astimezone(timezone.utc)
        end = datetime.combine(
            local_date + timedelta(days=7), datetime_time.max, self.zone
        ).astimezone(timezone.utc)
        cursor = None
        event_count = 0
        statuses: list[int] = []
        for page in range(self.polymarket_config["maximum_event_pages"]):
            params: dict[str, object] = {
                "tag_id": self.polymarket_config["soccer_tag_id"],
                "active": "true",
                "closed": "false",
                "limit": self.polymarket_config["event_page_size"],
                "start_time_min": start.isoformat(),
                "start_time_max": end.isoformat(),
            }
            if cursor:
                params["after_cursor"] = cursor
            attempt_id = self._begin_attempt_record(
                job_key,
                "polymarket_gamma",
                "event_discovery",
                None,
                now,
                {"page": page, "parameters": params},
            )
            try:
                payload, item, status = self._polymarket_get(
                    "soccer_events", "/events/keyset", params
                )
            except Exception as error:
                checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                    self._request_failure_disposition(error, now)
                )
                self._finish_attempts(
                    {job_key: attempt_id} if attempt_id else {},
                    status=attempt_status,
                    finished_at=datetime.now(timezone.utc),
                    http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                    retry_after_seconds=retry_after,
                    raw_artifact_id=raw_id,
                    error=error,
                    metadata={"page": page, "parameters": params},
                )
                self._record_checkpoint(
                    job_key, "polymarket_gamma", "event_discovery", None, now,
                    checkpoint_status,
                    error.http_status if isinstance(error, RequestExecutionError) else None,
                    {"page": page}, error=str(error),
                    next_attempt_at=next_attempt,
                    terminal_reason=terminal_reason,
                )
                self.summary.setdefault("warnings", []).append(
                    "Polymarket event discovery deferred"
                )
                return
            self._finish_attempts(
                {job_key: attempt_id} if attempt_id else {},
                status="succeeded",
                finished_at=datetime.now(timezone.utc),
                http_status=status,
                raw_artifact_id=item.get("_raw_artifact_id"),
                metadata={"page": page, "parameters": params},
            )
            statuses.append(status)
            self.loader.load_polymarket_payload("soccer_events", payload, item)
            events = payload.get("events", []) if isinstance(payload, dict) else []
            event_count += len(events) if isinstance(events, list) else 0
            next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
            if not next_cursor or next_cursor == cursor or not events:
                break
            cursor = next_cursor
        self._record_checkpoint(
            job_key, "polymarket_gamma", "event_discovery", None, now,
            "succeeded", statuses[-1] if statuses else None, {"events": event_count},
        )
        self.summary["executed_jobs"].append(job_key)

    def _plan_detail_jobs(
        self,
        fixtures: list[FixtureRecord],
        now: datetime,
        *,
        mutate_state: bool = True,
    ) -> list[DetailJob]:
        jobs: list[DetailJob] = []
        lineup_offsets = self.api_config.get("lineup_stage_offsets", [50, 35, 20, 5])
        for fixture in fixtures:
            schedule_version, schedule_observation_id, kickoff = (
                self._lineup_schedule_version(fixture)
            )
            if mutate_state:
                self._supersede_obsolete_lineup_jobs(
                    fixture, schedule_version, kickoff, now
                )
            stage_keys = self.connection.execute(
                """
                SELECT job_key
                FROM collection_checkpoint
                WHERE (fixture_id = ? OR fixture_source_id = ?)
                  AND job_type = 'lineup_stage'
                """,
                [fixture.internal_id, fixture.source_id],
            ).fetchall()
            attempted_stage_keys = {row[0] for row in stage_keys}
            schedule_fixture = replace(fixture, kickoff=kickoff)
            stage_plans = lineup_stage_plans(
                fixture_source_id=fixture.source_id,
                schedule_version=schedule_version,
                kickoff=kickoff,
                now=now,
                offsets=lineup_offsets,
                attempted_job_keys=attempted_stage_keys,
                lineup_complete=self._lineup_complete(
                    fixture.internal_id,
                    now,
                    schedule_kickoff=kickoff,
                ),
            )
            jobs.extend(
                DetailJob(
                    plan.job_key,
                    "lineup_stage",
                    schedule_fixture,
                    plan.stage_time,
                    plan.schedule_version,
                    schedule_observation_id,
                )
                for plan in stage_plans
            )

            status = latest_fixture_status(
                self.connection, fixture.internal_id, "api_football"
            )
            if mutate_state:
                self._terminalize_obsolete_postmatch_jobs(
                    fixture, schedule_version, status, now
                )
            post_rows = self.connection.execute(
                """
                SELECT job_key,status FROM collection_checkpoint
                WHERE (fixture_id = ? OR fixture_source_id = ?)
                  AND (
                    job_type IN ('postmatch_status','postmatch_final_retry',
                                 'correction_refresh_24h','correction_refresh_72h')
                    OR job_type IN ('postmatch_primary','postmatch_retry')
                  )
                """,
                [fixture.internal_id, fixture.source_id],
            ).fetchall()
            attempted_post_keys = {
                key for key, checkpoint_status in post_rows
                if checkpoint_status not in {"pending", "failed", "rate_limited"}
            }
            plans = postmatch_stage_plans(
                fixture_source_id=fixture.source_id,
                schedule_version=schedule_version,
                kickoff=kickoff,
                now=now,
                canonical_status=status,
                components_complete=self._postmatch_complete(
                    fixture.internal_id, now
                ),
                attempted_job_keys=attempted_post_keys,
                first_check_minutes=int(
                    self.api_config.get("post_match_first_check_minutes", 150)
                ),
                live_poll_minutes=int(
                    self.api_config.get("post_match_live_poll_minutes", 30)
                ),
                live_poll_until_minutes=int(
                    self.api_config.get("post_match_live_poll_until_minutes", 360)
                ),
                final_retry_minutes=int(
                    self.api_config.get("post_match_final_retry_minutes", 480)
                ),
                correction_offsets_minutes=self.api_config.get(
                    "correction_refresh_offsets_minutes", [1440, 4320]
                ),
            )
            jobs.extend(
                DetailJob(plan.job_key, plan.stage, schedule_fixture, plan.stage_time)
                for plan in plans
            )
        return jobs

    def _terminalize_obsolete_postmatch_jobs(
        self,
        fixture: FixtureRecord,
        schedule_version: str,
        canonical_status: str,
        now: datetime,
    ) -> None:
        terminal_statuses = {
            "postponed", "cancelled", "abandoned", "administrative_result"
        }
        rows = self.connection.execute(
            """
            SELECT job_key,status FROM collection_checkpoint
            WHERE (fixture_id = ? OR fixture_source_id = ?)
              AND (job_type LIKE 'postmatch%' OR job_type LIKE 'correction_refresh_%')
            """,
            [fixture.internal_id, fixture.source_id],
        ).fetchall()
        for job_key, checkpoint_status in rows:
            current_version = f":{schedule_version}:" in job_key
            if current_version and canonical_status not in terminal_statuses:
                continue
            if checkpoint_status in {"terminal", "skipped_with_reason"}:
                continue
            reason = (
                f"fixture_{canonical_status}"
                if current_version and canonical_status in terminal_statuses
                else "schedule_superseded"
            )
            self.connection.execute(
                """
                UPDATE collection_checkpoint
                SET status='terminal', completed_at=coalesce(completed_at, ?),
                    next_attempt_at=NULL, terminal_reason=?, updated_at=?,
                    last_run_id=?
                WHERE job_key=?
                """,
                [now, reason, now, self.current_run_id, job_key],
            )

    def _lineup_schedule_version(
        self, fixture: FixtureRecord
    ) -> tuple[str, str | None, datetime]:
        row = self.connection.execute(
            """
            SELECT schedule_observation_id, scheduled_kickoff
            FROM fixture_schedule_observation
            WHERE fixture_id = ? AND source_code = 'api_football'
            ORDER BY retrieved_at DESC, schedule_observation_id DESC
            LIMIT 1
            """,
            [fixture.internal_id],
        ).fetchone()
        if row and row[0]:
            kickoff = (row[1] or fixture.kickoff).astimezone(timezone.utc)
            return f"kickoff-{int(kickoff.timestamp())}", str(row[0]), kickoff
        kickoff = fixture.kickoff.astimezone(timezone.utc)
        return f"kickoff-{int(kickoff.timestamp())}", None, kickoff

    def _supersede_obsolete_lineup_jobs(
        self,
        fixture: FixtureRecord,
        schedule_version: str,
        kickoff: datetime,
        now: datetime,
    ) -> None:
        rows = self.connection.execute(
            """
            SELECT job_key, job_type, status, terminal_reason, metadata
            FROM collection_checkpoint
            WHERE (fixture_id = ? OR fixture_source_id = ?)
              AND job_type IN ('lineup_primary', 'lineup_retry', 'lineup_stage')
            """,
            [fixture.internal_id, fixture.source_id],
        ).fetchall()
        current_kickoff_key = str(int(kickoff.timestamp()))
        schedule_superseded = False
        for job_key, job_type, status, terminal_reason, metadata in rows:
            if job_type == "lineup_stage" and f":{schedule_version}:" in job_key:
                continue
            if job_type in {"lineup_primary", "lineup_retry"}:
                reason = (
                    "legacy_stage_replaced"
                    if job_key.endswith(f":{current_kickoff_key}")
                    else "schedule_superseded"
                )
            else:
                reason = "schedule_superseded"
            if status == "terminal" and terminal_reason == reason:
                continue
            schedule_superseded = schedule_superseded or reason == "schedule_superseded"
            details = metadata
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except json.JSONDecodeError:
                    details = {}
            if not isinstance(details, dict):
                details = {}
            details.update({
                "schedule_version": schedule_version,
                "superseded_at": now.isoformat(),
                "superseded_reason": reason,
            })
            self.connection.execute(
                """
                UPDATE collection_checkpoint
                SET status = 'terminal', completed_at = coalesce(completed_at, ?),
                    next_attempt_at = NULL, terminal_reason = ?, metadata = ?,
                    updated_at = ?, last_run_id = ?
                WHERE job_key = ?
                """,
                [now, reason, json_text(details), now, self.current_run_id, job_key],
            )
        if schedule_superseded:
            self.connection.execute(
                """
                UPDATE fixture_collection_component
                SET state = 'pending', reason_code = 'schedule_superseded',
                    details = ?, validated_at = NULL, last_raw_artifact_id = NULL,
                    updated_at = ?
                WHERE fixture_id = ? AND source_code = 'api_football'
                  AND component_code = 'pregame_lineup_capture'
                """,
                [
                    json_text({"schedule_version": schedule_version}),
                    now,
                    fixture.internal_id,
                ],
            )

    def _execute_detail_jobs(self, jobs: list[DetailJob], now: datetime) -> None:
        if not jobs:
            return
        by_source_id: dict[str, list[DetailJob]] = {}
        for job in jobs:
            by_source_id.setdefault(job.fixture.source_id, []).append(job)
        fixture_ids = sorted(by_source_id)
        supports_batch = bool(self.api_config.get("supports_fixture_ids_batch", False))
        batch_size = int(self.api_config["fixture_batch_size"]) if supports_batch else 1
        for batch in chunks(fixture_ids, batch_size):
            if not self._api_budget_available(now):
                self.summary.setdefault("warnings", []).append("API-Football daily reserve reached")
                return
            batch_jobs = [
                job for source_id in batch for job in by_source_id[source_id]
            ]
            attempt_ids = self._begin_attempts(
                batch_jobs,
                "api_football",
                now,
                {"fixture_source_ids": batch},
            )
            params = {"ids": "-".join(batch)} if supports_batch else {"id": batch[0]}
            try:
                payload, item, status = self._api_get(
                    "fixture_details_batch", "/fixtures", params, now
                )
            except Exception as error:
                checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                    self._request_failure_disposition(error, now)
                )
                self._finish_attempts(
                    attempt_ids,
                    status=attempt_status,
                    finished_at=datetime.now(timezone.utc),
                    http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                    retry_after_seconds=retry_after,
                    raw_artifact_id=raw_id,
                    error=error,
                    metadata={"fixture_source_ids": batch},
                )
                for job in batch_jobs:
                    self._record_checkpoint(
                        job.job_key,
                        "api_football",
                        job.job_type,
                        job.fixture.source_id,
                        job.scheduled_for,
                        checkpoint_status,
                        error.http_status if isinstance(error, RequestExecutionError) else None,
                        {"fixture_source_ids": batch},
                        error=str(error),
                        fixture_id=job.fixture.internal_id,
                        next_attempt_at=next_attempt,
                        terminal_reason=terminal_reason,
                    )
                if self._fatal_request_error(error):
                    raise
                self.summary.setdefault("warnings", []).append(
                    f"fixture details batch deferred ({len(batch)} fixtures)"
                )
                continue
            lineup_schedule_ids = {
                job.fixture.source_id: job.schedule_observation_id
                for job in batch_jobs
                if job.job_type == "lineup_stage"
                and job.schedule_observation_id
            }
            if lineup_schedule_ids:
                item["_lineup_schedule_observation_ids"] = lineup_schedule_ids
            self.loader.load_api_football_payload(payload, item, "fixture_details_batch")
            returned_ids = {
                str(match.get("fixture", {}).get("id"))
                for match in payload.get("response", [])
                if isinstance(match, dict)
            }
            matches_by_id = {
                str(match.get("fixture", {}).get("id")): match
                for match in payload.get("response", [])
                if isinstance(match, dict)
            }
            for source_id in batch:
                match = matches_by_id.get(source_id)
                if match is not None:
                    self._record_event_processing(
                        by_source_id[source_id][0].fixture,
                        match,
                        item,
                        now,
                    )
            fixture_results: dict[str, dict[str, object]] = {}
            for source_id in batch:
                fixture = by_source_id[source_id][0].fixture
                if source_id in returned_ids:
                    fixture_results[fixture.internal_id] = reconcile_fixture_components(
                        self.connection, fixture.internal_id, "api_football", now
                    )
            for source_id in batch:
                for job in by_source_id[source_id]:
                    if source_id not in returned_ids:
                        state = "incomplete"
                        metadata = {"reason": "fixture_missing_from_batch_response"}
                        terminal_reason = None
                    elif job.job_type.startswith("lineup"):
                        (
                            current_schedule_version,
                            current_schedule_observation_id,
                            current_kickoff,
                        ) = self._lineup_schedule_version(job.fixture)
                        result = validate_lineups(
                            self.connection,
                            job.fixture.internal_id,
                            "api_football",
                            now,
                            schedule_observation_id=current_schedule_observation_id,
                            schedule_kickoff=current_kickoff,
                        )
                        schedule_changed = (
                            job.schedule_version is not None
                            and current_schedule_version != job.schedule_version
                        )
                        state = (
                            "terminal"
                            if schedule_changed
                            else self._checkpoint_state_for_component(result)
                        )
                        terminal_reason = (
                            "schedule_superseded" if schedule_changed else None
                        )
                        if result.state == "complete":
                            self._record_pregame_lineup_capture(
                                replace(job.fixture, kickoff=current_kickoff),
                                result,
                                now,
                                current_schedule_observation_id,
                            )
                        metadata = {
                            "lineup_state": result.state,
                            "lineup_job_key": job.job_key,
                            "schedule_version": current_schedule_version,
                            "schedule_changed_in_response": schedule_changed,
                            **result.details,
                        }
                    else:
                        results = fixture_results[job.fixture.internal_id]
                        complete = all(
                            component_is_terminally_done(results[name].state)
                            for name in (
                                "result", "lineups", "team_statistics",
                                "player_statistics", "events",
                            )
                        )
                        state = "succeeded" if complete else "incomplete"
                        terminal_reason = None
                        metadata = {
                            "postmatch_complete": complete,
                            "component_states": {
                                name: results[name].state
                                for name in (
                                    "result", "lineups", "team_statistics",
                                    "player_statistics", "events",
                                )
                            },
                        }
                        component = component_for_job_type(job.job_type)
                        if component and component.startswith("correction_refresh_"):
                            correction_result = ValidationResult(
                                "complete",
                                None,
                                {
                                    "facts_revalidated": True,
                                    "component_states": metadata["component_states"],
                                },
                                item.get("_raw_artifact_id"),
                            )
                            record_component_result(
                                self.connection,
                                fixture_id=job.fixture.internal_id,
                                source_code="api_football",
                                component_code=component,
                                result=correction_result,
                                now=now,
                            )
                            if component == "correction_refresh_72h":
                                earlier = self.connection.execute(
                                    """
                                    SELECT 1 FROM fixture_collection_component
                                    WHERE fixture_id=? AND source_code='api_football'
                                      AND component_code='correction_refresh_24h'
                                    """,
                                    [job.fixture.internal_id],
                                ).fetchone()
                                if not earlier:
                                    record_component_result(
                                        self.connection,
                                        fixture_id=job.fixture.internal_id,
                                        source_code="api_football",
                                        component_code="correction_refresh_24h",
                                        result=ValidationResult(
                                            "missed",
                                            "correction_window_missed_during_downtime",
                                            {
                                                "recovered_by": "correction_refresh_72h",
                                                "retrieved_at": now.isoformat(),
                                            },
                                            item.get("_raw_artifact_id"),
                                        ),
                                        now=now,
                                    )
                            state = "succeeded"
                    self._record_checkpoint(
                        job.job_key, "api_football", job.job_type, source_id,
                        job.scheduled_for, state, status, metadata,
                        fixture_id=job.fixture.internal_id,
                        terminal_reason=terminal_reason,
                    )
                    attempt_id = attempt_ids.get(job.job_key)
                    if attempt_id:
                        self._finish_attempts(
                            {job.job_key: attempt_id},
                            status=(
                                "succeeded"
                                if state in {"succeeded", "terminal"}
                                else "incomplete"
                            ),
                            finished_at=datetime.now(timezone.utc),
                            http_status=status,
                            raw_artifact_id=item.get("_raw_artifact_id"),
                            metadata=metadata,
                        )
                    self.summary["executed_jobs"].append(job.job_key)

    @staticmethod
    def _checkpoint_state_for_component(result) -> str:
        if result.state == "complete":
            return "succeeded"
        if result.state in {"terminal", "unavailable", "missed"}:
            return "terminal"
        return "incomplete"

    def _record_event_processing(
        self,
        fixture: FixtureRecord,
        match: dict,
        item: dict,
        now: datetime,
    ) -> None:
        events = match.get("events")
        raw_artifact_id = item.get("_raw_artifact_id")
        if not isinstance(events, list):
            result = events_processing_result(
                processed=False,
                event_count=None,
                raw_artifact_id=raw_artifact_id,
            )
        else:
            invalid_event_count = self.connection.execute(
                """
                SELECT count(*)
                FROM match_event e
                JOIN fixture f ON f.fixture_id = e.fixture_id
                WHERE e.fixture_id = ? AND e.source_code = ?
                  AND e.raw_artifact_id = ?
                  AND e.team_id IS NOT NULL
                  AND e.team_id NOT IN (f.home_team_id, f.away_team_id)
                """,
                [fixture.internal_id, "api_football", raw_artifact_id],
            ).fetchone()[0]
            result = events_processing_result(
                processed=True,
                event_count=len(events),
                invalid_event_count=invalid_event_count,
                raw_artifact_id=raw_artifact_id,
            )
        record_component_result(
            self.connection,
            fixture_id=fixture.internal_id,
            source_code="api_football",
            component_code="events",
            result=result,
            now=now,
        )

    def _record_pregame_lineup_capture(
        self,
        fixture: FixtureRecord,
        result: ValidationResult,
        now: datetime,
        schedule_observation_id: str | None = None,
    ) -> None:
        if result.state != "complete":
            return
        row = self.connection.execute(
            """
            SELECT 1
            FROM lineup_snapshot
            WHERE fixture_id = ? AND source_code = ?
              AND raw_artifact_id = ? AND captured_before_kickoff = true
            LIMIT 1
            """,
            [fixture.internal_id, "api_football", result.last_raw_artifact_id],
        ).fetchone()
        if not row:
            return
        record_component_result(
            self.connection,
            fixture_id=fixture.internal_id,
            source_code="api_football",
            component_code="pregame_lineup_capture",
            result=ValidationResult(
                "complete",
                None,
                {
                    "captured_before_kickoff": True,
                    "schedule_observation_id": schedule_observation_id,
                },
                result.last_raw_artifact_id,
            ),
            now=now,
        )

    def _plan_market_jobs(self, fixtures: list[FixtureRecord], now: datetime) -> list[DetailJob]:
        jobs: list[DetailJob] = []
        live_interval = int(self.polymarket_config.get("live_refresh_minutes", 10))
        live_lookahead = timedelta(
            hours=int(self.polymarket_config.get("live_lookahead_hours", 72))
        )
        live_slot = now.replace(
            minute=now.minute - (now.minute % live_interval),
            second=0,
            microsecond=0,
        )
        for fixture in fixtures:
            if not self._fixture_has_any_market_tokens(fixture.internal_id):
                continue
            schedule_version, _, kickoff = self._lineup_schedule_version(fixture)
            rows = self.connection.execute(
                """
                SELECT job_key,status,attempts,next_attempt_at
                FROM collection_checkpoint
                WHERE fixture_id=? AND source_code='polymarket_clob'
                """,
                [fixture.internal_id],
            ).fetchall()
            maximum_attempts = int(
                self.polymarket_config.get("snapshot_maximum_attempts", 3)
            )
            attempted = {
                key for key, status, attempts, next_attempt_at in rows
                if checkpoint_is_stopping(status)
                or attempts >= maximum_attempts
                or (next_attempt_at is not None and next_attempt_at > now)
            }
            plans = market_stage_plans(
                fixture_source_id=fixture.source_id,
                schedule_version=schedule_version,
                kickoff=kickoff,
                now=now,
                offsets_minutes=self.polymarket_config[
                    "snapshot_offsets_minutes"
                ],
                stage_window_minutes=int(
                    self.polymarket_config["snapshot_stage_window_minutes"]
                ),
                lineup_complete=self._lineup_complete(
                    fixture.internal_id, now, schedule_kickoff=kickoff
                ),
                attempted_job_keys=attempted,
                closure_delay_minutes=int(
                    self.polymarket_config["closure_snapshot_delay_minutes"]
                ),
            )
            schedule_fixture = replace(fixture, kickoff=kickoff)
            jobs.extend(
                DetailJob(
                    plan.job_key,
                    plan.stage,
                    schedule_fixture,
                    plan.stage_time,
                    capture_target_at=plan.capture_target_at,
                    capture_deadline_at=plan.capture_deadline_at,
                )
                for plan in plans
            )
            if now < kickoff <= now + live_lookahead:
                live_key = (
                    f"polymarket:market_live:{fixture.source_id}:"
                    f"{schedule_version}:{live_slot.isoformat()}"
                )
                if not self._checkpoint_done(live_key):
                    jobs.append(
                        DetailJob(
                            live_key,
                            "market_live",
                            schedule_fixture,
                            live_slot,
                        )
                    )
        return jobs

    def _execute_market_jobs(self, jobs: list[DetailJob], now: datetime) -> None:
        if not jobs:
            return
        tokens_by_fixture = {
            job.fixture.internal_id: self._market_tokens(
                job.fixture.internal_id,
                include_closed=job.job_type == "market_after_closure",
            )
            for job in jobs
        }
        all_tokens = sorted({token for tokens in tokens_by_fixture.values() for token in tokens})
        received: set[str] = set()
        last_status = None
        attempt_ids = self._begin_attempts(
            jobs,
            "polymarket_clob",
            now,
            {"token_count": len(all_tokens)},
        )
        for batch in chunks(all_tokens, int(self.polymarket_config["orderbook_batch_size"])):
            try:
                payload, item, status = self._polymarket_post(
                    "order_books_batch", "/books", [{"token_id": token} for token in batch]
                )
            except Exception as error:
                checkpoint_status, attempt_status, next_attempt, retry_after, raw_id, terminal_reason = (
                    self._request_failure_disposition(error, now)
                )
                self._finish_attempts(
                    attempt_ids,
                    status=attempt_status,
                    finished_at=datetime.now(timezone.utc),
                    http_status=(error.http_status if isinstance(error, RequestExecutionError) else None),
                    retry_after_seconds=retry_after,
                    raw_artifact_id=raw_id,
                    error=error,
                    metadata={"token_count": len(batch)},
                )
                for job in jobs:
                    self._record_checkpoint(
                        job.job_key,
                        "polymarket_clob",
                        job.job_type,
                        job.fixture.source_id,
                        job.scheduled_for,
                        checkpoint_status,
                        error.http_status if isinstance(error, RequestExecutionError) else None,
                        {"token_count": len(batch)},
                        error=str(error),
                        fixture_id=job.fixture.internal_id,
                        next_attempt_at=next_attempt,
                        terminal_reason=terminal_reason,
                    )
                self.summary.setdefault("warnings", []).append(
                    "Polymarket order-book batch deferred"
                )
                return
            last_status = status
            retrieved_at = parse_datetime(item.get("retrieved_at")) or now
            valid_jobs = {
                job.job_key: self._market_retrieval_valid(job, retrieved_at)
                for job in jobs
            }
            stage_by_token: dict[str, str] = {}
            kickoff_by_token: dict[str, str] = {}
            capture_by_token: dict[str, dict[str, str | bool | None]] = {}
            for job in jobs:
                for token in tokens_by_fixture[job.fixture.internal_id]:
                    stage_by_token[token] = job.job_type
                    kickoff_by_token[token] = job.fixture.kickoff.isoformat()
                    capture_by_token[token] = {
                        "target_at": (
                            job.capture_target_at.isoformat()
                            if job.capture_target_at
                            else None
                        ),
                        "window_start_at": job.scheduled_for.isoformat(),
                        "deadline_at": (
                            job.capture_deadline_at.isoformat()
                            if job.capture_deadline_at
                            else None
                        ),
                        "timing_valid": valid_jobs[job.job_key],
                        "timing_failure_reason": (
                            None
                            if valid_jobs[job.job_key]
                            else "retrieval_outside_frozen_capture_window"
                        ),
                    }
            item["_cadence_stage_by_token"] = stage_by_token
            item["_kickoff_by_token"] = kickoff_by_token
            item["_capture_by_token"] = capture_by_token
            self.loader.load_polymarket_payload("order_books_batch", payload, item)
            if isinstance(payload, list):
                received.update(
                    str(book.get("asset_id")) for book in payload if isinstance(book, dict)
                )
        for job in jobs:
            tokens = tokens_by_fixture[job.fixture.internal_id]
            if not tokens:
                continue
            retrieval_valid = valid_jobs.get(job.job_key, False)
            complete = set(tokens).issubset(received) and retrieval_valid
            received_for_fixture = set(tokens).intersection(received)
            previous_attempts_row = self.connection.execute(
                "SELECT attempts FROM collection_checkpoint WHERE job_key=?",
                [job.job_key],
            ).fetchone()
            previous_attempts = previous_attempts_row[0] if previous_attempts_row else 0
            maximum_attempts = (
                1
                if job.job_type == "market_live"
                else int(self.polymarket_config.get("snapshot_maximum_attempts", 3))
            )
            exhausted = not complete and previous_attempts + 1 >= maximum_attempts
            checkpoint_status = (
                "succeeded" if complete else "terminal" if exhausted else "incomplete"
            )
            terminal_reason = (
                "orderbook_tokens_unavailable_after_retries" if exhausted else None
            )
            retry_at = (
                None
                if complete or exhausted
                else now + timedelta(
                    minutes=int(
                        self.polymarket_config.get("snapshot_retry_minutes", 15)
                    )
                )
            )
            self._record_checkpoint(
                job.job_key, "polymarket_clob", job.job_type, job.fixture.source_id,
                job.scheduled_for, checkpoint_status, last_status,
                {
                    "requested_tokens": len(tokens),
                    "received_tokens": len(received_for_fixture),
                    "cadence_stage": job.job_type,
                    "retrieval_within_stage_window": retrieval_valid,
                },
                fixture_id=job.fixture.internal_id,
                maximum_attempts=maximum_attempts,
                terminal_reason=terminal_reason,
                next_attempt_at=retry_at,
            )
            attempt_id = attempt_ids.get(job.job_key)
            if attempt_id:
                self._finish_attempts(
                    {job.job_key: attempt_id},
                    status="succeeded" if complete else "incomplete",
                    finished_at=datetime.now(timezone.utc),
                    http_status=last_status,
                    raw_artifact_id=item.get("_raw_artifact_id"),
                    metadata={
                        "requested_tokens": len(tokens),
                        "received_tokens": len(received_for_fixture),
                        "cadence_stage": job.job_type,
                        "retrieval_within_stage_window": retrieval_valid,
                    },
                )
            self.summary["executed_jobs"].append(job.job_key)

    def _market_retrieval_valid(
        self, job: DetailJob, retrieved_at: datetime
    ) -> bool:
        retrieved_at = retrieved_at.astimezone(timezone.utc)
        if job.job_type.startswith("market_t_minus_"):
            deadline = job.capture_deadline_at or (
                job.scheduled_for
                + timedelta(
                    minutes=int(
                        self.polymarket_config["snapshot_stage_window_minutes"]
                    )
                )
            )
            return (
                job.scheduled_for <= retrieved_at < deadline
                and retrieved_at < job.fixture.kickoff
            )
        if job.job_type == "market_after_lineup":
            return job.scheduled_for <= retrieved_at < job.fixture.kickoff
        if job.job_type == "market_after_closure":
            return retrieved_at >= job.scheduled_for
        if job.job_type == "market_live":
            return job.scheduled_for <= retrieved_at < job.fixture.kickoff
        # Compatibility for pre-rework checkpoints/tests. New planning never
        # emits these legacy names.
        return True

    def _mark_expired_pregame_components(
        self, fixtures: list[FixtureRecord], now: datetime
    ) -> None:
        for fixture in fixtures:
            _, _, kickoff = self._lineup_schedule_version(fixture)
            if now < kickoff:
                continue
            schedule_fixture = replace(fixture, kickoff=kickoff)
            status = latest_fixture_status(
                self.connection, fixture.internal_id, "api_football"
            )
            if status in {
                "postponed", "cancelled", "abandoned", "administrative_result"
            }:
                continue
            lineup = validate_lineups(
                self.connection,
                fixture.internal_id,
                "api_football",
                now,
                schedule_kickoff=kickoff,
            )
            lineup_captured_before_kickoff = False
            if lineup.state == "complete" and lineup.last_raw_artifact_id:
                row = self.connection.execute(
                    "SELECT retrieved_at FROM raw_artifact WHERE raw_artifact_id = ?",
                    [lineup.last_raw_artifact_id],
                ).fetchone()
                lineup_captured_before_kickoff = bool(
                    row and row[0].astimezone(timezone.utc) < kickoff
                )
            self._mark_pregame_component(
                schedule_fixture,
                "pregame_lineup_capture",
                complete=lineup_captured_before_kickoff,
                now=now,
                complete_reason="valid_lineup_retrieved_before_kickoff",
                missed_reason="kickoff_passed_without_pregame_lineup",
            )
            if self._fixture_has_any_market_tokens(schedule_fixture.internal_id):
                required_market_stages = {
                    f"market_t_minus_{int(offset)}"
                    for offset in self.polymarket_config["snapshot_offsets_minutes"]
                }
                pregame_lineup = self.connection.execute(
                    """
                    SELECT state FROM fixture_collection_component
                    WHERE fixture_id=? AND source_code='api_football'
                      AND component_code='pregame_lineup_capture'
                    """,
                    [schedule_fixture.internal_id],
                ).fetchone()
                if pregame_lineup and pregame_lineup[0] == "complete":
                    required_market_stages.add("market_after_lineup")
                captured_market_stages = {
                    row[0] for row in self.connection.execute(
                        """
                        SELECT DISTINCT job_type FROM collection_checkpoint
                        WHERE fixture_id=? AND source_code='polymarket_clob'
                          AND status='succeeded'
                          AND (job_type LIKE 'market_t_minus_%'
                               OR job_type='market_after_lineup')
                        """,
                        [schedule_fixture.internal_id],
                    ).fetchall()
                }
                market_captured_before_kickoff = required_market_stages.issubset(
                    captured_market_stages
                )
                self._mark_pregame_component(
                    schedule_fixture,
                    "pregame_market_capture",
                    complete=market_captured_before_kickoff,
                    now=now,
                    complete_reason="market_snapshot_retrieved_before_kickoff",
                    missed_reason="kickoff_passed_without_pregame_market_snapshot",
                )

    def _mark_pregame_component(
        self,
        fixture: FixtureRecord,
        component_code: str,
        *,
        complete: bool,
        now: datetime,
        complete_reason: str,
        missed_reason: str,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT state
            FROM fixture_collection_component
            WHERE fixture_id = ? AND source_code = ? AND component_code = ?
            """,
            [fixture.internal_id, "api_football", component_code],
        ).fetchone()
        if row and component_is_terminally_done(row[0]):
            return
        if complete:
            result = ValidationResult(
                "complete", None, {"reason": complete_reason}
            )
        else:
            result = ValidationResult(
                "missed", missed_reason, {"kickoff": fixture.kickoff.isoformat()}
            )
        record_component_result(
            self.connection,
            fixture_id=fixture.internal_id,
            source_code="api_football",
            component_code=component_code,
            result=result,
            now=now,
        )

    def _monitored_fixtures(
        self,
        local_date: date,
        *,
        lookback_days: int = 0,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[FixtureRecord]:
        rows = self.connection.execute(
            """
            SELECT f.fixture_id, fm.source_entity_id, f.scheduled_kickoff, f.status,
                   ht.name, aw.name, cm.source_entity_id, c.country_code, c.name
            FROM fixture f
            JOIN source_entity_map fm
              ON fm.internal_entity_id = f.fixture_id
             AND fm.source_code = 'api_football' AND fm.entity_type = 'fixture'
            JOIN team ht ON ht.team_id = f.home_team_id
            JOIN team aw ON aw.team_id = f.away_team_id
            LEFT JOIN competition c ON c.competition_id = f.competition_id
            LEFT JOIN source_entity_map cm
              ON cm.internal_entity_id = c.competition_id
             AND cm.source_code = 'api_football' AND cm.entity_type = 'competition'
            WHERE f.scheduled_kickoff IS NOT NULL
            """
        ).fetchall()
        selected: list[FixtureRecord] = []
        earliest_date = (
            start_date if start_date is not None
            else local_date - timedelta(days=lookback_days)
        )
        latest_date = end_date if end_date is not None else local_date
        if earliest_date > latest_date:
            raise ValueError("Fixture selection start_date must not be after end_date")
        for internal_id, source_id, kickoff, status, home, away, league_id, country, name in rows:
            kickoff = kickoff.astimezone(timezone.utc)
            fixture_date = kickoff.astimezone(self.zone).date()
            if not earliest_date <= fixture_date <= latest_date:
                continue
            if not self._is_monitored_competition(league_id, country, name):
                continue
            selected.append(FixtureRecord(internal_id, str(source_id), kickoff, status, home, away))
        selected.sort(key=lambda fixture: fixture.kickoff)
        return selected

    def _is_monitored_match(self, match: dict) -> bool:
        league = match.get("league") or {}
        return self._is_monitored_competition(
            league.get("id"), league.get("country"), league.get("name")
        )

    def _is_monitored_competition(self, league_id: object, country: object, name: object) -> bool:
        if league_id is not None and str(league_id) in self.monitored_league_ids:
            return True
        return self._competition_key(str(country or ""), str(name or "")) in self.monitored_competition_keys

    @staticmethod
    def _competition_key(country: str, name: str) -> str:
        return f"{normalized_name(country)}|{normalized_name(name)}"

    def _lineup_complete(
        self,
        fixture_id: str,
        now: datetime | None = None,
        schedule_observation_id: str | None = None,
        schedule_kickoff: datetime | None = None,
    ) -> bool:
        result = validate_lineups(
            self.connection,
            fixture_id,
            "api_football",
            now or datetime.now(timezone.utc),
            schedule_observation_id=schedule_observation_id,
            schedule_kickoff=schedule_kickoff,
        )
        return result.state == "complete"

    def _postmatch_complete(
        self, fixture_id: str, now: datetime | None = None
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        results = {
            "result": validate_result(self.connection, fixture_id, "api_football"),
            "lineups": validate_lineups(
                self.connection, fixture_id, "api_football", now
            ),
            "team_statistics": validate_team_statistics(
                self.connection, fixture_id, "api_football", now
            ),
            "player_statistics": validate_player_statistics(
                self.connection, fixture_id, "api_football", now
            ),
        }
        results["events"] = validate_events(
            self.connection, fixture_id, "api_football", now
        )
        return all(
            component_is_terminally_done(results[name].state)
            for name in (
                "result", "lineups", "team_statistics",
                "player_statistics", "events",
            )
        )

    def _link_polymarket_events(self, fixtures: list[FixtureRecord]) -> int:
        events = self.connection.execute(
            """
            SELECT prediction_market_event_id, title, end_time
            FROM prediction_market_event
            WHERE fixture_id IS NULL AND coalesce(active, true)
            """
        ).fetchall()
        linked = 0
        for event_id, title, event_time in events:
            title_norm = normalized_name(title or "")
            candidates: list[tuple[float, FixtureRecord]] = []
            for fixture in fixtures:
                if not self._team_name_in_title(fixture.home_name, title_norm):
                    continue
                if not self._team_name_in_title(fixture.away_name, title_norm):
                    continue
                difference = abs((event_time.astimezone(timezone.utc) - fixture.kickoff).total_seconds()) if event_time else 0
                if not event_time or difference <= 6 * 3600:
                    candidates.append((difference, fixture))
            if not candidates:
                continue
            candidates.sort(key=lambda item: item[0])
            if (
                len(candidates) > 1
                and abs(candidates[1][0] - candidates[0][0]) < 1800
            ):
                self.connection.execute(
                    """
                    UPDATE prediction_market_event
                    SET fixture_link_conflict='ambiguous_fixture_candidates'
                    WHERE prediction_market_event_id=? AND fixture_id IS NULL
                    """,
                    [event_id],
                )
                continue
            self.connection.execute(
                """
                UPDATE prediction_market_event
                SET fixture_id=?, fixture_link_method='team_names_and_kickoff',
                    fixture_link_confidence=?, fixture_linked_at=?,
                    fixture_link_conflict=NULL
                WHERE prediction_market_event_id=? AND fixture_id IS NULL
                """,
                [
                    candidates[0][1].internal_id,
                    1.0 if len(candidates) == 1 else 0.9,
                    datetime.now(timezone.utc),
                    event_id,
                ],
            )
            linked += 1
        return linked

    def _team_name_in_title(self, team_name: str, normalized_title: str) -> bool:
        canonical = normalized_name(team_name)
        variants = {canonical}
        variants.update(
            alias for alias, mapped in self.warehouse.team_aliases.items() if mapped == canonical
        )
        removable = {"fc", "cf", "afc", "sc", "club"}
        for value in list(variants):
            words = value.split()
            while words and words[0] in removable:
                words.pop(0)
            while words and words[-1] in removable:
                words.pop()
            if words:
                variants.add(" ".join(words))
        padded_title = f" {normalized_title} "
        return any(f" {variant} " in padded_title for variant in variants if variant)

    def _fixture_has_market_tokens(self, fixture_id: str) -> bool:
        return bool(self._market_tokens(fixture_id))

    def _fixture_has_any_market_tokens(self, fixture_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM prediction_market_event e
            JOIN prediction_market m USING (prediction_market_event_id)
            JOIN prediction_market_outcome o USING (prediction_market_id)
            WHERE e.fixture_id = ? AND o.source_token_id IS NOT NULL
            LIMIT 1
            """,
            [fixture_id],
        ).fetchone()
        return bool(row)

    def _market_tokens(
        self, fixture_id: str, *, include_closed: bool = False
    ) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT o.source_token_id
            FROM prediction_market_event e
            JOIN prediction_market m USING (prediction_market_event_id)
            JOIN prediction_market_outcome o USING (prediction_market_id)
            WHERE e.fixture_id = ?
              AND (? OR (coalesce(m.active, true) AND NOT coalesce(m.closed, false)))
              AND o.source_token_id IS NOT NULL
            ORDER BY o.source_token_id
            """,
            [fixture_id, include_closed],
        ).fetchall()
        return [row[0] for row in rows]

    def _checkpoint_done(self, job_key: str) -> bool:
        row = self.connection.execute(
            "SELECT status FROM collection_checkpoint WHERE job_key = ?", [job_key]
        ).fetchone()
        return bool(row and checkpoint_is_stopping(row[0]))

    def _checkpoint_attempted(self, job_key: str) -> bool:
        row = self.connection.execute(
            "SELECT attempts, status FROM collection_checkpoint WHERE job_key = ?",
            [job_key],
        ).fetchone()
        return bool(row and (row[0] > 0 or row[1] != "pending"))

    def _record_checkpoint(
        self,
        job_key: str,
        source: str,
        job_type: str,
        fixture_source_id: str | None,
        scheduled_for: datetime,
        status: str,
        http_status: int | None,
        metadata: dict,
        error: str | None = None,
        *,
        fixture_id: str | None = None,
        priority: int = 2,
        terminal_reason: str | None = None,
        next_attempt_at: datetime | None = None,
        maximum_attempts: int = 1,
    ) -> None:
        attempted_at = datetime.now(timezone.utc)
        if fixture_id is None and fixture_source_id is not None:
            fixture_id = self.warehouse.mapped_id(
                "api_football", "fixture", fixture_source_id
            )
        stopping = checkpoint_is_stopping(status)
        self.connection.execute(
            """
            INSERT INTO collection_checkpoint (
                job_key, source_code, job_type, fixture_source_id, scheduled_for,
                status, attempts, last_attempt_at, completed_at, last_http_status,
                last_error, metadata, updated_at, fixture_id, component_code,
                next_attempt_at, maximum_attempts, priority, terminal_reason,
                last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_key) DO UPDATE SET
                status = excluded.status,
                attempts = collection_checkpoint.attempts + 1,
                last_attempt_at = excluded.last_attempt_at,
                completed_at = excluded.completed_at,
                last_http_status = excluded.last_http_status,
                last_error = excluded.last_error,
                metadata = excluded.metadata,
                fixture_id = coalesce(excluded.fixture_id, collection_checkpoint.fixture_id),
                component_code = coalesce(
                    excluded.component_code, collection_checkpoint.component_code
                ),
                next_attempt_at = excluded.next_attempt_at,
                maximum_attempts = excluded.maximum_attempts,
                priority = excluded.priority,
                terminal_reason = excluded.terminal_reason,
                last_run_id = excluded.last_run_id,
                updated_at = excluded.updated_at
            """,
            [job_key, source, job_type, fixture_source_id, scheduled_for, status,
             attempted_at, attempted_at if stopping else None,
             http_status, error, json_text(metadata), attempted_at, fixture_id,
             component_for_job_type(job_type),
             None if stopping else (next_attempt_at or attempted_at),
             maximum_attempts, priority, terminal_reason, self.current_run_id],
        )

    def _api_budget_available(self, now: datetime) -> bool:
        reset_zone = ZoneInfo(self.api_config.get("quota_reset_timezone", "UTC"))
        local_day = now.astimezone(reset_zone).date()
        start_local = datetime.combine(
            local_day, datetime_time.min, tzinfo=reset_zone
        )
        end_local = start_local + timedelta(days=1)
        used = self.connection.execute(
            """
            SELECT count(*) FROM raw_artifact
            WHERE source_code = 'api_football'
              AND retrieved_at >= ? AND retrieved_at < ?
              AND resource_name != 'status'
            """,
            [start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)],
        ).fetchone()[0]
        usable = int(self.api_config["daily_limit"]) - int(self.api_config["reserve_calls"])
        return used < usable

    def _api_get(
        self,
        resource: str,
        path: str,
        params: dict[str, object],
        now: datetime,
    ) -> tuple[dict, dict, int]:
        def request() -> HttpResponse:
            if not self.api_key:
                raise ProviderResponseError(
                    "API_FOOTBALL_KEY is missing", retryable=False
                )
            if not self._api_budget_available(datetime.now(timezone.utc)):
                raise ProviderResponseError(
                    "API-Football daily call reserve reached", retryable=False
                )
            if self.last_api_request_at is not None:
                remaining = float(self.api_config["minimum_interval_seconds"]) - (
                    time.monotonic() - self.last_api_request_at
                )
                if remaining > 0:
                    time.sleep(remaining)
            response = self.http.get(
                self.API_FOOTBALL_URL, path, params=params,
                headers={"x-apisports-key": self.api_key},
            )
            self.last_api_request_at = time.monotonic()
            self.api_calls += 1
            return response

        result = self.request_executor.execute(
            request,
            validate=self._validated_api_payload,
            response_hook=lambda response: self._store_and_register(
                "api_football", resource, response, params
            ),
            attempt_hook=self._http_attempt_recorder(
                "api_football", resource, sorted(params)
            ),
        )
        return result.value, result.hook_value, result.response.status

    def _polymarket_get(
        self, resource: str, path: str, params: dict[str, object]
    ) -> tuple[object, dict, int]:
        def request() -> HttpResponse:
            response = self.http.get(self.POLYMARKET_GAMMA_URL, path, params=params)
            self.polymarket_calls += 1
            return response

        result = self.request_executor.execute(
            request,
            validate=lambda response: self._validated_json(
                response, "Polymarket Gamma"
            ),
            response_hook=lambda response: self._store_and_register(
                "polymarket_gamma", resource, response, params
            ),
            attempt_hook=self._http_attempt_recorder(
                "polymarket_gamma", resource, sorted(params)
            ),
        )
        return result.value, result.hook_value, result.response.status

    def _polymarket_post(
        self, resource: str, path: str, payload: object
    ) -> tuple[object, dict, int]:
        token_ids = (
            [
                str(item["token_id"])
                for item in payload
                if isinstance(item, dict) and item.get("token_id")
            ]
            if isinstance(payload, list)
            else []
        )
        request_metadata = {"token_count": len(token_ids), "token_ids": token_ids}
        def request() -> HttpResponse:
            response = self.http.post_json(self.POLYMARKET_CLOB_URL, path, payload)
            self.polymarket_calls += 1
            return response

        result = self.request_executor.execute(
            request,
            validate=lambda response: self._validated_json(
                response, "Polymarket CLOB"
            ),
            response_hook=lambda response: self._store_and_register(
                "polymarket_clob", resource, response, request_metadata
            ),
            attempt_hook=self._http_attempt_recorder(
                "polymarket_clob", resource, sorted(request_metadata)
            ),
        )
        return result.value, result.hook_value, result.response.status

    def _validated_api_payload(self, response: HttpResponse) -> dict:
        payload = self._validated_json(response, "API-Football")
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                "API-Football returned an invalid response shape"
            )
        if payload.get("errors"):
            text = json.dumps(payload["errors"], ensure_ascii=True).lower()
            permanent = any(
                marker in text
                for marker in (
                    "key", "token", "account", "subscription", "plan",
                    "permission", "auth",
                )
            )
            raise ProviderResponseError(
                "API-Football provider reported an error",
                retryable=not permanent,
            )
        return payload

    @staticmethod
    def _validated_json(response: HttpResponse, source: str) -> object:
        if response.status != 200:
            raise ProviderResponseError(
                f"{source} returned an unexpected success status",
                retryable=False,
            )
        try:
            return response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderResponseError(
                f"{source} returned invalid JSON", retryable=True
            ) from error

    def _store_and_register(
        self,
        source: str,
        resource: str,
        response: HttpResponse,
        params: dict[str, object],
    ) -> dict:
        artifact = self.raw_store.store(
            source=source, resource=resource, response=response, request_params=params
        )
        item = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
        item["_metadata_path"] = artifact.metadata_path
        item["_raw_artifact_id"] = metadata_artifact_id(artifact.metadata_path)
        RawCatalog.register_item(self.warehouse, item)
        return item
