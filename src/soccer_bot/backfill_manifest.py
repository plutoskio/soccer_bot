from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .coverage_audit import FINAL_STATUSES, RawRequestCache
from .database import Warehouse, normalized_name
from .loaders import parse_datetime


DETAIL_ACTIONS = {"REQUEST_API", "NEW_FIXTURE"}


class BackfillManifestBuilder:
    def __init__(
        self,
        *,
        warehouse: Warehouse,
        cache: RawRequestCache,
        coverage_result: dict,
        audit_config: dict,
    ) -> None:
        self.warehouse = warehouse
        self.connection = warehouse.connection
        self.cache = cache
        self.coverage_result = coverage_result
        self.audit_config = audit_config
        self.competition_config = {
            int(item["league_id"]): item for item in audit_config["competitions"]
        }
        self.fixture_source_map = self._source_map("fixture")
        self.team_source_map = self._source_map("team")
        self.team_name_index = self._team_name_index()
        self.fixtures_by_teams = self._fixture_index()
        self.completeness = self._completeness_index()
        self.raw_complete_fixture_ids = self._raw_complete_fixture_ids()

    def build(self) -> tuple[list[dict], list[dict], dict]:
        rows: list[dict] = []
        seen: set[int] = set()
        for competition in self.coverage_result["competitions"]:
            league_id = int(competition["league_id"])
            config = self.competition_config[league_id]
            approved = {
                int(season["season"]): season
                for season in competition.get("seasons", [])
                if season.get("grade") == "PASS"
            }
            for season, season_result in approved.items():
                payload = self.cache.get(
                    "historical_coverage_fixtures", {"league": league_id, "season": season}
                )
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Missing cached fixture list for league={league_id}, season={season}")
                for fixture in self._eligible_fixtures(payload.get("response") or [], config):
                    api_fixture_id = int(fixture["fixture"]["id"])
                    if api_fixture_id in seen:
                        continue
                    seen.add(api_fixture_id)
                    rows.append(self._manifest_row(
                        fixture, league_id, season, competition, season_result
                    ))
        rows.sort(key=lambda row: (
            row["priority"], -row["season"], row["league_id"], row["kickoff"], row["api_fixture_id"]
        ))
        batches = self._batches(rows)
        summary = self._summary(rows, batches)
        return rows, batches, summary

    @staticmethod
    def _eligible_fixtures(fixtures: list[dict], config: dict) -> list[dict]:
        excluded = [str(term).casefold() for term in config.get("exclude_round_terms", [])]
        result = []
        for fixture in fixtures:
            if fixture.get("fixture", {}).get("status", {}).get("short") not in FINAL_STATUSES:
                continue
            round_name = str((fixture.get("league") or {}).get("round") or "").casefold()
            if any(term in round_name for term in excluded):
                continue
            result.append(fixture)
        return result

    def _manifest_row(
        self,
        fixture: dict,
        league_id: int,
        season: int,
        competition: dict,
        season_result: dict,
    ) -> dict:
        api_fixture_id = int(fixture["fixture"]["id"])
        kickoff = parse_datetime(fixture["fixture"].get("date"))
        teams = fixture.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        canonical_id = self.fixture_source_map.get(str(api_fixture_id))
        match_method = "api_source_id" if canonical_id else None
        match_confidence = 1.0 if canonical_id else None
        ambiguity: list[str] = []

        if not canonical_id:
            team_type = "national" if league_id in {1, 4} else "club"
            home_id, home_reason = self._resolve_team(home, team_type)
            away_id, away_reason = self._resolve_team(away, team_type)
            if home_reason:
                ambiguity.append(f"home:{home_reason}")
            if away_reason:
                ambiguity.append(f"away:{away_reason}")
            if home_id and away_id and kickoff:
                candidates = [
                    record for record in self.fixtures_by_teams.get((home_id, away_id), [])
                    if abs((record[1] - kickoff).total_seconds()) <= 36 * 3600
                ]
                if len(candidates) == 1:
                    canonical_id = candidates[0][0]
                    match_method = "canonical_teams_and_kickoff"
                    match_confidence = 0.95
                elif len(candidates) > 1:
                    candidates.sort(key=lambda record: abs((record[1] - kickoff).total_seconds()))
                    closest_distance = abs((candidates[0][1] - kickoff).total_seconds())
                    second_distance = abs((candidates[1][1] - kickoff).total_seconds())
                    if closest_distance + 3600 < second_distance:
                        canonical_id = candidates[0][0]
                        match_method = "canonical_teams_nearest_kickoff"
                        match_confidence = 0.9
                    else:
                        ambiguity.append("multiple_fixture_candidates")

        missing = self._missing_components(canonical_id)
        if ambiguity and not canonical_id:
            action = "NEEDS_REVIEW"
        elif canonical_id and not missing:
            action = "COMPLETE"
        elif api_fixture_id in self.raw_complete_fixture_ids:
            action = "REPROCESS_LOCAL"
        elif canonical_id:
            action = "REQUEST_API"
        else:
            action = "NEW_FIXTURE"

        return {
            "api_fixture_id": api_fixture_id,
            "league_id": league_id,
            "competition": competition.get("configured_label"),
            "provider_competition": competition.get("provider_name"),
            "season": season,
            "round": (fixture.get("league") or {}).get("round"),
            "kickoff": kickoff.isoformat() if kickoff else None,
            "home_api_team_id": home.get("id"),
            "home_team": home.get("name"),
            "away_api_team_id": away.get("id"),
            "away_team": away.get("name"),
            "canonical_fixture_id": canonical_id,
            "match_method": match_method,
            "match_confidence": match_confidence,
            "ambiguity": ambiguity,
            "missing_components": missing,
            "raw_complete_response": api_fixture_id in self.raw_complete_fixture_ids,
            "action": action,
            "priority": self._priority(league_id),
            "approved_season_fixture_count": season_result.get("fixture_count"),
        }

    def _resolve_team(self, team: dict, team_type: str) -> tuple[str | None, str | None]:
        source_id = str(team.get("id"))
        if source_id in self.team_source_map:
            return self.team_source_map[source_id], None
        norm = normalized_name(str(team.get("name") or ""))
        norm = self.warehouse.team_aliases.get(norm, norm)
        candidates = self.team_name_index.get((norm, team_type), [])
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return None, "ambiguous_team_name"
        # A team absent from the warehouse is expected when this provider is
        # extending coverage. It is a new entity, not an identity ambiguity.
        return None, None

    def _missing_components(self, fixture_id: str | None) -> list[str]:
        if not fixture_id:
            return ["canonical_fixture", "result", "lineups", "team_statistics", "player_statistics"]
        state = self.completeness.get(fixture_id, {})
        missing = []
        if not state.get("result"):
            missing.append("result")
        if state.get("lineup_teams", 0) < 2:
            missing.append("lineups")
        if state.get("team_stat_teams", 0) < 2:
            missing.append("team_statistics")
        if state.get("participating_players", 0) < 22:
            missing.append("player_statistics")
        elif state.get("passing_coverage", 0.0) < 0.8:
            missing.append("player_passing")
        return missing

    def _source_map(self, entity_type: str) -> dict[str, str]:
        return dict(self.connection.execute(
            """
            SELECT source_entity_id, internal_entity_id
            FROM source_entity_map
            WHERE source_code = 'api_football' AND entity_type = ?
            """,
            [entity_type],
        ).fetchall())

    def _team_name_index(self) -> dict[tuple[str, str], list[str]]:
        result: dict[tuple[str, str], list[str]] = defaultdict(list)
        for team_id, norm, team_type in self.connection.execute(
            "SELECT team_id, normalized_name, team_type FROM team"
        ).fetchall():
            result[(norm, team_type)].append(team_id)
        return result

    def _fixture_index(self) -> dict[tuple[str, str], list[tuple[str, datetime]]]:
        result: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
        for fixture_id, home_id, away_id, kickoff in self.connection.execute(
            """
            SELECT fixture_id, home_team_id, away_team_id, scheduled_kickoff
            FROM fixture WHERE scheduled_kickoff IS NOT NULL
            """
        ).fetchall():
            result[(home_id, away_id)].append((fixture_id, kickoff.astimezone(timezone.utc)))
        return result

    def _completeness_index(self) -> dict[str, dict]:
        states: dict[str, dict] = defaultdict(dict)
        for fixture_id, value in self.connection.execute(
            "SELECT fixture_id, true FROM fixture_result_observation GROUP BY fixture_id"
        ).fetchall():
            states[fixture_id]["result"] = bool(value)
        for fixture_id, count in self.connection.execute(
            """
            SELECT fixture_id, count(DISTINCT team_id)
            FROM lineup_snapshot
            WHERE source_code = 'api_football' AND lineup_type = 'confirmed' AND is_complete
            GROUP BY fixture_id
            """
        ).fetchall():
            states[fixture_id]["lineup_teams"] = count
        for fixture_id, count in self.connection.execute(
            """
            SELECT fixture_id, count(DISTINCT team_id)
            FROM team_match_stat_observation
            WHERE source_code = 'api_football'
            GROUP BY fixture_id
            """
        ).fetchall():
            states[fixture_id]["team_stat_teams"] = count
        for fixture_id, participants, with_passing in self.connection.execute(
            """
            SELECT fixture_id,
                   count(*) FILTER (WHERE minutes_played > 0),
                   count(*) FILTER (
                       WHERE minutes_played > 0 AND passes IS NOT NULL
                         AND accurate_passes IS NOT NULL
                   )
            FROM player_match_stat_observation
            WHERE source_code = 'api_football'
            GROUP BY fixture_id
            """
        ).fetchall():
            states[fixture_id]["participating_players"] = participants
            states[fixture_id]["passing_coverage"] = (
                with_passing / participants if participants else 0.0
            )
        return states

    def _raw_complete_fixture_ids(self) -> set[int]:
        result = set()
        for competition in self.coverage_result["competitions"]:
            for season in competition.get("seasons", []):
                for match in season.get("sample", []):
                    if match.get("complete") and match.get("fixture_id") is not None:
                        result.add(int(match["fixture_id"]))
        return result

    @staticmethod
    def _priority(league_id: int) -> int:
        if league_id in {1, 2, 4}:
            return 1
        if league_id in {39, 61, 78, 135, 140}:
            return 2
        return 3

    @staticmethod
    def _batches(rows: list[dict]) -> list[dict]:
        grouped: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
        for row in rows:
            if row["action"] in DETAIL_ACTIONS:
                grouped[(row["priority"], row["league_id"], row["season"])].append(row)
        batches = []
        for (priority, league_id, season), fixtures in sorted(
            grouped.items(), key=lambda item: (item[0][0], -item[0][2], item[0][1])
        ):
            fixtures.sort(key=lambda row: (row["kickoff"], row["api_fixture_id"]))
            for index in range(0, len(fixtures), 20):
                part = fixtures[index:index + 20]
                batch_number = index // 20 + 1
                batches.append({
                    "batch_id": f"api-football-{league_id}-{season}-{batch_number:04d}",
                    "priority": priority,
                    "league_id": league_id,
                    "season": season,
                    "fixture_ids": [row["api_fixture_id"] for row in part],
                    "fixture_count": len(part),
                    "status": "pending",
                    "attempts": 0,
                })
        return batches

    @staticmethod
    def _summary(rows: list[dict], batches: list[dict]) -> dict:
        actions = Counter(row["action"] for row in rows)
        by_season = defaultdict(Counter)
        labels = {}
        for row in rows:
            key = (row["league_id"], row["season"])
            by_season[key][row["action"]] += 1
            labels[key] = row["competition"]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fixtures": len(rows),
            "actions": dict(sorted(actions.items())),
            "batches": len(batches),
            "api_fixture_requests": sum(batch["fixture_count"] for batch in batches),
            "by_season": [
                {
                    "league_id": key[0],
                    "season": key[1],
                    "competition": labels[key],
                    "actions": dict(sorted(counts.items())),
                }
                for key, counts in sorted(by_season.items(), key=lambda item: (item[0][0], -item[0][1]))
            ],
        }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_manifest_report(summary: dict, path: Path) -> None:
    actions = summary["actions"]
    lines = [
        "# API-Football Backfill Manifest",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        f"Approved completed fixtures considered: **{summary['fixtures']:,}**  ",
        f"Fixtures already complete: **{actions.get('COMPLETE', 0):,}**  ",
        f"Fixtures recoverable from local raw data: **{actions.get('REPROCESS_LOCAL', 0):,}**  ",
        f"Existing fixtures requiring API detail: **{actions.get('REQUEST_API', 0):,}**  ",
        f"New fixtures requiring API detail: **{actions.get('NEW_FIXTURE', 0):,}**  ",
        f"Fixtures requiring identity review: **{actions.get('NEEDS_REVIEW', 0):,}**  ",
        f"Planned requests with 20-fixture batching: **{summary['batches']:,}**",
        "",
        "| Competition | Season | Complete | Local replay | Request API | New fixture | Review |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["by_season"]:
        counts = item["actions"]
        lines.append(
            f"| {item['competition']} | {item['season']} | "
            f"{counts.get('COMPLETE', 0):,} | {counts.get('REPROCESS_LOCAL', 0):,} | "
            f"{counts.get('REQUEST_API', 0):,} | {counts.get('NEW_FIXTURE', 0):,} | "
            f"{counts.get('NEEDS_REVIEW', 0):,} |"
        )
    lines.extend([
        "",
        "Machine-readable files are stored under `data/staged/`. `NEEDS_REVIEW` rows are never placed into API execution batches. The request count excludes fixtures already complete or recoverable from retained raw responses.",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
