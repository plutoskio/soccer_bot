from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo


UTC = timezone.utc


@dataclass(frozen=True)
class DiscoveryJob:
    target_date: date
    cadence: str
    slot: str
    job_key: str
    scheduled_for: datetime
    priority: int
    reason: str


@dataclass(frozen=True)
class LineupStagePlan:
    stage: str
    offset_minutes: int
    stage_time: datetime
    job_key: str
    schedule_version: str


@dataclass(frozen=True)
class PostmatchStagePlan:
    stage: str
    offset_minutes: int
    stage_time: datetime
    job_key: str
    component_code: str | None = None


@dataclass(frozen=True)
class MarketStagePlan:
    stage: str
    stage_time: datetime
    job_key: str
    include_closed: bool = False
    capture_target_at: datetime | None = None
    capture_deadline_at: datetime | None = None


def effective_recovery_days(
    configured_days: int,
    catch_up_days: int | None = None,
    completed_frontier_days: int | None = None,
) -> int:
    """Return the past-date bound without allowing a caller to shrink it."""
    configured = _nonnegative_int(configured_days, "recovery_days")
    requested = 0 if catch_up_days is None else _nonnegative_int(
        catch_up_days, "catch_up_days"
    )
    frontier = 0 if completed_frontier_days is None else _nonnegative_int(
        completed_frontier_days, "completed_frontier_days"
    )
    return max(configured, requested, frontier)


def discovery_date_window(
    today: date,
    *,
    recovery_days: int,
    planning_days: int,
    catch_up_days: int | None = None,
    completed_frontier_days: int | None = None,
) -> tuple[date, date, list[date]]:
    """Build the inclusive local-date discovery window."""
    past_days = effective_recovery_days(
        recovery_days, catch_up_days, completed_frontier_days
    )
    future_days = _nonnegative_int(planning_days, "planning_days")
    start = today - timedelta(days=past_days)
    end = today + timedelta(days=future_days)
    dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    return start, end, dates


def discovery_job_for_date(
    target_date: date,
    *,
    today: date,
    now: datetime,
    zone: ZoneInfo,
    today_tomorrow_hours: int = 6,
) -> DiscoveryJob:
    """Create the immutable cadence-slot key for one target date."""
    now = now.astimezone(UTC)
    local_now = now.astimezone(zone)
    if target_date < today:
        cadence = "recovery"
        slot = f"recovery:{target_date.isoformat()}"
        priority = 0
        reason = "missing_or_recovery_discovery"
    elif target_date <= today + timedelta(days=1):
        hours = _positive_int(today_tomorrow_hours, "today_tomorrow_hours")
        slot_start = _floor_local_time(local_now, hours)
        cadence = "six_hour"
        slot = f"six_hour:{slot_start.strftime('%Y-%m-%dT%H:%M')}"
        priority = 1 if target_date == today else 2
        reason = "intraday_discovery_refresh"
    else:
        cadence = "daily"
        slot = f"daily:{local_now.date().isoformat()}"
        priority = 3
        reason = "future_daily_discovery_refresh"
    return DiscoveryJob(
        target_date=target_date,
        cadence=cadence,
        slot=slot,
        job_key=(
            f"api_football:fixture_discovery:{target_date.isoformat()}"
            f":{cadence}:{slot.split(':', 1)[1]}"
        ),
        scheduled_for=now,
        priority=priority,
        reason=reason,
    )


def fixture_refresh_job_key(
    fixture_source_id: str,
    *,
    reason: str,
    slot: str,
) -> str:
    """Build an immutable key for a fixture-specific schedule refresh."""
    return f"api_football:fixture_refresh:{fixture_source_id}:{reason}:{slot}"


def lineup_stage_job_key(
    fixture_source_id: str, schedule_version: str, offset_minutes: int
) -> str:
    return (
        f"api_football:lineup_stage:{fixture_source_id}:"
        f"{schedule_version}:{int(offset_minutes)}"
    )


