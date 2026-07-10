from __future__ import annotations

import csv
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .database import (
    Warehouse,
    json_text,
    metadata_artifact_id,
    normalized_name,
    optional_float,
    optional_int,
    stable_id,
)
from .player_names import (
    api_player_comparison_name,
    compatible_api_player_compound_names,
    compatible_api_player_names,
)
from .player_linking import (
    LineupAlias,
    StatCandidate,
    deduplicate_api_lineup_entries,
    link_team_players,
)


FOOTBALL_DATA_COMPETITIONS = {
    "E0": ("English Premier League", "GB-ENG", "Europe/London"),
    "SP1": ("Spanish La Liga", "ES", "Europe/Madrid"),
    "D1": ("German Bundesliga", "DE", "Europe/Berlin"),
    "I1": ("Italian Serie A", "IT", "Europe/Rome"),
    "F1": ("French Ligue 1", "FR", "Europe/Paris"),
}

UNDERSTAT_COMPETITIONS = {
    "EPL": ("English Premier League", "GB-ENG"),
    "La liga": ("Spanish La Liga", "ES"),
    "Bundesliga": ("German Bundesliga", "DE"),
    "Serie A": ("Italian Serie A", "IT"),
    "Ligue 1": ("French Ligue 1", "FR"),
    "RFPL": ("Russian Premier League", "RU"),
}


API_FOOTBALL_STATUS_MAP = {
    "NS": "scheduled",
    "TBD": "scheduled",
    "1H": "live",
    "2H": "live",
    "ET": "live",
    "P": "live",
    "LIVE": "live",
    "HT": "live",
    "INT": "delayed",
    "SUSP": "suspended",
    "FT": "final",
    "AET": "final",
    "PEN": "final",
    "PST": "postponed",
    "CANC": "cancelled",
    "ABD": "abandoned",
    "AWD": "administrative_result",
    "WO": "administrative_result",
}


def canonical_api_football_status(
    status_short: object, *, administrative_unplayed: bool = False
) -> str:
    """Map API-Football status codes to the project's canonical statuses."""
    if administrative_unplayed:
        return "administrative_result"
    code = str(status_short or "").strip().upper()
    return API_FOOTBALL_STATUS_MAP.get(code, "unknown")


def parse_datetime(value: str | None, default_timezone=timezone.utc) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_timezone)
    return result


def parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_api_passes(passes: dict) -> tuple[int | None, int | None, float | None]:
    """Normalize API-Football's two historical pass-accuracy representations.

    Current payloads use a completed-pass count (for example 16 of 20), while
    older payloads use a percentage string (for example ``"80%"``).
    """
    total = optional_int(passes.get("total"))
    raw_accuracy = passes.get("accuracy")
    if raw_accuracy is None:
        return total, None, None

    is_percentage = isinstance(raw_accuracy, str) and raw_accuracy.strip().endswith("%")
    cleaned = raw_accuracy.strip().removesuffix("%") if isinstance(raw_accuracy, str) else raw_accuracy
    accuracy_value = optional_float(cleaned)
    if accuracy_value is None:
        return total, None, None

    # A numeric value larger than total cannot be a completed-pass count.
    if is_percentage or (total is not None and accuracy_value > total and accuracy_value <= 100):
        percentage = accuracy_value
        accurate = round(total * percentage / 100) if total is not None else None
        return total, accurate, percentage

    accurate = int(accuracy_value)
    percentage = 100.0 * accurate / total if total else None
    return total, accurate, percentage


def api_player_identity_key(source_player_id: object, name: str) -> str:
    """Disambiguate provider IDs that API-Football reuses for different people."""
    return f"{source_player_id}|{normalized_name(name)}"


class RawCatalog:
    def __init__(self, root: Path, warehouse: Warehouse) -> None:
        self.root = root
        self.warehouse = warehouse
        self.items: list[dict] = []
        for metadata_path in sorted(root.rglob("*.meta.json")):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["_metadata_path"] = metadata_path
            metadata["_raw_artifact_id"] = metadata_artifact_id(metadata_path)
            self.items.append(metadata)

    def load_database_catalog(self) -> None:
        rows = [self.database_row(item) for item in self.items]
        if rows:
            self.warehouse.connection.executemany(
                """
                INSERT INTO raw_artifact (
                    raw_artifact_id, source_code, resource_name, retrieved_at,
                    request_url, request_parameters, http_status, response_headers,
                    content_sha256, uncompressed_bytes, data_path, metadata_path,
                    duplicate_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (raw_artifact_id) DO UPDATE SET
                    http_status = excluded.http_status,
                    response_headers = excluded.response_headers,
                    duplicate_content = excluded.duplicate_content
                """,
                rows,
            )

    @staticmethod
    def database_row(item: dict) -> list:
        return [
            item["_raw_artifact_id"],
            item["source"],
            item["resource"],
            parse_datetime(item["retrieved_at"]),
            item.get("request_url"),
            json_text(item.get("request_parameters", {})),
            item.get("http_status"),
            json_text(item.get("response_headers", {})),
            item["content_sha256"],
            item.get("uncompressed_bytes"),
            item["data_path"],
            str(item["_metadata_path"]),
            bool(item.get("duplicate_content")),
        ]

    @classmethod
    def register_item(cls, warehouse: Warehouse, item: dict) -> None:
        warehouse.connection.execute(
            """
            INSERT INTO raw_artifact (
                raw_artifact_id, source_code, resource_name, retrieved_at,
                request_url, request_parameters, http_status, response_headers,
                content_sha256, uncompressed_bytes, data_path, metadata_path,
                duplicate_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (raw_artifact_id) DO UPDATE SET
                http_status = excluded.http_status,
                response_headers = excluded.response_headers,
                duplicate_content = excluded.duplicate_content
            """,
            cls.database_row(item),
        )

    def iter(
        self,
        source: str,
        resource: str,
        *,
        unique_content: bool = True,
    ):
        seen: set[str] = set()
        for item in self.items:
            if item["source"] != source or item["resource"] != resource:
                continue
            if item.get("http_status") != 200:
                continue
            if unique_content and item["content_sha256"] in seen:
                continue
            seen.add(item["content_sha256"])
            yield item

    @staticmethod
    def read_bytes(item: dict) -> bytes:
        with gzip.open(item["data_path"], "rb") as handle:
            body = handle.read()
        if item.get("response_headers", {}).get("content-encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body

    @classmethod
    def read_json(cls, item: dict):
        return json.loads(cls.read_bytes(item).decode("utf-8"))

    @classmethod
    def read_csv(cls, item: dict) -> list[dict]:
        text = cls.read_bytes(item).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))


