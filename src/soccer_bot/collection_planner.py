from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