def lineup_stage_plans(
    *,
    fixture_source_id: str,
    schedule_version: str,
    kickoff: datetime,
    now: datetime,
    offsets: list[int],
    attempted_job_keys: set[str],
    lineup_complete: bool,
) -> list[LineupStagePlan]:
    """Return only the latest due stage for one schedule version.

    Earlier elapsed stages are missed scheduling opportunities, not attempts.
    One current HTTP response must never be recorded as several historical
    stage attempts.
    """
    if lineup_complete:
        return []
    kickoff = kickoff.astimezone(UTC)
    now = now.astimezone(UTC)
    if now >= kickoff:
        return []
    normalized_offsets = _lineup_offsets(offsets)
    due_offsets = [
        offset
        for offset in normalized_offsets
        if now >= kickoff - timedelta(minutes=offset)
    ]
    if not due_offsets:
        return []
    offset = min(due_offsets)
    stage_time = kickoff - timedelta(minutes=offset)
    job_key = lineup_stage_job_key(fixture_source_id, schedule_version, offset)
    if job_key in attempted_job_keys:
        return []
    return [
        LineupStagePlan(
            stage=f"lineup_t_minus_{offset}",
            offset_minutes=offset,
            stage_time=stage_time,
            job_key=job_key,
            schedule_version=schedule_version,
        )
    ]


def postmatch_stage_plans(
    *,
    fixture_source_id: str,
    schedule_version: str,
    kickoff: datetime,
    now: datetime,
    canonical_status: str,
    components_complete: bool,
    attempted_job_keys: set[str],
    first_check_minutes: int = 150,
    live_poll_minutes: int = 30,
    live_poll_until_minutes: int = 360,
    final_retry_minutes: int = 480,
    correction_offsets_minutes: list[int] | None = None,
) -> list[PostmatchStagePlan]:
    """Plan concrete status/detail and correction slots for one schedule.

    Jobs are keyed by the observed kickoff version. Terminal fixture statuses
    produce no sporting-data work. Correction refreshes are independent of
    factual completeness and therefore remain due after an early complete
    response.
    """
    kickoff = kickoff.astimezone(UTC)
    now = now.astimezone(UTC)
    first = _positive_int(first_check_minutes, "post_match_first_check_minutes")
    poll = _positive_int(live_poll_minutes, "post_match_live_poll_minutes")
    poll_until = _positive_int(
        live_poll_until_minutes, "post_match_live_poll_until_minutes"
    )
    final_retry = _positive_int(final_retry_minutes, "post_match_final_retry_minutes")
    corrections = correction_offsets_minutes or [1440, 4320]
    corrections = sorted({_positive_int(value, "correction offset") for value in corrections})
    if poll_until < first:
        raise ValueError("post_match_live_poll_until_minutes must not precede first check")

    terminal = {
        "postponed", "cancelled", "abandoned", "administrative_result"
    }
    if canonical_status in terminal:
        return []

    plans: list[PostmatchStagePlan] = []

    def add(stage: str, offset: int, component: str | None = None) -> None:
        key = (
            f"api_football:{stage}:{fixture_source_id}:"
            f"{schedule_version}:{offset}"
        )
        stage_time = kickoff + timedelta(minutes=offset)
        if now >= stage_time and key not in attempted_job_keys:
            plans.append(PostmatchStagePlan(stage, offset, stage_time, key, component))

    completed_correction_offsets = []
    for key in attempted_job_keys:
        if ":correction_refresh_" not in key:
            continue
        try:
            completed_correction_offsets.append(int(key.rsplit(":", 1)[1]))
        except (TypeError, ValueError):
            continue
    latest_completed_correction = max(completed_correction_offsets, default=0)
    if latest_completed_correction == 0:
        add("postmatch_status", first)

        if canonical_status in {"scheduled", "live", "delayed", "unknown"}:
            for offset in range(first + poll, poll_until + 1, poll):
                add("postmatch_status", offset)
        elif canonical_status == "suspended":
            # Suspended fixtures remain retryable through the separately
            # configured fixture-status refresh cadence.
            pass
        elif canonical_status == "final" and not components_complete:
            add("postmatch_final_retry", final_retry)

    for offset in corrections:
        if offset <= latest_completed_correction:
            continue
        hours = offset // 60
        add(
            f"correction_refresh_{hours}h",
            offset,
            f"correction_refresh_{hours}h",
        )

    if not plans:
        return []
    # A single current response must not be labeled as several historical poll
    # slots after downtime. Execute only the latest due stage for this fixture.
    return [max(plans, key=lambda plan: (plan.stage_time, plan.job_key))]


