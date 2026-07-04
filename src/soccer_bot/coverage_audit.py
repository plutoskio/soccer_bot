from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import gzip
import json
from pathlib import Path
import time
from typing import Iterable

from .http import HttpClient, HttpResponse
from .raw_store import RawArtifactStore


FINAL_STATUSES = {"FT", "AET", "PEN"}


def deterministic_sample(records: list[dict], limit: int) -> list[dict]:
    completed = [
        record for record in records
        if record.get("fixture", {}).get("status", {}).get("short") in FINAL_STATUSES
    ]
    completed.sort(key=lambda record: (
        record.get("fixture", {}).get("timestamp") or 0,
        record.get("fixture", {}).get("id") or 0,
    ))
    if len(completed) <= limit:
        return completed
    if limit == 1:
        return [completed[len(completed) // 2]]
    indices = [round(index * (len(completed) - 1) / (limit - 1)) for index in range(limit)]
    return [completed[index] for index in indices]


def declared_player_coverage(season: dict) -> bool:
    fixtures = (season.get("coverage") or {}).get("fixtures") or {}
    return bool(fixtures.get("statistics_players"))


@dataclass(frozen=True)
class MatchAudit:
    fixture_id: int | None
    result: bool
    lineups: bool
    team_statistics: bool
    player_blocks: bool
    participating_players: int
    minutes: bool
    core_player_fields: bool
    passing_coverage: float
    complete: bool


def audit_match(match: dict) -> MatchAudit:
    fixture_id = match.get("fixture", {}).get("id")
    score = match.get("score", {}).get("fulltime", {})
    result = score.get("home") is not None and score.get("away") is not None
    lineups = match.get("lineups") or []
    lineup_complete = (
        len(lineups) == 2
        and all(len(lineup.get("startXI") or []) == 11 for lineup in lineups)
    )
    team_statistics = len(match.get("statistics") or []) == 2
    player_teams = match.get("players") or []
    player_blocks = len(player_teams) == 2
    records = [
        record
        for team in player_teams
        for record in (team.get("players") or [])
    ]
    participating = []
    for record in records:
        statistics = (record.get("statistics") or [{}])[0]
        minutes = (statistics.get("games") or {}).get("minutes")
        if minutes is not None and int(minutes) > 0:
            participating.append(statistics)
    minutes_complete = len(participating) >= 22
    core_complete = bool(participating) and all(
        isinstance(statistics.get("games"), dict)
        and isinstance(statistics.get("goals"), dict)
        and isinstance(statistics.get("shots"), dict)
        and isinstance(statistics.get("passes"), dict)
        for statistics in participating
    )
    with_passes = sum(
        1 for statistics in participating
        if (statistics.get("passes") or {}).get("total") is not None
        and (statistics.get("passes") or {}).get("accuracy") is not None
    )
    passing_coverage = with_passes / len(participating) if participating else 0.0
    complete = bool(
        result
        and lineup_complete
        and team_statistics
        and player_blocks
        and minutes_complete
        and core_complete
        and passing_coverage >= 0.8
    )
    return MatchAudit(
        fixture_id=fixture_id,
        result=result,
        lineups=lineup_complete,
        team_statistics=team_statistics,
        player_blocks=player_blocks,
        participating_players=len(participating),
        minutes=minutes_complete,
        core_player_fields=core_complete,
        passing_coverage=passing_coverage,
        complete=complete,
    )


class RawRequestCache:
    def __init__(self, root: Path) -> None:
        self.entries: dict[tuple[str, str], dict] = {}
        for metadata_path in root.rglob("*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get("http_status") != 200:
                continue
            resource = str(metadata.get("resource"))
            fingerprint = self.fingerprint(metadata.get("request_parameters") or {})
            current = self.entries.get((resource, fingerprint))
            if current is None or metadata.get("retrieved_at", "") > current.get("retrieved_at", ""):
                self.entries[(resource, fingerprint)] = metadata

    @staticmethod
    def fingerprint(params: dict[str, object]) -> str:
        return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)

    def get(self, resource: str, params: dict[str, object]) -> object | None:
        metadata = self.entries.get((resource, self.fingerprint(params)))
        if not metadata:
            return None
        with gzip.open(metadata["data_path"], "rb") as handle:
            body = handle.read()
        if str((metadata.get("response_headers") or {}).get("content-encoding", "")).lower() == "gzip":
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8"))

    def add(self, resource: str, params: dict[str, object], metadata_path: Path) -> None:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.entries[(resource, self.fingerprint(params))] = metadata


class HistoricalCoverageAuditor:
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(
        self,
        *,
        client: HttpClient,
        store: RawArtifactStore,
        cache: RawRequestCache,
        api_key: str,
        config: dict,
    ) -> None:
        self.client = client
        self.store = store
        self.cache = cache
        self.headers = {"x-apisports-key": api_key}
        self.config = config
        self.calls = 0
        self.cache_hits = 0
        self.last_call_at: float | None = None

    def request(self, resource: str, path: str, params: dict[str, object]) -> dict:
        cached = self.cache.get(resource, params)
        if isinstance(cached, dict):
            self.cache_hits += 1
            return cached
        if self.calls >= int(self.config["maximum_calls_per_run"]):
            raise RuntimeError("Historical coverage audit call budget exhausted")
        if self.last_call_at is not None:
            wait = float(self.config["minimum_interval_seconds"]) - (time.monotonic() - self.last_call_at)
            if wait > 0:
                time.sleep(wait)
        response = self.client.get(self.BASE_URL, path, params=params, headers=self.headers, timeout=90)
        self.last_call_at = time.monotonic()
        self.calls += 1
        artifact = self.store.store(
            source="api_football", resource=resource, response=response, request_params=params
        )
        payload = self._validated_payload(response)
        self.cache.add(resource, params, artifact.metadata_path)
        return payload

    @staticmethod
    def _validated_payload(response: HttpResponse) -> dict:
        if response.status != 200:
            raise RuntimeError(f"API-Football returned HTTP {response.status}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("API-Football returned a non-object response")
        if payload.get("errors"):
            raise RuntimeError(f"API-Football returned errors: {payload['errors']}")
        return payload

    def run(self) -> dict:
        results = []
        for competition in self.config["competitions"]:
            league_id = int(competition["league_id"])
            metadata = self.request(
                "historical_coverage_league", "/leagues", {"id": league_id}
            )
            league_records = metadata.get("response") or []
            if not league_records:
                results.append({
                    "league_id": league_id,
                    "configured_label": competition["label"],
                    "grade": "FAIL",
                    "reason": "league_not_returned",
                    "seasons": [],
                })
                continue
            league_record = league_records[0]
            available = {int(item["year"]): item for item in league_record.get("seasons", [])}
            target_years = self._target_years(competition, available)
            season_results = [
                self._audit_season(competition, league_id, year, available.get(year))
                for year in target_years
            ]
            results.append({
                "league_id": league_id,
                "configured_label": competition["label"],
                "provider_name": (league_record.get("league") or {}).get("name"),
                "provider_country": (league_record.get("country") or {}).get("name"),
                "seasons": season_results,
            })
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "network_calls": self.calls,
            "cache_hits": self.cache_hits,
            "competitions": results,
        }

    def _target_years(self, competition: dict, available: dict[int, dict]) -> list[int]:
        if competition.get("seasons"):
            return [int(year) for year in competition["seasons"]]
        count = int(competition.get("completed_seasons", self.config["default_completed_seasons"]))
        today = date.today()
        completed = []
        for year, metadata in available.items():
            end_value = metadata.get("end")
            try:
                ended = date.fromisoformat(end_value) <= today if end_value else not metadata.get("current")
            except ValueError:
                ended = not metadata.get("current")
            if ended:
                completed.append(year)
        return sorted(completed, reverse=True)[:count]

    def _audit_season(
        self, competition: dict, league_id: int, year: int, metadata: dict | None
    ) -> dict:
        if metadata is None:
            return {
                "season": year,
                "declared_player_stats": False,
                "grade": "FAIL",
                "reason": "season_not_available",
                "fixture_count": 0,
                "estimated_detail_calls": 0,
                "sample": [],
            }
        declared = declared_player_coverage(metadata)
        if not declared:
            return {
                "season": year,
                "declared_player_stats": False,
                "grade": "FAIL",
                "reason": "player_statistics_not_declared",
                "fixture_count": 0,
                "estimated_detail_calls": 0,
                "sample": [],
            }
        fixture_payload = self.request(
            "historical_coverage_fixtures",
            "/fixtures",
            {"league": league_id, "season": year},
        )
        fixtures = fixture_payload.get("response") or []
        excluded_terms = [
            str(term).casefold() for term in competition.get("exclude_round_terms", [])
        ]
        if excluded_terms:
            fixtures = [
                fixture for fixture in fixtures
                if not any(
                    term in str((fixture.get("league") or {}).get("round") or "").casefold()
                    for term in excluded_terms
                )
            ]
        completed = [
            fixture for fixture in fixtures
            if fixture.get("fixture", {}).get("status", {}).get("short") in FINAL_STATUSES
        ]
        sample = deterministic_sample(fixtures, int(self.config["sample_matches_per_season"]))
        if not sample:
            return {
                "season": year,
                "declared_player_stats": True,
                "grade": "FAIL",
                "reason": "no_completed_fixtures",
                "fixture_count": len(completed),
                "estimated_detail_calls": (len(completed) + 19) // 20,
                "sample": [],
            }
        ids = "-".join(str(item["fixture"]["id"]) for item in sample)
        detail_payload = self.request(
            "historical_coverage_sample", "/fixtures", {"ids": ids}
        )
        details = detail_payload.get("response") or []
        audits = [audit_match(match) for match in details]
        completeness = sum(audit.complete for audit in audits) / len(sample)
        if len(details) == len(sample) and completeness >= 0.9:
            grade = "PASS"
            reason = "sample_complete"
        elif completeness >= 0.5:
            grade = "PARTIAL"
            reason = "sample_partially_complete"
        else:
            grade = "FAIL"
            reason = "sample_incomplete"
        return {
            "season": year,
            "declared_player_stats": True,
            "grade": grade,
            "reason": reason,
            "fixture_count": len(completed),
            "estimated_detail_calls": (len(completed) + 19) // 20,
            "sample_size": len(sample),
            "returned_sample_size": len(details),
            "complete_sample_matches": sum(audit.complete for audit in audits),
            "sample": [asdict(audit) for audit in audits],
        }


def write_markdown_report(result: dict, output: Path) -> None:
    seasons = [
        (competition, season)
        for competition in result["competitions"]
        for season in competition.get("seasons", [])
    ]
    grades = {
        grade: sum(1 for _, season in seasons if season.get("grade") == grade)
        for grade in ("PASS", "PARTIAL", "FAIL")
    }
    approved_fixtures = sum(
        season.get("fixture_count", 0) for _, season in seasons if season.get("grade") == "PASS"
    )
    estimated_calls = sum(
        season.get("estimated_detail_calls", 0)
        for _, season in seasons if season.get("grade") == "PASS"
    )
    lines = [
        "# API-Football Historical Coverage Audit",
        "",
        f"Generated: {result['generated_at']}",
        "",
        f"Network calls this run: **{result['network_calls']}**  ",
        f"Cached responses reused: **{result['cache_hits']}**  ",
        f"Season grades: **{grades['PASS']} PASS**, **{grades['PARTIAL']} PARTIAL**, **{grades['FAIL']} FAIL**  ",
        f"Completed fixtures in approved seasons: **{approved_fixtures:,}**  ",
        f"Maximum estimated 20-fixture detail calls: **{estimated_calls:,}**",
        "",
        "A PASS requires at least 90% of the deterministic sample to contain a final score, two complete starting lineups, two team-stat blocks, two player blocks, at least 22 participating players with minutes, the core player structures, and passing data for at least 80% of participants.",
        "",
        "| Competition | Provider competition | Season | Declared player stats | Fixtures | Sample complete | Grade | Reason |",
        "|---|---|---:|:---:|---:|---:|:---:|---|",
    ]
    for competition, season in seasons:
        complete = f"{season.get('complete_sample_matches', 0)}/{season.get('sample_size', 0)}"
        lines.append(
            f"| {competition.get('configured_label')} | "
            f"{competition.get('provider_name', '—')} ({competition.get('provider_country', '—')}) | "
            f"{season.get('season')} | {'yes' if season.get('declared_player_stats') else 'no'} | "
            f"{season.get('fixture_count', 0):,} | {complete} | **{season.get('grade')}** | "
            f"{season.get('reason')} |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "Only PASS seasons should enter the automatic historical backfill. PARTIAL and FAIL seasons remain excluded until their missing structures are understood. The estimated call count is an upper bound before subtracting fixtures already complete in DuckDB.",
        "",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
