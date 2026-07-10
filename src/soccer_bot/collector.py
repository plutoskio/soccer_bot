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
)
from .database import Warehouse, json_text, normalized_name
from .http import HttpClient, HttpResponse
from .loaders import RawCatalog, WarehouseLoader, metadata_artifact_id, parse_datetime
from .raw_store import RawArtifactStore


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
    ) -> None:
        if not api_key:
            raise ValueError("API_FOOTBALL_KEY is missing")
        self.warehouse = warehouse
        self.connection = warehouse.connection
        self.raw_store = raw_store
        self.http = http_client
        self.api_key = api_key
        self.config = config
        self.zone = ZoneInfo(config["timezone"])
        self.discovery_config = config.get("discovery", {})
        self.api_config = config["api_football"]
        self.polymarket_config = config["polymarket"]
        competitions = config["competitions"]
        self.monitored_league_ids = {str(value) for value in competitions["league_ids"]}
        self.monitored_competition_keys = {
            self._competition_key(*value.split("|", 1))
            for value in competitions["competition_keys"]
        }
        self.loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
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
            today_fixtures = [
                fixture for fixture in fixtures
                if fixture.kickoff.astimezone(self.zone).date() == local_date
            ]
            self.summary["selected_fixtures"] = len(fixtures)
            self._discover_polymarket(local_date, today_fixtures, now, dry_run)
            if not dry_run:
                self._reconcile_fixture_components(fixtures, now)
                self._mark_expired_pregame_components(fixtures, now)
            detail_jobs = self._plan_detail_jobs(fixtures, now)
            self.summary["planned_jobs"].extend(job.job_key for job in detail_jobs)
            if not dry_run:
                self._execute_detail_jobs(detail_jobs, now)
                self._reconcile_fixture_components(fixtures, now)
                self._mark_expired_pregame_components(fixtures, now)
                self.summary["linked_polymarket_events"] = self._link_polymarket_events(fixtures)
            market_jobs = self._plan_market_jobs(fixtures, now)
            self.summary["planned_jobs"].extend(job.job_key for job in market_jobs)
            if not dry_run:
                self._execute_market_jobs(market_jobs, now)
            self.summary["api_football_calls"] = self.api_calls
            self.summary["polymarket_calls"] = self.polymarket_calls
            if not dry_run:
                self.connection.execute(
                    """
                    UPDATE collection_run SET finished_at = ?, status = 'completed',
                        api_football_calls = ?, polymarket_calls = ?, summary = ?
                    WHERE collection_run_id = ?
                    """,
                    [datetime.now(timezone.utc), self.api_calls, self.polymarket_calls,
                     json_text(self.summary), run_id],
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
            if not checkpoint_is_stopping(status):
                continue
            component = component_for_job_type(job_type)
            if component:
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
                started_at, status, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            [
                attempt_id, job_key, self.current_run_id, number,
                source, job_type, fixture_id, now, json_text(metadata),
            ],
        )
        return attempt_id

    def _finish_attempts(
        self,
        attempt_ids: dict[str, str],
        *,
        status: str,
        finished_at: datetime,
        http_status: int | None = None,
        raw_artifact_id: str | None = None,
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
                    raw_artifact_id = ?, error_class = ?, error_message = ?,
                    metadata = coalesce(?, metadata)
                WHERE collection_attempt_id = ?
                """,
                [
                    finished_at, status, http_status, raw_artifact_id,
                    error_class, error_message,
                    json_text(metadata) if metadata is not None else None,
                    attempt_id,
                ],
            )

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
        """Use the latest monitored final fixture as a recovery expansion hint.

        A completed fixture does not prove discovery coverage for surrounding
        dates, so the configured recovery window remains in force. This only
        expands the window when the database's latest monitored final fixture
        is older than that safety window.
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
            self._discover_fixtures(job, now, dry_run)

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
            self._finish_attempts(
                {job_key: attempt_id} if attempt_id else {},
                status="retryable_error",
                finished_at=now,
                error=error,
                metadata=request_metadata,
            )
            self._record_checkpoint(
                job_key,
                "api_football",
                "fixture_discovery",
                None,
                job.scheduled_for,
                "failed",
                None,
                request_metadata,
                error=str(error),
                priority=job.priority,
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
            if status in {"postponed", "cancelled"}:
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
                self._finish_attempts(
                    {job.job_key: attempt_id} if attempt_id else {},
                    status="retryable_error",
                    finished_at=now,
                    error=error,
                    metadata=request_metadata,
                )
                self._record_checkpoint(
                    job.job_key,
                    "api_football",
                    "fixture_refresh",
                    job.fixture.source_id,
                    job.scheduled_for,
                    "failed",
                    None,
                    request_metadata,
                    error=str(error),
                    fixture_id=job.fixture.internal_id,
                    priority=1,
                )
                raise
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
        job_key = f"polymarket:event_discovery:{local_date.isoformat()}"
        if self._checkpoint_done(job_key):
            return
        self.summary["planned_jobs"].append(job_key)
        if dry_run:
            return
        start = datetime.combine(local_date, datetime_time.min, self.zone).astimezone(timezone.utc)
        end = datetime.combine(local_date, datetime_time.max, self.zone).astimezone(timezone.utc)
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
                self._finish_attempts(
                    {job_key: attempt_id} if attempt_id else {},
                    status="retryable_error",
                    finished_at=datetime.now(timezone.utc),
                    error=error,
                    metadata={"page": page, "parameters": params},
                )
                self._record_checkpoint(
                    job_key, "polymarket_gamma", "event_discovery", None, now,
                    "failed", None, {"page": page}, error=str(error),
                )
                raise
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

    def _plan_detail_jobs(self, fixtures: list[FixtureRecord], now: datetime) -> list[DetailJob]:
        jobs: list[DetailJob] = []
        lineup_offsets = self.api_config.get("lineup_stage_offsets", [50, 35, 20, 5])
        first_post = int(self.api_config["post_match_first_check_minutes"])
        retry_post = int(self.api_config["post_match_retry_minutes"])
        for fixture in fixtures:
            schedule_version, schedule_observation_id, kickoff = (
                self._lineup_schedule_version(fixture)
            )
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

            kickoff_key = int(kickoff.timestamp())
            post_primary = f"api_football:postmatch_primary:{fixture.source_id}:{kickoff_key}"
            post_retry = f"api_football:postmatch_retry:{fixture.source_id}:{kickoff_key}"
            post_stage = postmatch_stage(
                now=now, kickoff=kickoff,
                data_complete=self._postmatch_complete(fixture.internal_id, now),
                primary_attempted=self._checkpoint_attempted(post_primary),
                retry_attempted=self._checkpoint_attempted(post_retry),
                first_check_minutes=first_post, retry_minutes=retry_post,
            )
            if post_stage:
                key = post_primary if post_stage == "postmatch_primary" else post_retry
                scheduled = kickoff + timedelta(
                    minutes=first_post if post_stage == "postmatch_primary" else retry_post
                )
                jobs.append(DetailJob(key, post_stage, schedule_fixture, scheduled))
        return jobs

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
                self._finish_attempts(
                    attempt_ids,
                    status="retryable_error",
                    finished_at=datetime.now(timezone.utc),
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
                        "failed",
                        None,
                        {"fixture_source_ids": batch},
                        error=str(error),
                        fixture_id=job.fixture.internal_id,
                    )
                raise
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
                    elif job.job_type.startswith("lineup"):
                        result = validate_lineups(
                            self.connection,
                            job.fixture.internal_id,
                            "api_football",
                            now,
                            schedule_kickoff=job.fixture.kickoff,
                        )
                        state = self._checkpoint_state_for_component(result)
                        if state == "succeeded":
                            self._record_pregame_lineup_capture(
                                job.fixture,
                                result,
                                now,
                                job.schedule_observation_id,
                            )
                        metadata = {
                            "lineup_state": result.state,
                            "lineup_job_key": job.job_key,
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
                    self._record_checkpoint(
                        job.job_key, "api_football", job.job_type, source_id,
                        job.scheduled_for, state, status, metadata,
                        fixture_id=job.fixture.internal_id,
                    )
                    attempt_id = attempt_ids.get(job.job_key)
                    if attempt_id:
                        self._finish_attempts(
                            {job.job_key: attempt_id},
                            status="succeeded" if state == "succeeded" else "incomplete",
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
        prekick_minutes = int(self.polymarket_config["prekick_snapshot_minutes"])
        for fixture in fixtures:
            if not self._fixture_has_market_tokens(fixture.internal_id):
                continue
            kickoff_key = int(fixture.kickoff.timestamp())
            lineup_key = f"polymarket:lineup_snapshot:{fixture.source_id}:{kickoff_key}"
            if (
                self._lineup_complete(fixture.internal_id, now)
                and now < fixture.kickoff
                and not self._checkpoint_done(lineup_key)
            ):
                jobs.append(DetailJob(lineup_key, "lineup_snapshot", fixture, now))
            prekick_key = f"polymarket:prekick_snapshot:{fixture.source_id}:{kickoff_key}"
            prekick_at = fixture.kickoff - timedelta(minutes=prekick_minutes)
            if prekick_at <= now <= fixture.kickoff + timedelta(minutes=5) and not self._checkpoint_done(prekick_key):
                jobs.append(DetailJob(prekick_key, "prekick_snapshot", fixture, prekick_at))
        return jobs

    def _execute_market_jobs(self, jobs: list[DetailJob], now: datetime) -> None:
        if not jobs:
            return
        tokens_by_fixture = {
            job.fixture.internal_id: self._market_tokens(job.fixture.internal_id)
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
                self._finish_attempts(
                    attempt_ids,
                    status="retryable_error",
                    finished_at=datetime.now(timezone.utc),
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
                        "failed",
                        None,
                        {"token_count": len(batch)},
                        error=str(error),
                        fixture_id=job.fixture.internal_id,
                    )
                raise
            last_status = status
            self.loader.load_polymarket_payload("order_books_batch", payload, item)
            if isinstance(payload, list):
                received.update(
                    str(book.get("asset_id")) for book in payload if isinstance(book, dict)
                )
        for job in jobs:
            tokens = tokens_by_fixture[job.fixture.internal_id]
            if not tokens:
                continue
            complete = set(tokens).issubset(received)
            self._record_checkpoint(
                job.job_key, "polymarket_clob", job.job_type, job.fixture.source_id,
                job.scheduled_for, "succeeded" if complete else "incomplete", last_status,
                {"requested_tokens": len(tokens), "received_tokens": len(received)},
                fixture_id=job.fixture.internal_id,
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
                        "received_tokens": len(received),
                    },
                )
            self.summary["executed_jobs"].append(job.job_key)

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
                market_captured_before_kickoff = bool(
                    self.connection.execute(
                        """
                        SELECT 1
                        FROM orderbook_snapshot ob
                        JOIN prediction_market_outcome o
                          ON o.source_token_id = ob.source_token_id
                        JOIN prediction_market m
                          ON m.prediction_market_id = o.prediction_market_id
                        JOIN prediction_market_event e
                          ON e.prediction_market_event_id = m.prediction_market_event_id
                        WHERE e.fixture_id = ? AND ob.retrieved_at < ?
                        LIMIT 1
                        """,
                        [schedule_fixture.internal_id, kickoff],
                    ).fetchone()
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
            SELECT prediction_market_event_id, title, coalesce(start_time, end_time)
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
            self.connection.execute(
                "UPDATE prediction_market_event SET fixture_id = ? WHERE prediction_market_event_id = ?",
                [candidates[0][1].internal_id, event_id],
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

    def _market_tokens(self, fixture_id: str) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT o.source_token_id
            FROM prediction_market_event e
            JOIN prediction_market m USING (prediction_market_event_id)
            JOIN prediction_market_outcome o USING (prediction_market_id)
            WHERE e.fixture_id = ? AND coalesce(m.active, true)
              AND NOT coalesce(m.closed, false) AND o.source_token_id IS NOT NULL
            ORDER BY o.source_token_id
            """,
            [fixture_id],
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
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
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
                priority = excluded.priority,
                terminal_reason = excluded.terminal_reason,
                last_run_id = excluded.last_run_id,
                updated_at = excluded.updated_at
            """,
            [job_key, source, job_type, fixture_source_id, scheduled_for, status,
             attempted_at, attempted_at if stopping else None,
             http_status, error, json_text(metadata), attempted_at, fixture_id,
             component_for_job_type(job_type), None if stopping else attempted_at,
             priority, terminal_reason, self.current_run_id],
        )

    def _api_budget_available(self, now: datetime) -> bool:
        used = self.connection.execute(
            """
            SELECT count(*) FROM raw_artifact
            WHERE source_code = 'api_football' AND CAST(retrieved_at AS DATE) = ?
              AND resource_name != 'status'
            """,
            [now.date()],
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
        if not self._api_budget_available(now):
            raise RuntimeError("API-Football daily call reserve reached")
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
        item = self._store_and_register("api_football", resource, response, params)
        payload = self._validated_json(response, "API-Football")
        if not isinstance(payload, dict):
            raise RuntimeError("API-Football returned a non-object response")
        if payload.get("errors"):
            raise RuntimeError(f"API-Football returned errors: {payload['errors']}")
        return payload, item, response.status

    def _polymarket_get(
        self, resource: str, path: str, params: dict[str, object]
    ) -> tuple[object, dict, int]:
        response = self.http.get(self.POLYMARKET_GAMMA_URL, path, params=params)
        self.polymarket_calls += 1
        item = self._store_and_register("polymarket_gamma", resource, response, params)
        return self._validated_json(response, "Polymarket Gamma"), item, response.status

    def _polymarket_post(
        self, resource: str, path: str, payload: object
    ) -> tuple[object, dict, int]:
        response = self.http.post_json(self.POLYMARKET_CLOB_URL, path, payload)
        self.polymarket_calls += 1
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
        item = self._store_and_register(
            "polymarket_clob", resource, response, request_metadata
        )
        return self._validated_json(response, "Polymarket CLOB"), item, response.status

    @staticmethod
    def _validated_json(response: HttpResponse, source: str) -> object:
        if response.status != 200:
            raise RuntimeError(f"{source} request failed with HTTP {response.status}")
        try:
            return response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"{source} returned invalid JSON") from error

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