def market_stage_plans(
    *,
    fixture_source_id: str,
    schedule_version: str,
    kickoff: datetime,
    now: datetime,
    offsets_minutes: list[int],
    stage_window_minutes: int,
    lineup_complete: bool,
    attempted_job_keys: set[str],
    closure_delay_minutes: int = 180,
) -> list[MarketStagePlan]:
    kickoff = kickoff.astimezone(UTC)
    now = now.astimezone(UTC)
    window = _positive_int(stage_window_minutes, "market stage window")
    offsets = sorted(
        {_positive_int(value, "market snapshot offset") for value in offsets_minutes},
        reverse=True,
    )
    plans: list[MarketStagePlan] = []
    for offset in offsets:
        stage = f"market_t_minus_{offset}"
        capture_target = kickoff - timedelta(minutes=offset)
        stage_time = capture_target - timedelta(minutes=window)
        key = f"polymarket:{stage}:{fixture_source_id}:{schedule_version}"
        if (
            stage_time <= now < capture_target
            and key not in attempted_job_keys
        ):
            plans.append(
                MarketStagePlan(
                    stage,
                    stage_time,
                    key,
                    capture_target_at=capture_target,
                    capture_deadline_at=capture_target,
                )
            )

    lineup_stage = "market_after_lineup"
    lineup_key = f"polymarket:{lineup_stage}:{fixture_source_id}:{schedule_version}"
    if lineup_complete and now < kickoff and lineup_key not in attempted_job_keys:
        # Give the lineup-triggered capture its own artifact/stage. A following
        # five-minute scheduler run can still capture a coincident timed stage.
        plans = [
            MarketStagePlan(
                lineup_stage,
                now,
                lineup_key,
                capture_target_at=kickoff,
                capture_deadline_at=kickoff,
            )
        ]

    closure_delay = _positive_int(closure_delay_minutes, "market closure delay")
    closure_time = kickoff + timedelta(minutes=closure_delay)
    closure_stage = "market_after_closure"
    closure_key = f"polymarket:{closure_stage}:{fixture_source_id}:{schedule_version}"
    if now >= closure_time and closure_key not in attempted_job_keys:
        plans.append(
            MarketStagePlan(
                closure_stage, closure_time, closure_key, include_closed=True
            )
        )
    return sorted(plans, key=lambda plan: (plan.stage_time, plan.job_key))