class WarehouseLoader:
    def __init__(self, warehouse: Warehouse, catalog: RawCatalog) -> None:
        self.warehouse = warehouse
        self.connection = warehouse.connection
        self.catalog = catalog
        self._api_lineup_alias_map: dict[tuple[str, str, str, str], str] = {}
        # These caches are opt-in because normal incremental loads are small.  The
        # historical identity repair primes them once to avoid tens of thousands
        # of repeated scans over the same warehouse tables.
        self._api_stat_candidate_cache: dict[
            tuple[str, str, str], list[StatCandidate]
        ] | None = None
        self._api_fixture_id_cache: dict[str, str] = {}
        self._api_team_id_cache: dict[str, str] = {}
        self._api_player_id_cache: dict[str, tuple[str, str]] = {}
        self._api_fixture_team_type_cache: dict[str, str] = {}
        self._use_runtime_api_player_id_cache = False
        self._api_runtime_source_ids_by_internal: dict[str, set[str]] = {}

    def enable_api_backfill_identity_cache(self) -> None:
        """Use IDs observed in this process instead of reverse-scanning mappings.

        Historical detail payloads load player statistics before lineups, so
        every lineup candidate's provider identity is already available here.
        The flag is intentionally opt-in to preserve other loader workflows.
        """
        self._use_runtime_api_player_id_cache = True

    def load_all(self) -> None:
        self.catalog.load_database_catalog()
        self.load_football_data_uk()
        self.load_understat()
        self.load_statsbomb()
        self.load_api_football()
        self.load_polymarket()

    def prime_api_link_repair_caches(self) -> None:
        """Load stable API-Football identity evidence once for a bulk replay."""
        mapping_rows = self.connection.execute(
            """
            SELECT entity_type, source_entity_id, internal_entity_id
            FROM source_entity_map
            WHERE source_code='api_football'
              AND entity_type IN ('fixture', 'team')
            """
        ).fetchall()
        self._api_fixture_id_cache = {
            source_id: internal_id
            for entity_type, source_id, internal_id in mapping_rows
            if entity_type == "fixture"
        }
        self._api_team_id_cache = {
            source_id: internal_id
            for entity_type, source_id, internal_id in mapping_rows
            if entity_type == "team"
        }
        self._api_fixture_team_type_cache = {
            fixture_id: (
                "national" if competition_type == "international_tournament" else "club"
            )
            for fixture_id, competition_type in self.connection.execute(
                """
                SELECT f.fixture_id, c.competition_type
                FROM fixture f LEFT JOIN competition c USING (competition_id)
                """
            ).fetchall()
        }

        rows = self.connection.execute(
            """
            SELECT s.fixture_id, s.team_id, s.raw_artifact_id, s.player_id,
                   p.full_name, s.minutes_played, s.started, s.shirt_number,
                   s.position_code,
                   list(m.source_entity_id ORDER BY m.source_entity_id)
            FROM player_match_stat_observation s
            JOIN player p USING (player_id)
            LEFT JOIN source_entity_map m
              ON m.internal_entity_id=p.player_id
             AND m.source_code='api_football' AND m.entity_type='player'
            WHERE s.source_code='api_football'
            GROUP BY s.fixture_id, s.team_id, s.raw_artifact_id, s.player_id,
                     p.full_name, s.minutes_played, s.started, s.shirt_number,
                     s.position_code
            """
        ).fetchall()
        cache: dict[tuple[str, str, str], list[StatCandidate]] = {}
        for row in rows:
            cache.setdefault((row[0], row[1], row[2]), []).append(
                StatCandidate(
                    player_id=row[3], name=row[4], minutes_played=row[5],
                    started=row[6], shirt_number=row[7], position=row[8],
                    source_player_ids=tuple(
                        value for value in row[9] if value is not None
                    ),
                )
            )
        self._api_stat_candidate_cache = cache

        self._api_player_id_cache = {
            source_id: (internal_id, full_name)
            for source_id, internal_id, full_name in self.connection.execute(
                """
                SELECT m.source_entity_id, m.internal_entity_id, p.full_name
                FROM source_entity_map m JOIN player p
                  ON p.player_id=m.internal_entity_id
                WHERE m.source_code='api_football' AND m.entity_type='player'
                """
            ).fetchall()
        }

    def api_fixture_id(self, source_fixture_id: object) -> str | None:
        source_id = str(source_fixture_id)
        if self._api_fixture_id_cache:
            return self._api_fixture_id_cache.get(source_id)
        return self.warehouse.mapped_id("api_football", "fixture", source_id)

    def _resolve_api_team(self, source_id: object, name: str, team_type: str) -> str:
        cached = self._api_team_id_cache.get(str(source_id))
        if cached:
            return cached
        return self.warehouse.resolve_team(
            "api_football", source_id, name, team_type=team_type
        )

    def load_football_data_uk(self) -> None:
        source = "football_data_uk"
        for item in self.catalog.iter(source, "league_csv"):
            rows = self.catalog.read_csv(item)
            if not rows:
                continue
            self.connection.execute("BEGIN TRANSACTION")
            division = str(rows[0].get("Div") or "unknown")
            competition_name, country_code, timezone_name = FOOTBALL_DATA_COMPETITIONS.get(
                division, (division, None, "UTC")
            )
            competition_id = self.warehouse.resolve_competition(
                source,
                division,
                competition_name,
                country_code=country_code,
                competition_type="domestic_league",
            )
            path = str(item.get("request_parameters", {}).get("path", ""))
            match = re.search(r"/mmz4281/(\d{4})/", path)
            code = match.group(1) if match else "unknown"
            season_name = f"20{code[:2]}/{code[2:]}" if len(code) == 4 else code
            season_id = self.warehouse.resolve_season(
                source, f"{division}|{code}", competition_id, season_name
            )
            artifact_id = item["_raw_artifact_id"]
            retrieved_at = parse_datetime(item["retrieved_at"])
            for row_number, row in enumerate(rows, start=1):
                home_name = row.get("HomeTeam", "").strip()
                away_name = row.get("AwayTeam", "").strip()
                if not home_name or not away_name:
                    continue
                home_id = self.warehouse.resolve_team(
                    source, f"{division}|{home_name}", home_name, team_type="club", country_code=country_code
                )
                away_id = self.warehouse.resolve_team(
                    source, f"{division}|{away_name}", away_name, team_type="club", country_code=country_code
                )
                kickoff = self._football_data_kickoff(row, ZoneInfo(timezone_name))
                source_fixture_id = f"{division}|{code}|{row.get('Date')}|{home_name}|{away_name}"
                status = "completed" if row.get("FTHG") not in {None, ""} else "scheduled"
                fixture_id = self.warehouse.resolve_fixture(
                    source,
                    source_fixture_id,
                    home_team_id=home_id,
                    away_team_id=away_id,
                    scheduled_kickoff=kickoff,
                    competition_id=competition_id,
                    season_id=season_id,
                    status=status,
                    round_name=None,
                )
                if status == "completed":
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO fixture_result_observation VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            stable_id("result", source, source_fixture_id),
                            fixture_id,
                            source,
                            artifact_id,
                            kickoff,
                            retrieved_at,
                            optional_int(row.get("FTHG")),
                            optional_int(row.get("FTAG")),
                            optional_int(row.get("HTHG")),
                            optional_int(row.get("HTAG")),
                            None,
                            None,
                            None,
                            None,
                            "final",
                        ],
                    )
                self._insert_football_data_team_stats(
                    fixture_id, home_id, away_id, row, source, artifact_id, retrieved_at
                )
                self._insert_football_data_quotes(
                    fixture_id, row, source, artifact_id, retrieved_at, row_number
                )
                if row_number % 100 == 0:
                    self.connection.execute("COMMIT")
                    self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute("COMMIT")

    @staticmethod
    def _football_data_kickoff(row: dict, zone: ZoneInfo) -> datetime | None:
        value = f"{row.get('Date', '')} {row.get('Time') or '12:00'}".strip()
        for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"):
            try:
                return datetime.strptime(value, pattern).replace(tzinfo=zone)
            except ValueError:
                continue
        return None

    def _insert_football_data_team_stats(
        self, fixture_id, home_id, away_id, row, source, artifact_id, retrieved_at
    ) -> None:
        mappings = [
            (home_id, "HS", "HST", "HC", "HF", "HY", "HR"),
            (away_id, "AS", "AST", "AC", "AF", "AY", "AR"),
        ]
        for team_id, shots, on_target, corners, fouls, yellow, red in mappings:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO team_match_stat_observation (
                    observation_id, fixture_id, team_id, source_code, raw_artifact_id,
                    period, shots, shots_on_target, corners, fouls, yellow_cards,
                    red_cards, statistics, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, 'regulation', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    stable_id("team_stat", source, fixture_id, team_id),
                    fixture_id,
                    team_id,
                    source,
                    artifact_id,
                    optional_int(row.get(shots)),
                    optional_int(row.get(on_target)),
                    optional_int(row.get(corners)),
                    optional_int(row.get(fouls)),
                    optional_int(row.get(yellow)),
                    optional_int(row.get(red)),
                    json_text({key: row.get(key) for key in (shots, on_target, corners, fouls, yellow, red)}),
                    retrieved_at,
                ],
            )

    def _insert_football_data_quotes(
        self, fixture_id, row, source, artifact_id, retrieved_at, row_number
    ) -> None:
        sets = [
            ("Bet365", "sampled", {"home": "B365H", "draw": "B365D", "away": "B365A"}),
            ("market_average", "sampled", {"home": "AvgH", "draw": "AvgD", "away": "AvgA"}),
            ("Bet365", "closing", {"home": "B365CH", "draw": "B365CD", "away": "B365CA"}),
            ("market_average", "closing", {"home": "AvgCH", "draw": "AvgCD", "away": "AvgCA"}),
        ]
        for bookmaker, quote_type, selections in sets:
            for selection, column in selections.items():
                price = optional_float(row.get(column))
                if price is None:
                    continue
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO bookmaker_quote VALUES
                    (?, ?, ?, ?, ?, 'moneyline', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        stable_id("quote", source, fixture_id, bookmaker, quote_type, selection),
                        fixture_id,
                        source,
                        artifact_id,
                        bookmaker,
                        selection,
                        None,
                        price,
                        quote_type,
                        None,
                        retrieved_at,
                    ],
                )
        handicap = optional_float(row.get("AHCh") or row.get("AHh"))
        for selection, column in (("home", "AvgCAHH"), ("away", "AvgCAHA")):
            price = optional_float(row.get(column))
            if price is None:
                continue
            self.connection.execute(
                """
                INSERT OR REPLACE INTO bookmaker_quote VALUES
                (?, ?, ?, ?, 'market_average', 'asian_handicap', ?, ?, ?, 'closing', ?, ?)
                """,
                [
                    stable_id("quote", source, fixture_id, "asian", selection),
                    fixture_id,
                    source,
                    artifact_id,
                    selection,
                    handicap,
                    price,
                    None,
                    retrieved_at,
                ],
            )

    def load_understat(self) -> None:
        source = "understat"
        for item in self.catalog.iter(source, "league_data"):
            self.connection.execute("BEGIN TRANSACTION")
            payload = self.catalog.read_json(item)
            params = item.get("request_parameters", {})
            league = str(params.get("league", "EPL"))
            season_code = str(params.get("season", "2025"))
            competition_name, country_code = UNDERSTAT_COMPETITIONS.get(
                league, (league, None)
            )
            competition_id = self.warehouse.resolve_competition(
                source, league, competition_name, country_code=country_code,
                competition_type="domestic_league"
            )
            season_name = f"{season_code}/{str(int(season_code) + 1)[-2:]}" if season_code.isdigit() else season_code
            season_id = self.warehouse.resolve_season(
                source, f"{league}|{season_code}", competition_id, season_name
            )
            retrieved_at = parse_datetime(item["retrieved_at"])
            artifact_id = item["_raw_artifact_id"]
            for match_number, match in enumerate(payload.get("dates", []), start=1):
                home = match.get("h", {})
                away = match.get("a", {})
                if not home.get("id") or not away.get("id"):
                    continue
                home_id = self.warehouse.resolve_team(source, home["id"], home["title"], team_type="club")
                away_id = self.warehouse.resolve_team(source, away["id"], away["title"], team_type="club")
                kickoff = parse_datetime(match.get("datetime"), ZoneInfo("Europe/London"))
                fixture_id = self.warehouse.resolve_fixture(
                    source,
                    match["id"],
                    home_team_id=home_id,
                    away_team_id=away_id,
                    scheduled_kickoff=kickoff,
                    competition_id=competition_id,
                    season_id=season_id,
                    status="completed" if match.get("isResult") else "scheduled",
                )
                if match.get("isResult"):
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO fixture_result_observation (
                            observation_id, fixture_id, source_code, raw_artifact_id,
                            observed_at, retrieved_at, home_score_regulation,
                            away_score_regulation, result_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'final')
                        """,
                        [
                            stable_id("result", source, match["id"]), fixture_id, source,
                            artifact_id, kickoff, retrieved_at,
                            optional_int(match.get("goals", {}).get("h")),
                            optional_int(match.get("goals", {}).get("a")),
                        ],
                    )
                for team_id, side in ((home_id, "h"), (away_id, "a")):
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO team_match_stat_observation (
                            observation_id, fixture_id, team_id, source_code, raw_artifact_id,
                            period, xg, statistics, retrieved_at
                        ) VALUES (?, ?, ?, ?, ?, 'regulation', ?, ?, ?)
                        """,
                        [
                            stable_id("team_stat", source, match["id"], side), fixture_id,
                            team_id, source, artifact_id,
                            optional_float(match.get("xG", {}).get(side)), json_text(match), retrieved_at,
                        ],
                    )
                if match_number % 100 == 0:
                    self.connection.execute("COMMIT")
                    self.connection.execute("BEGIN TRANSACTION")
            for player_number, record in enumerate(payload.get("players", []), start=1):
                player_id = self.warehouse.resolve_player(
                    source, record["id"], record["player_name"], primary_position=record.get("position")
                )
                team_names = [name.strip() for name in str(record.get("team_title", "")).split(",") if name.strip()]
                team_id = None
                if len(team_names) == 1:
                    team_id = self.warehouse.resolve_team(
                        source, f"name:{team_names[0]}", team_names[0], team_type="club"
                    )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO player_season_stat VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        stable_id("player_season", source, league, season_code, record["id"]),
                        player_id, team_id, competition_id, season_id, source, artifact_id,
                        optional_int(record.get("games")), optional_int(record.get("time")),
                        optional_int(record.get("goals")), optional_int(record.get("assists")),
                        optional_int(record.get("shots")), optional_int(record.get("key_passes")),
                        optional_float(record.get("xG")), optional_float(record.get("xA")),
                        optional_int(record.get("npg")), optional_float(record.get("npxG")),
                        optional_float(record.get("xGChain")), optional_float(record.get("xGBuildup")),
                        record.get("position"), json_text(record), retrieved_at,
                    ],
                )
                if player_number % 100 == 0:
                    self.connection.execute("COMMIT")
                    self.connection.execute("BEGIN TRANSACTION")
            self.connection.execute("COMMIT")

    def load_statsbomb(self) -> None:
        source = "statsbomb_open"
        match_items = list(self.catalog.iter(source, "matches"))
        for item in match_items:
            payload = self.catalog.read_json(item)
            artifact_id = item["_raw_artifact_id"]
            retrieved_at = parse_datetime(item["retrieved_at"])
            for match in payload:
                competition = match.get("competition", {})
                season = match.get("season", {})
                competition_id = self.warehouse.resolve_competition(
                    source,
                    competition.get("competition_id"),
                    competition.get("competition_name", "Unknown"),
                    country_code=competition.get("country_name"),
                    competition_type="international_tournament"
                    if competition.get("country_name") == "International"
                    else None,
                )
                season_id = self.warehouse.resolve_season(
                    source,
                    f"{competition.get('competition_id')}|{season.get('season_id')}",
                    competition_id,
                    str(season.get("season_name", "Unknown")),
                )
                home = match.get("home_team", {})
                away = match.get("away_team", {})
                team_type = "national" if competition.get("country_name") == "International" else "club"
                home_id = self.warehouse.resolve_team(
                    source, home.get("home_team_id"), home.get("home_team_name", "Unknown"),
                    team_type=team_type, country_code=home.get("country", {}).get("name")
                )
                away_id = self.warehouse.resolve_team(
                    source, away.get("away_team_id"), away.get("away_team_name", "Unknown"),
                    team_type=team_type, country_code=away.get("country", {}).get("name")
                )
                kickoff = parse_datetime(
                    f"{match.get('match_date')}T{match.get('kick_off', '12:00:00')}"
                )
                fixture_id = self.warehouse.resolve_fixture(
                    source,
                    match.get("match_id"),
                    home_team_id=home_id,
                    away_team_id=away_id,
                    scheduled_kickoff=kickoff,
                    competition_id=competition_id,
                    season_id=season_id,
                    status="completed" if match.get("match_status") == "available" else match.get("match_status"),
                    venue_name=match.get("stadium", {}).get("name"),
                    neutral_venue=competition.get("country_name") == "International",
                    stage=match.get("competition_stage", {}).get("name"),
                    round_name=str(match.get("match_week")) if match.get("match_week") else None,
                )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO fixture_result_observation (
                        observation_id, fixture_id, source_code, raw_artifact_id,
                        observed_at, retrieved_at, home_score_regulation,
                        away_score_regulation, result_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'final')
                    """,
                    [
                        stable_id("result", source, match.get("match_id")), fixture_id,
                        source, artifact_id, kickoff, retrieved_at,
                        optional_int(match.get("home_score")), optional_int(match.get("away_score")),
                    ],
                )

        for item in self.catalog.iter(source, "lineups"):
            payload = self.catalog.read_json(item)
            artifact_id = item["_raw_artifact_id"]
            retrieved_at = parse_datetime(item["retrieved_at"])
            match_id = self._source_id_from_url(item.get("request_url"))
            fixture_id = self.warehouse.mapped_id(source, "fixture", match_id)
            if not fixture_id:
                continue
            for team in payload:
                team_id = self.warehouse.resolve_team(
                    source, team.get("team_id"), team.get("team_name", "Unknown"), team_type="national"
                )
                snapshot_id = stable_id("lineup", source, match_id, team.get("team_id"))
                players = team.get("lineup", [])
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO lineup_snapshot VALUES
                    (?, ?, ?, ?, ?, 'corrected_after_match', ?, ?, ?, ?)
                    """,
                    [snapshot_id, fixture_id, team_id, source, artifact_id, None, None, retrieved_at, bool(players)],
                )
                for record in players:
                    positions = record.get("positions", [])
                    started = any(position.get("start_reason") == "Starting XI" for position in positions)
                    position_name = positions[0].get("position") if positions else None
                    player_id = self.warehouse.resolve_player(
                        source, record.get("player_id"), record.get("player_name", "Unknown"),
                        nationality_code=record.get("country", {}).get("name"), primary_position=position_name
                    )
                    self.connection.execute(
                        """
                        INSERT OR REPLACE INTO lineup_player VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot_id, player_id, "starter" if started else "substitute",
                            position_name, None, optional_int(record.get("jersey_number")), None,
                            position_name == "Goalkeeper",
                        ],
                    )

        for item in self.catalog.iter(source, "events"):
            payload = self.catalog.read_json(item)
            artifact_id = item["_raw_artifact_id"]
            retrieved_at = parse_datetime(item["retrieved_at"])
            match_id = self._source_id_from_url(item.get("request_url"))
            fixture_id = self.warehouse.mapped_id(source, "fixture", match_id)
            if not fixture_id:
                continue
            for event in payload:
                team = event.get("team") or {}
                team_id = None
                if team.get("id"):
                    team_id = self.warehouse.resolve_team(
                        source, team["id"], team.get("name", "Unknown"), team_type="national"
                    )
                player = event.get("player") or {}
                player_id = None
                if player.get("id"):
                    player_id = self.warehouse.resolve_player(
                        source, player["id"], player.get("name", "Unknown")
                    )
                secondary = event.get("pass", {}).get("recipient") or event.get("substitution", {}).get("replacement") or {}
                secondary_id = None
                if secondary.get("id"):
                    secondary_id = self.warehouse.resolve_player(
                        source, secondary["id"], secondary.get("name", "Unknown")
                    )
                location = event.get("location") or [None, None]
                end_location = (
                    event.get("pass", {}).get("end_location")
                    or event.get("shot", {}).get("end_location")
                    or [None, None]
                )
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO match_event VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        stable_id("event", source, event.get("id")), fixture_id, team_id,
                        player_id, secondary_id, source, str(event.get("id")), artifact_id,
                        event.get("type", {}).get("name", "Unknown"),
                        event.get("shot", {}).get("outcome", {}).get("name"),
                        optional_int(event.get("period")), optional_int(event.get("minute")),
                        None, optional_int(event.get("second")), optional_float(location[0]),
                        optional_float(location[1]), optional_float(end_location[0]),
                        optional_float(end_location[1]), optional_float(event.get("shot", {}).get("statsbomb_xg")),
                        json_text(event), retrieved_at,
                    ],
                )

    def load_api_football(self) -> None:
        source = "api_football"
        for resource in (
            "fixtures_by_date",
            "fixture_by_id",
            "fixture_details_batch",
            "pro_validation_fixture_batch",
            "historical_coverage_sample",
            "historical_backfill_batch",
        ):
            for item in self.catalog.iter(source, resource):
                self.load_api_football_payload(self.catalog.read_json(item), item, resource)

        for item in self.catalog.iter(source, "fixture_lineups"):
            self.load_api_football_payload(self.catalog.read_json(item), item, "fixture_lineups")

        for item in self.catalog.iter(source, "fixture_events"):
            self.load_api_football_payload(self.catalog.read_json(item), item, "fixture_events")

        for item in self.catalog.iter(source, "fixture_players"):
            self.load_api_football_payload(self.catalog.read_json(item), item, "fixture_players")

        for item in self.catalog.iter(source, "fixture_statistics"):
            self.load_api_football_payload(self.catalog.read_json(item), item, "fixture_statistics")

    def load_api_football_payload(self, payload: object, item: dict, resource: str) -> None:
        """Normalize one API-Football response without rescanning the raw archive."""
        if not isinstance(payload, dict):
            return
        response = payload.get("response")
        if not isinstance(response, list):
            return
        if resource in {
            "fixtures_by_date",
            "fixture_by_id",
            "fixture_details_batch",
            "pro_validation_fixture_batch",
            "historical_coverage_sample",
            "historical_backfill_batch",
        }:
            for match in response:
                if not isinstance(match, dict):
                    continue
                fixture_id = self._load_api_fixture(match, item)
                fixture_source_id = match.get("fixture", {}).get("id")
                if resource in {
                    "fixture_by_id",
                    "fixture_details_batch",
                    "pro_validation_fixture_batch",
                    "historical_coverage_sample",
                    "historical_backfill_batch",
                }:
                    self._load_api_players(match.get("players", []), fixture_source_id, fixture_id, item)
                    self._load_api_lineups(match.get("lineups", []), fixture_source_id, fixture_id, item)
                    self._load_api_events(match.get("events", []), fixture_source_id, fixture_id, item)
                    self._load_api_statistics(match.get("statistics", []), fixture_source_id, fixture_id, item)
            return

        fixture_source_id = item.get("request_parameters", {}).get("fixture")
        fixture_id = self.warehouse.mapped_id("api_football", "fixture", fixture_source_id)
        if not fixture_id:
            return
        loaders = {
            "fixture_lineups": self._load_api_lineups,
            "fixture_events": self._load_api_events,
            "fixture_players": self._load_api_players,
            "fixture_statistics": self._load_api_statistics,
        }
        loader = loaders.get(resource)
        if loader:
            loader(response, fixture_source_id, fixture_id, item)

    def _api_fixture_team_type(self, fixture_id: str) -> str:
        cached = self._api_fixture_team_type_cache.get(fixture_id)
        if cached:
            return cached
        row = self.connection.execute(
            """
            SELECT c.competition_type
            FROM fixture f LEFT JOIN competition c USING (competition_id)
            WHERE f.fixture_id = ?
            """,
            [fixture_id],
        ).fetchone()
        return "national" if row and row[0] == "international_tournament" else "club"

    def _load_api_lineups(self, records: object, fixture_source_id: object, fixture_id: str, item: dict) -> None:
        if not isinstance(records, list):
            return
        source = "api_football"
        team_type = self._api_fixture_team_type(fixture_id)
        retrieved_at = parse_datetime(item["retrieved_at"])
        for team in records:
            team_data = team.get("team", {})
            team_id = self._resolve_api_team(
                team_data.get("id"), team_data.get("name", "Unknown"), team_type
            )
            snapshot_id = stable_id(
                "lineup", source, fixture_source_id, team_data.get("id"), item["content_sha256"]
            )
            starters = team.get("startXI") or []
            self.connection.execute(
                """INSERT OR REPLACE INTO lineup_snapshot VALUES
                (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)""",
                [snapshot_id, fixture_id, team_id, source, item["_raw_artifact_id"],
                 team.get("formation"), retrieved_at, retrieved_at, len(starters) == 11],
            )
            # Reprocessing a corrected snapshot must replace, not append to,
            # its player membership.
            self.connection.execute(
                "DELETE FROM lineup_player WHERE lineup_snapshot_id=?", [snapshot_id]
            )
            entries, _, _ = deduplicate_api_lineup_entries(team)
            aliases = [
                LineupAlias(
                    index=index,
                    source_player_id=str((entry.get("player") or {}).get("id")),
                    name=(entry.get("player") or {}).get("name", "Unknown"),
                    role=role,
                    shirt_number=optional_int((entry.get("player") or {}).get("number")),
                    position=(entry.get("player") or {}).get("pos"),
                )
                for index, (role, entry) in enumerate(entries)
            ]
            decisions = link_team_players(
                aliases,
                self._api_stat_candidates_for_team(
                    fixture_id, team_id, item["_raw_artifact_id"]
                ),
            )
            mapping_rows: list[tuple] = []
            lineup_rows: list[list] = []
            for alias, (role, entry) in zip(aliases, entries):
                player = entry.get("player", {})
                decision = decisions[alias.index]
                alias_source_id = (
                    f"{fixture_source_id}|{player.get('id')}|"
                    f"{normalized_name(player.get('name', 'Unknown'))}"
                )
                if decision.player_id:
                    player_id = decision.player_id
                    match_method = f"evidence:{decision.method}"
                    confidence = decision.confidence
                    review_status = "automatic"
                else:
                    player_id = self.warehouse.resolve_player(
                        "api_football_lineup", alias_source_id,
                        player.get("name", "Unknown"),
                        primary_position=player.get("pos"),
                    )
                    match_method = "unresolved_alias"
                    confidence = 0.0
                    review_status = "pending"
                mapping_rows.append((
                    "api_football_lineup", "player", alias_source_id,
                    player_id, player.get("name", "Unknown"), match_method,
                    confidence, review_status,
                ))
                alias_key = (
                    str(fixture_source_id), team_id, str(player.get("id")),
                    api_player_comparison_name(player.get("name", "Unknown")),
                )
                self._api_lineup_alias_map[alias_key] = player_id
                lineup_rows.append(
                    [snapshot_id, player_id, role, player.get("pos"), player.get("grid"),
                     optional_int(player.get("number")), False, player.get("pos") == "G"]
                )
            self.warehouse._map_entities(mapping_rows)
            if lineup_rows:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO lineup_player VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    lineup_rows,
                )

    def _load_api_events(self, records: object, fixture_source_id: object, fixture_id: str, item: dict) -> None:
        if not isinstance(records, list):
            return
        source = "api_football"
        team_type = self._api_fixture_team_type(fixture_id)
        retrieved_at = parse_datetime(item["retrieved_at"])
        occurrences: dict[tuple, int] = {}
        event_rows: list[list] = []
        for event in records:
            team = event.get("team") or {}
            player = event.get("player") or {}
            assist = event.get("assist") or {}
            team_id = self._resolve_api_team(
                team.get("id"), team.get("name", "Unknown"), team_type
            ) if team.get("id") else None
            player_id = self._resolve_api_event_player(
                fixture_source_id=fixture_source_id,
                fixture_id=fixture_id, team_id=team_id,
                source_player_id=player.get("id"), name=player.get("name", "Unknown"),
            ) if player.get("id") else None
            assist_id = self._resolve_api_event_player(
                fixture_source_id=fixture_source_id,
                fixture_id=fixture_id, team_id=team_id,
                source_player_id=assist.get("id"), name=assist.get("name", "Unknown"),
            ) if assist.get("id") else None
            event_key = (
                fixture_source_id,
                event.get("time", {}).get("elapsed"),
                event.get("time", {}).get("extra"),
                team.get("id"),
                player.get("id"),
                normalized_name(player.get("name", "")),
                event.get("type"),
            )
            occurrence = occurrences.get(event_key, 0)
            occurrences[event_key] = occurrence + 1
            source_event_id = (
                f"{fixture_source_id}|{event.get('time', {}).get('elapsed')}|"
                f"{event.get('time', {}).get('extra')}|{team.get('id')}|"
                f"{player.get('id')}|{normalized_name(player.get('name', ''))}|"
                f"{event.get('type')}|{occurrence}"
            )
            event_rows.append(
                [stable_id("event", source, source_event_id), fixture_id, team_id, player_id,
                 assist_id, source, source_event_id, item["_raw_artifact_id"],
                 event.get("type", "Unknown"), event.get("detail"), None,
                 optional_int(event.get("time", {}).get("elapsed")),
                 optional_int(event.get("time", {}).get("extra")), None,
                 None, None, None, None, None, json_text(event), retrieved_at]
            )
        if event_rows:
            self.connection.executemany(
                "INSERT OR REPLACE INTO match_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                event_rows,
            )

    def _load_api_players(self, records: object, fixture_source_id: object, fixture_id: str, item: dict) -> None:
        if not isinstance(records, list):
            return
        source = "api_football"
        team_type = self._api_fixture_team_type(fixture_id)
        retrieved_at = parse_datetime(item["retrieved_at"])
        for team_record in records:
            team = team_record.get("team", {})
            team_id = self.warehouse.resolve_team(
                source, team.get("id"), team.get("name", "Unknown"), team_type=team_type
            )
            for record in team_record.get("players") or []:
                player = record.get("player", {})
                statistics = (record.get("statistics") or [{}])[0]
                games = statistics.get("games", {})
                goals = statistics.get("goals", {})
                shots = statistics.get("shots", {})
                passes = statistics.get("passes", {})
                cards = statistics.get("cards", {})
                penalty = statistics.get("penalty", {})
                tackles = statistics.get("tackles", {})
                duels = statistics.get("duels", {})
                dribbles = statistics.get("dribbles", {})
                fouls = statistics.get("fouls", {})
                player_name = player.get("name", "Unknown")
                player_identity_key = api_player_identity_key(
                    player.get("id"), player_name
                )
                player_id = self.warehouse.resolve_player(
                    source, player_identity_key, player_name,
                    primary_position=games.get("position"),
                )
                if self._use_runtime_api_player_id_cache:
                    self._api_runtime_source_ids_by_internal.setdefault(
                        player_id, set()
                    ).add(player_identity_key)
                started = not bool(games.get("substitute"))
                total_passes, accurate_passes, pass_accuracy_pct = parse_api_passes(passes)
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO player_match_stat_observation (
                        observation_id, fixture_id, team_id, player_id, source_code,
                        raw_artifact_id, minutes_played, started, position_code, goals,
                        assists, shots, shots_on_target, key_passes, passes, accurate_passes,
                        yellow_cards, red_cards, penalties_scored, statistics, retrieved_at,
                        xg, xa, npxg, pass_accuracy_pct, rating, captain, shirt_number,
                        goals_conceded, goalkeeper_saves, tackles, tackle_blocks,
                        interceptions, duels, duels_won, dribbles_attempted,
                        dribbles_successful, dribbled_past, fouls_drawn, fouls_committed,
                        yellow_red_cards, penalties_won, penalties_committed,
                        penalties_missed, penalties_saved
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    [stable_id("player_stat", source, fixture_source_id,
                               api_player_identity_key(player.get("id"), player_name)),
                     fixture_id, team_id, player_id, source, item["_raw_artifact_id"],
                     optional_int(games.get("minutes")), started, games.get("position"),
                     optional_int(goals.get("total")) or 0, optional_int(goals.get("assists")) or 0,
                     optional_int(shots.get("total")), optional_int(shots.get("on")),
                     optional_int(passes.get("key")), total_passes,
                     accurate_passes, optional_int(cards.get("yellow")),
                     optional_int(cards.get("red")), optional_int(penalty.get("scored")),
                     json_text(statistics), retrieved_at, pass_accuracy_pct,
                     optional_float(games.get("rating")), games.get("captain"),
                     optional_int(games.get("number")), optional_int(goals.get("conceded")),
                     optional_int(goals.get("saves")), optional_int(tackles.get("total")),
                     optional_int(tackles.get("blocks")), optional_int(tackles.get("interceptions")),
                     optional_int(duels.get("total")), optional_int(duels.get("won")),
                     optional_int(dribbles.get("attempts")), optional_int(dribbles.get("success")),
                     optional_int(dribbles.get("past")), optional_int(fouls.get("drawn")),
                     optional_int(fouls.get("committed")), optional_int(cards.get("yellowred")),
                     optional_int(penalty.get("won")), optional_int(penalty.get("commited")),
                     optional_int(penalty.get("missed")), optional_int(penalty.get("saved"))],
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO appearance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [stable_id("appearance", source, fixture_source_id,
                               api_player_identity_key(player.get("id"), player_name)),
                     fixture_id, team_id, player_id, source, item["_raw_artifact_id"], started,
                     optional_int(games.get("minutes")), games.get("position"),
                     optional_int(games.get("number")), optional_float(games.get("rating")), retrieved_at],
                )

    def _api_stat_candidates_for_team(
        self, fixture_id: str, team_id: str, raw_artifact_id: str
    ) -> list[StatCandidate]:
        if self._api_stat_candidate_cache is not None:
            return self._api_stat_candidate_cache.get(
                (fixture_id, team_id, raw_artifact_id), []
            )
        if self._use_runtime_api_player_id_cache:
            rows = self.connection.execute(
                """
                SELECT s.player_id, p.full_name, s.minutes_played, s.started,
                       s.shirt_number, s.position_code
                FROM player_match_stat_observation s
                JOIN player p USING (player_id)
                WHERE s.fixture_id=? AND s.team_id=?
                  AND s.source_code='api_football' AND s.raw_artifact_id=?
                ORDER BY s.player_id
                """,
                [fixture_id, team_id, raw_artifact_id],
            ).fetchall()
            return [
                StatCandidate(
                    player_id=row[0], name=row[1], minutes_played=row[2],
                    started=row[3], shirt_number=row[4], position=row[5],
                    source_player_ids=tuple(sorted(
                        self._api_runtime_source_ids_by_internal.get(row[0], set())
                    )),
                )
                for row in rows
            ]
        rows = self.connection.execute(
            """
            SELECT s.player_id, p.full_name, s.minutes_played, s.started,
                   s.shirt_number, s.position_code,
                   list(m.source_entity_id ORDER BY m.source_entity_id)
            FROM player_match_stat_observation s
            JOIN player p USING (player_id)
            LEFT JOIN source_entity_map m
              ON m.internal_entity_id=p.player_id
             AND m.source_code='api_football' AND m.entity_type='player'
            WHERE s.fixture_id=? AND s.team_id=? AND s.source_code='api_football'
              AND s.raw_artifact_id=?
            GROUP BY s.player_id, p.full_name, s.minutes_played, s.started,
                     s.shirt_number, s.position_code
            ORDER BY s.player_id
            """,
            [fixture_id, team_id, raw_artifact_id],
        ).fetchall()
        return [
            StatCandidate(
                player_id=row[0], name=row[1], minutes_played=row[2],
                started=row[3], shirt_number=row[4], position=row[5],
                source_player_ids=tuple(value for value in row[6] if value is not None),
            )
            for row in rows
        ]

    def _resolve_api_event_player(
        self,
        *,
        fixture_source_id: object,
        fixture_id: str,
        team_id: str | None,
        source_player_id: object,
        name: str,
    ) -> str:
        alias_key = (
            str(fixture_source_id), str(team_id), str(source_player_id),
            api_player_comparison_name(name),
        )
        lineup_player_id = self._api_lineup_alias_map.get(alias_key)
        if lineup_player_id:
            return lineup_player_id
        identity_key = api_player_identity_key(source_player_id, name)
        cached_player = self._api_player_id_cache.get(identity_key)
        mapped = cached_player[0] if cached_player else self.warehouse.mapped_id(
            "api_football", "player", identity_key
        )
        mapped_name = cached_player[1] if cached_player else None
        if mapped:
            if mapped_name is None:
                row = self.connection.execute(
                    "SELECT full_name FROM player WHERE player_id=?", [mapped]
                ).fetchone()
                mapped_name = row[0] if row else None
            if mapped_name and (
                compatible_api_player_names(name, mapped_name)
                or compatible_api_player_compound_names(name, mapped_name)
            ):
                return mapped
        lineup_source_id = (
            f"{fixture_source_id}|{source_player_id}|{normalized_name(name)}"
        )
        mapped_lineup = self.warehouse.mapped_id(
            "api_football_lineup", "player", lineup_source_id
        )
        if mapped_lineup:
            return mapped_lineup
        event_source_id = (
            f"{fixture_source_id}|{source_player_id}|{normalized_name(name)}"
        )
        return self.warehouse.resolve_player(
            "api_football_event", event_source_id, name
        )

    def _load_api_statistics(self, records: object, fixture_source_id: object, fixture_id: str, item: dict) -> None:
        if not isinstance(records, list):
            return
        source = "api_football"
        team_type = self._api_fixture_team_type(fixture_id)
        retrieved_at = parse_datetime(item["retrieved_at"])
        for team_record in records:
            team = team_record.get("team", {})
            team_id = self.warehouse.resolve_team(
                source, team.get("id"), team.get("name", "Unknown"), team_type=team_type
            )
            stats = {entry.get("type"): entry.get("value") for entry in team_record.get("statistics", [])}
            self.connection.execute(
                """
                INSERT OR REPLACE INTO team_match_stat_observation (
                    observation_id, fixture_id, team_id, source_code, raw_artifact_id,
                    period, shots, shots_on_target, xg, possession_pct, corners, fouls,
                    yellow_cards, red_cards, passes, accurate_passes, statistics, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, 'regulation', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [stable_id("team_stat", source, fixture_source_id, team.get("id")), fixture_id,
                 team_id, source, item["_raw_artifact_id"], optional_int(stats.get("Total Shots")),
                 optional_int(stats.get("Shots on Goal")), optional_float(stats.get("expected_goals")),
                 optional_float(stats.get("Ball Possession")), optional_int(stats.get("Corner Kicks")),
                 optional_int(stats.get("Fouls")), optional_int(stats.get("Yellow Cards")),
                 optional_int(stats.get("Red Cards")), optional_int(stats.get("Total passes")),
                 optional_int(stats.get("Passes accurate")), json_text(stats), retrieved_at],
            )

    def _load_api_fixture(self, match: dict, item: dict) -> str:
        source = "api_football"
        league = match.get("league", {})
        teams = match.get("teams", {})
        fixture = match.get("fixture", {})
        league_name = league.get("name", "Unknown")
        international = league_name in {
            "World Cup", "Friendlies", "UEFA Nations League", "Euro Championship",
            "Copa America", "Africa Cup of Nations"
        }
        competition_id = self.warehouse.resolve_competition(
            source, league.get("id"), league_name, country_code=league.get("country"),
            competition_type="international_tournament" if international else None
        )
        season_name = str(league.get("season", "Unknown"))
        season_id = self.warehouse.resolve_season(
            source, f"{league.get('id')}|{season_name}", competition_id, season_name
        )
        team_type = "national" if international else "club"
        home = teams.get("home", {})
        away = teams.get("away", {})
        home_id = self.warehouse.resolve_team(
            source, home.get("id"), home.get("name", "Unknown"), team_type=team_type
        )
        away_id = self.warehouse.resolve_team(
            source, away.get("id"), away.get("name", "Unknown"), team_type=team_type
        )
        kickoff = parse_datetime(fixture.get("date"))
        status_short = fixture.get("status", {}).get("short")
        administrative_unplayed = bool(match.get("_administrative_result_unplayed"))
        canonical_status = canonical_api_football_status(
            status_short, administrative_unplayed=administrative_unplayed
        )
        status = (
            "administrative_result_unplayed"
            if administrative_unplayed
            else "completed" if status_short in {"FT", "AET", "PEN"}
            else "scheduled" if status_short in {"NS", "TBD"}
            else status_short
        )
        fixture_id = self.warehouse.resolve_fixture(
            source, fixture.get("id"), home_team_id=home_id, away_team_id=away_id,
            scheduled_kickoff=kickoff, competition_id=competition_id, season_id=season_id,
            status=status, venue_name=fixture.get("venue", {}).get("name"),
            neutral_venue=None, round_name=league.get("round")
        )
        fixture_source_id = str(fixture.get("id"))
        retrieved_at = parse_datetime(item["retrieved_at"])
        self.connection.execute(
            """
            INSERT INTO fixture_schedule_observation (
                schedule_observation_id, fixture_id, source_code, fixture_source_id,
                provider_status, canonical_status, scheduled_kickoff, observed_at,
                retrieved_at, raw_artifact_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (schedule_observation_id) DO UPDATE SET
                fixture_id = excluded.fixture_id,
                provider_status = excluded.provider_status,
                canonical_status = excluded.canonical_status,
                scheduled_kickoff = excluded.scheduled_kickoff,
                observed_at = excluded.observed_at,
                retrieved_at = excluded.retrieved_at,
                raw_artifact_id = excluded.raw_artifact_id
            """,
            [
                stable_id("fixture_schedule_observation", source, fixture_source_id,
                          item["_raw_artifact_id"]),
                fixture_id,
                source,
                fixture_source_id,
                str(status_short) if status_short is not None else None,
                canonical_status,
                kickoff,
                None,
                retrieved_at,
                item["_raw_artifact_id"],
            ],
        )
        if canonical_status == "unknown":
            self.connection.execute(
                """
                INSERT OR REPLACE INTO data_quality_issue (
                    issue_id, rule_code, severity, entity_type, internal_entity_id,
                    source_code, raw_artifact_id, details, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    stable_id(
                        "quality_issue", "api_unknown_fixture_status", fixture_id,
                        item["_raw_artifact_id"]
                    ),
                    "api_unknown_fixture_status",
                    "warning",
                    "fixture",
                    fixture_id,
                    source,
                    item["_raw_artifact_id"],
                    json_text({"provider_status": status_short}),
                    "open",
                ],
            )
        score = match.get("score", {})
        fulltime = score.get("fulltime", {})
        if fulltime.get("home") is not None:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO fixture_result_observation (
                    observation_id, fixture_id, source_code, raw_artifact_id, observed_at,
                    retrieved_at, home_score_regulation, away_score_regulation,
                    halftime_home_score, halftime_away_score, home_score_extra_time,
                    away_score_extra_time, home_score_penalties, away_score_penalties, result_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    stable_id("result", source, fixture.get("id")), fixture_id, source,
                    item["_raw_artifact_id"], kickoff, parse_datetime(item["retrieved_at"]),
                    optional_int(fulltime.get("home")), optional_int(fulltime.get("away")),
                    optional_int(score.get("halftime", {}).get("home")),
                    optional_int(score.get("halftime", {}).get("away")),
                    optional_int(score.get("extratime", {}).get("home")),
                    optional_int(score.get("extratime", {}).get("away")),
                    optional_int(score.get("penalty", {}).get("home")),
                    optional_int(score.get("penalty", {}).get("away")), "final",
                ],
            )
        return fixture_id

    def load_polymarket(self) -> None:
        for source, resource in (("polymarket_gamma", "soccer_events"), ("polymarket_gamma", "fixture_search")):
            for item in self.catalog.iter(source, resource):
                self.load_polymarket_payload(resource, self.catalog.read_json(item), item)

        for resource in ("order_book", "order_books_batch"):
            for item in self.catalog.iter("polymarket_clob", resource, unique_content=False):
                self.load_polymarket_payload(resource, self.catalog.read_json(item), item)

        for item in self.catalog.iter("polymarket_clob", "price_history", unique_content=False):
            self.load_polymarket_payload("price_history", self.catalog.read_json(item), item)

    def load_polymarket_payload(self, resource: str, payload: object, item: dict) -> None:
        """Normalize one Gamma or CLOB response without rescanning prior artifacts."""
        if resource in {"soccer_events", "fixture_search"}:
            events = payload.get("events", []) if isinstance(payload, dict) else payload
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        self._load_polymarket_event(event, item)
            return
        if resource in {"order_book", "order_books_batch"}:
            books = payload if isinstance(payload, list) else [payload]
            for book in books:
                if isinstance(book, dict):
                    self._load_polymarket_order_book(book, item)
            return
        if resource == "price_history" and isinstance(payload, dict):
            token_id = str(item.get("request_parameters", {}).get("market", ""))
            for point in payload.get("history", []):
                timestamp = self._unix_timestamp(point.get("t"))
                price = optional_float(point.get("p"))
                if timestamp and price is not None:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO market_price_history VALUES (?, ?, ?, ?)",
                        [token_id, timestamp, price, item["_raw_artifact_id"]],
                    )

    def _load_polymarket_order_book(self, payload: dict, item: dict) -> None:
        token_id = str(
            payload.get("asset_id")
            or item.get("request_parameters", {}).get("token_id", "")
        )
        if not token_id:
            return
        outcome = self.connection.execute(
            "SELECT outcome_id FROM prediction_market_outcome WHERE source_token_id = ? LIMIT 1",
            [token_id],
        ).fetchone()
        observed_at = self._unix_timestamp(payload.get("timestamp")) or parse_datetime(item["retrieved_at"])
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        bid_prices = [optional_float(level.get("price")) for level in bids]
        ask_prices = [optional_float(level.get("price")) for level in asks]
        best_bid = max((price for price in bid_prices if price is not None), default=None)
        best_ask = min((price for price in ask_prices if price is not None), default=None)
        snapshot_id = stable_id("orderbook", token_id, observed_at, item["content_sha256"])
        self.connection.execute(
            "INSERT OR REPLACE INTO orderbook_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [snapshot_id, outcome[0] if outcome else None, token_id, payload.get("market"),
             observed_at, parse_datetime(item["retrieved_at"]), best_bid, best_ask,
             optional_float(payload.get("tick_size")), optional_float(payload.get("min_order_size")),
             item["_raw_artifact_id"]],
        )
        for side, levels in (("bid", bids), ("ask", asks)):
            for index, level in enumerate(levels):
                price = optional_float(level.get("price"))
                size = optional_float(level.get("size"))
                if price is not None and size is not None:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO orderbook_level VALUES (?, ?, ?, ?, ?)",
                        [snapshot_id, side, index, price, size],
                    )

    def _load_polymarket_event(self, event: dict, item: dict) -> None:
        source_event_id = str(event.get("id"))
        if not source_event_id or source_event_id == "None":
            return
        event_id = stable_id("prediction_market_event", source_event_id)
        retrieved_at = parse_datetime(item["retrieved_at"])
        self.connection.execute(
            """
            INSERT INTO prediction_market_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (prediction_market_event_id) DO UPDATE SET
                title = excluded.title, active = excluded.active, closed = excluded.closed,
                retrieved_at = excluded.retrieved_at
            """,
            [
                event_id, source_event_id, event.get("title"), event.get("slug"),
                event.get("description"), None, parse_datetime(event.get("startTime") or event.get("startDate")),
                parse_datetime(event.get("endDate")), event.get("resolutionSource"),
                event.get("active"), event.get("closed"), retrieved_at,
            ],
        )
        for market in event.get("markets", []):
            source_market_id = str(market.get("id"))
            if not source_market_id or source_market_id == "None":
                continue
            market_id = stable_id("prediction_market", source_market_id)
            self.connection.execute(
                """
                INSERT INTO prediction_market VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (prediction_market_id) DO UPDATE SET
                    active = excluded.active, closed = excluded.closed, volume = excluded.volume,
                    liquidity = excluded.liquidity, retrieved_at = excluded.retrieved_at
                """,
                [
                    market_id, event_id, source_market_id, market.get("question"), market.get("slug"),
                    market.get("sportsMarketType") or "unclassified", optional_float(market.get("line")),
                    market.get("description"), market.get("active"), market.get("closed"),
                    optional_float(market.get("volumeNum") or market.get("volume")),
                    optional_float(market.get("liquidityNum") or market.get("liquidity")), retrieved_at,
                ],
            )
            outcomes = parse_json_list(market.get("outcomes"))
            prices = parse_json_list(market.get("outcomePrices"))
            tokens = parse_json_list(market.get("clobTokenIds"))
            for index, outcome_name in enumerate(outcomes):
                token = str(tokens[index]) if index < len(tokens) else None
                price = optional_float(prices[index]) if index < len(prices) else None
                self.connection.execute(
                    "INSERT OR REPLACE INTO prediction_market_outcome VALUES (?, ?, ?, ?, ?)",
                    [stable_id("market_outcome", source_market_id, index), market_id, token, str(outcome_name), price],
                )

    @staticmethod
    def _source_id_from_url(url: str | None) -> str | None:
        if not url:
            return None
        match = re.search(r"/(\d+)\.json(?:\?|$)", url)
        return match.group(1) if match else None

    @staticmethod
    def _unix_timestamp(value) -> datetime | None:
        numeric = optional_float(value)
        if numeric is None:
            return None
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