def validate_collector_config(config: dict, catch_up_days: int | None = None) -> None:
    """Fail before database or network work when collector policy is invalid."""
    if not isinstance(config, dict):
        raise ValueError("collector config must be an object")
    discovery = config.get("discovery")
    api = config.get("api_football")
    if not isinstance(discovery, dict) or not isinstance(api, dict):
        raise ValueError("collector config requires discovery and api_football objects")
    effective_recovery_days(discovery.get("recovery_days", 14), catch_up_days)
    _nonnegative_int(discovery.get("planning_days", 7), "planning_days")
    _positive_int(
        discovery.get("today_tomorrow_refresh_hours", 6),
        "today_tomorrow_refresh_hours",
    )
    _positive_int(
        discovery.get("near_kickoff_refresh_minutes", 120),
        "near_kickoff_refresh_minutes",
    )
    status_hours = _positive_int(
        discovery.get("postponed_cancelled_refresh_hours", 6),
        "postponed_cancelled_refresh_hours",
    )
    if status_hours > 24:
        raise ValueError("postponed_cancelled_refresh_hours must not exceed 24")
    batch_size = _positive_int(api.get("fixture_batch_size", 20), "fixture_batch_size")
    if batch_size > 20:
        raise ValueError("fixture_batch_size must not exceed 20")
    _lineup_offsets(api.get("lineup_stage_offsets", [50, 35, 20, 5]))
    first_post = _positive_int(
        api.get("post_match_first_check_minutes", 150),
        "post_match_first_check_minutes",
    )
    poll = _positive_int(
        api.get("post_match_live_poll_minutes", 30),
        "post_match_live_poll_minutes",
    )
    poll_until = _positive_int(
        api.get("post_match_live_poll_until_minutes", 360),
        "post_match_live_poll_until_minutes",
    )
    if poll_until < first_post or (poll_until - first_post) % poll:
        raise ValueError("post-match live polling window must align to its interval")
    _positive_int(
        api.get("post_match_final_retry_minutes", 480),
        "post_match_final_retry_minutes",
    )
    correction_offsets = api.get("correction_refresh_offsets_minutes", [1440, 4320])
    if not isinstance(correction_offsets, list) or not correction_offsets:
        raise ValueError("correction_refresh_offsets_minutes must be a non-empty list")
    normalized_corrections = [
        _positive_int(value, "correction refresh offset")
        for value in correction_offsets
    ]
    if len(set(normalized_corrections)) != len(normalized_corrections):
        raise ValueError("correction_refresh_offsets_minutes must not contain duplicates")
    try:
        ZoneInfo(str(api.get("quota_reset_timezone", "UTC")))
    except Exception as error:
        raise ValueError("quota_reset_timezone must be a valid IANA timezone") from error
    retry = config.get("retry", {})
    if not isinstance(retry, dict):
        raise ValueError("retry config must be an object")
    _positive_int(
        retry.get("maximum_inline_attempts", 3), "maximum_inline_attempts"
    )
    for key, default in (
        ("maximum_inline_retry_seconds", 5),
        ("backoff_base_seconds", 1),
        ("backoff_cap_seconds", 60),
        ("jitter_seconds", 0.25),
    ):
        value = float(retry.get(key, default))
        if value < 0:
            raise ValueError(f"{key} must not be negative")
    lock = config.get("lock", {})
    if not isinstance(lock, dict):
        raise ValueError("lock config must be an object")
    stale = _positive_int(
        lock.get("stale_timeout_seconds", 900), "stale_timeout_seconds"
    )
    heartbeat = _positive_int(
        lock.get("heartbeat_interval_seconds", 30),
        "heartbeat_interval_seconds",
    )
    if heartbeat >= stale:
        raise ValueError("heartbeat_interval_seconds must be below stale timeout")
    polymarket = config.get("polymarket")
    if not isinstance(polymarket, dict):
        raise ValueError("polymarket config must be an object")
    offsets = polymarket.get("snapshot_offsets_minutes", [1440, 360, 90, 15, 5])
    if not isinstance(offsets, list) or not offsets:
        raise ValueError("snapshot_offsets_minutes must be a non-empty list")
    normalized_market_offsets = [
        _positive_int(value, "market snapshot offset") for value in offsets
    ]
    if len(set(normalized_market_offsets)) != len(normalized_market_offsets):
        raise ValueError("snapshot_offsets_minutes must not contain duplicates")
    _positive_int(
        polymarket.get("closure_snapshot_delay_minutes", 180),
        "closure_snapshot_delay_minutes",
    )
    maximum_attempts = _positive_int(
        polymarket.get("snapshot_maximum_attempts", 3),
        "snapshot_maximum_attempts",
    )
    retry_minutes = _positive_int(
        polymarket.get("snapshot_retry_minutes", 15),
        "snapshot_retry_minutes",
    )
    window_minutes = _positive_int(
        polymarket.get("snapshot_stage_window_minutes", 10),
        "snapshot_stage_window_minutes",
    )
    if window_minutes <= retry_minutes * (maximum_attempts - 1):
        raise ValueError(
            "snapshot_stage_window_minutes must permit every configured retry "
            "strictly before the market cutoff"
        )
    mapping_path = polymarket.get("mapping_policy_path")
    if not isinstance(mapping_path, str) or not mapping_path.strip():
        raise ValueError("mapping_policy_path must be a non-empty path")
    if Path(mapping_path).is_absolute() or ".." in Path(mapping_path).parts:
        raise ValueError("mapping_policy_path must stay inside the repository")
    for key, default in (
        ("discovery_hourly_minutes", 60),
        ("discovery_matchday_minutes", 15),
        ("live_refresh_minutes", 10),
    ):
        interval = _positive_int(polymarket.get(key, default), key)
        if interval > 60 or 60 % interval:
            raise ValueError(f"{key} must evenly divide one hour")
    live_lookahead = _positive_int(
        polymarket.get("live_lookahead_hours", 72), "live_lookahead_hours"
    )
    live_max_age = _positive_int(
        polymarket.get("live_max_age_minutes", 20), "live_max_age_minutes"
    )
    if live_lookahead > 168:
        raise ValueError("live_lookahead_hours must not exceed seven days")
    if live_max_age < int(polymarket.get("live_refresh_minutes", 10)):
        raise ValueError("live_max_age_minutes must cover one refresh interval")
    health = config.get("health", {})
    if not isinstance(health, dict):
        raise ValueError("health config must be an object")
    report_directory = health.get("report_directory", "reports/collector")
    if not isinstance(report_directory, str) or not report_directory.strip():
        raise ValueError("health report_directory must be a non-empty path")
    if Path(report_directory).is_absolute() or ".." in Path(report_directory).parts:
        raise ValueError("health report_directory must stay inside the repository")
    operations = config.get("operations", {})
    if not isinstance(operations, dict):
        raise ValueError("operations config must be an object")
    if operations.get("enabled", False):
        operations_report = operations.get(
            "report_directory", "data/reports/operations"
        )
        if not isinstance(operations_report, str) or not operations_report.strip():
            raise ValueError("operations report_directory must be a non-empty path")
        operations_path = Path(operations_report)
        if operations_path.is_absolute() or ".." in operations_path.parts:
            raise ValueError(
                "operations report_directory must stay inside the repository"
            )
        _positive_int(
            operations.get("publication_stale_after_seconds", 1200),
            "operations publication_stale_after_seconds",
        )
        _positive_int(
            operations.get("cycle_stale_after_seconds", 1200),
            "operations cycle_stale_after_seconds",
        )
        warning = float(operations.get("volume_warning_percent", 80))
        critical = float(operations.get("volume_critical_percent", 95))
        if not 0 < warning < critical <= 100:
            raise ValueError(
                "operations volume thresholds must satisfy 0 < warning < critical <= 100"
            )
        if not isinstance(operations.get("fail_run_on_critical", True), bool):
            raise ValueError("operations fail_run_on_critical must be boolean")
    publication = config.get("prediction_publication", {})
    if not isinstance(publication, dict):
        raise ValueError("prediction_publication config must be an object")
    if publication.get("enabled", False):
        for key in (
            "model_version",
            "logical_model_sha256",
            "model_path",
            "model_config_path",
            "reproducibility_sha256",
            "output_directory",
            "report_directory",
        ):
            value = publication.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"prediction_publication {key} must be non-empty")
        logical_hash = publication["logical_model_sha256"]
        if len(logical_hash) != 64 or any(
            character not in "0123456789abcdef" for character in logical_hash
        ):
            raise ValueError(
                "prediction_publication logical_model_sha256 must be lowercase SHA-256"
            )
        reproducibility_hash = publication["reproducibility_sha256"]
        if len(reproducibility_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in reproducibility_hash
        ):
            raise ValueError(
                "prediction_publication reproducibility_sha256 must be lowercase SHA-256"
            )
        for key in (
            "model_path",
            "model_config_path",
            "output_directory",
            "report_directory",
        ):
            path = Path(publication[key])
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"prediction_publication {key} must stay inside the repository"
                )
        platform = publication.get("specialized_platform", {})
        if not isinstance(platform, dict):
            raise ValueError("specialized_platform must be an object")
        if platform.get("enabled", False):
            for key in ("output_directory", "object_key"):
                value = platform.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"specialized_platform {key} must be non-empty")
                path = Path(value)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(
                        f"specialized_platform {key} must stay inside the repository"
                    )
            _positive_int(
                platform.get("minimum_state_rows", 1),
                "specialized_platform minimum_state_rows",
            )
            _positive_int(
                platform.get("timeout_seconds", 240),
                "specialized_platform timeout_seconds",
            )
        market_evidence = publication.get("polymarket_market_evidence", {})
        if not isinstance(market_evidence, dict):
            raise ValueError("polymarket_market_evidence must be an object")
        if market_evidence.get("enabled", False):
            for key in ("policy_path", "output_directory"):
                value = market_evidence.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"polymarket_market_evidence {key} must be non-empty"
                    )
                if Path(value).is_absolute() or ".." in Path(value).parts:
                    raise ValueError(
                        f"polymarket_market_evidence {key} must stay inside the repository"
                    )
            policy_hash = market_evidence.get("policy_sha256")
            if not isinstance(policy_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", policy_hash
            ):
                raise ValueError(
                    "polymarket_market_evidence policy_sha256 must be lowercase SHA-256"
                )
            required_offsets = {4320, 1440}
            if not required_offsets.issubset(set(normalized_market_offsets)):
                raise ValueError(
                    "Polymarket evidence requires T-72h and T-24h capture offsets"
                )
        _positive_int(
            publication.get("minimum_prediction_rows", 1),
            "prediction publication minimum_prediction_rows",
        )
        _positive_int(
            publication.get("timeout_seconds", 240),
            "prediction publication timeout_seconds",
        )
        player_shadow = publication.get("confirmed_lineup_player_shadow", {})
        if not isinstance(player_shadow, dict):
            raise ValueError("confirmed_lineup_player_shadow must be an object")
        if player_shadow.get("enabled", False):
            for key in (
                "model_version",
                "logical_model_sha256",
                "model_path",
                "config_path",
                "config_sha256",
                "output_directory",
            ):
                value = player_shadow.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"confirmed_lineup_player_shadow {key} must be non-empty"
                    )
                if key not in {"logical_model_sha256", "config_sha256"}:
                    path = Path(value)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError(
                            "confirmed_lineup_player_shadow paths must stay inside the repository"
                        )
            player_hash = player_shadow["logical_model_sha256"]
            if len(player_hash) != 64 or any(
                character not in "0123456789abcdef" for character in player_hash
            ):
                raise ValueError(
                    "confirmed_lineup_player_shadow hash must be lowercase SHA-256"
                )
            config_hash = player_shadow["config_sha256"]
            if len(config_hash) != 64 or any(
                character not in "0123456789abcdef" for character in config_hash
            ):
                raise ValueError(
                    "confirmed_lineup_player_shadow config hash must be lowercase SHA-256"
                )
            _positive_int(
                player_shadow.get("timeout_seconds", 120),
                "confirmed_lineup_player_shadow timeout_seconds",
            )
        shadow = publication.get("shadow_score_grid", {})
        if not isinstance(shadow, dict):
            raise ValueError("prediction shadow_score_grid must be an object")
        if shadow.get("enabled", False):
            for key in (
                "model_version",
                "logical_model_sha256",
                "model_path",
                "prospective_gate_path",
                "output_directory",
            ):
                value = shadow.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"prediction shadow_score_grid {key} must be non-empty"
                    )
            shadow_hash = shadow["logical_model_sha256"]
            if len(shadow_hash) != 64 or any(
                character not in "0123456789abcdef" for character in shadow_hash
            ):
                raise ValueError(
                    "prediction shadow_score_grid logical_model_sha256 must be "
                    "lowercase SHA-256"
                )
            for key in (
                "model_path",
                "prospective_gate_path",
                "output_directory",
            ):
                path = Path(shadow[key])
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(
                        f"prediction shadow_score_grid {key} must stay inside "
                        "the repository"
                    )
            _positive_int(
                shadow.get("minimum_prediction_rows", 1),
                "prediction shadow_score_grid minimum_prediction_rows",
            )
            settlement = shadow.get("settlement_ledger", {})
            if not isinstance(settlement, dict):
                raise ValueError(
                    "prediction shadow settlement_ledger must be an object"
                )
            if settlement.get("enabled", False):
                for key in ("config_path", "output_directory"):
                    value = settlement.get(key)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(
                            f"prediction shadow settlement_ledger {key} must be non-empty"
                        )
                    path = Path(value)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError(
                            "prediction shadow settlement_ledger path must stay "
                            "inside the repository"
                        )
                _positive_int(
                    settlement.get("timeout_seconds", 240),
                    "prediction shadow settlement_ledger timeout_seconds",
                )
                evaluation = settlement.get("evaluation_program", {})
                if not isinstance(evaluation, dict):
                    raise ValueError(
                        "prediction shadow evaluation_program must be an object"
                    )
                if evaluation.get("enabled", False):
                    for key in (
                        "config_path",
                        "evaluation_config_sha256",
                        "output_directory",
                    ):
                        value = evaluation.get(key)
                        if not isinstance(value, str) or not value.strip():
                            raise ValueError(
                                f"prediction shadow evaluation_program {key} must be non-empty"
                            )
                        if key != "evaluation_config_sha256":
                            path = Path(value)
                            if path.is_absolute() or ".." in path.parts:
                                raise ValueError(
                                    "prediction shadow evaluation_program path must stay "
                                    "inside the repository"
                                )
                    evaluation_hash = evaluation["evaluation_config_sha256"]
                    if len(evaluation_hash) != 64 or any(
                        character not in "0123456789abcdef"
                        for character in evaluation_hash
                    ):
                        raise ValueError(
                            "prediction shadow evaluation_program hash must be lowercase SHA-256"
                        )
                    _positive_int(
                        evaluation.get("timeout_seconds", 120),
                        "prediction shadow evaluation_program timeout_seconds",
                    )
    identity = config.get("identity", {})
    if not isinstance(identity, dict):
        raise ValueError("identity config must be an object")
    _positive_int(
        identity.get("recent_team_lookback_days", 730),
        "recent_team_lookback_days",
    )
    _positive_int(
        identity.get("recent_team_max_candidates", 50),
        "recent_team_max_candidates",
    )


def discovery_date_from_checkpoint(
    job_key: str, metadata: object | None = None
) -> date | None:
    """Extract the target date from new or legacy discovery checkpoint rows."""
    if isinstance(metadata, dict):
        for key in ("target_date", "date"):
            value = metadata.get(key)
            if value:
                try:
                    return date.fromisoformat(str(value))
                except ValueError:
                    pass
    parts = job_key.split(":")
    if len(parts) >= 3 and parts[0] == "api_football" and parts[1] == "fixture_discovery":
        try:
            return date.fromisoformat(parts[2])
        except ValueError:
            return None
    return None


def _floor_local_time(value: datetime, interval_hours: int) -> datetime:
    floored_hour = value.hour - (value.hour % interval_hours)
    return datetime.combine(
        value.date(), time(hour=floored_hour), tzinfo=value.tzinfo
    )


def _nonnegative_int(value: int, name: str) -> int:
    value = int(value)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _positive_int(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _lineup_offsets(values: list[int]) -> list[int]:
    if not isinstance(values, list) or not values:
        raise ValueError("lineup_stage_offsets must be a non-empty list")
    offsets = [int(value) for value in values]
    if any(value <= 0 for value in offsets):
        raise ValueError("lineup_stage_offsets must contain positive minutes")
    if len(set(offsets)) != len(offsets):
        raise ValueError("lineup_stage_offsets must not contain duplicates")
    return sorted(offsets, reverse=True)
