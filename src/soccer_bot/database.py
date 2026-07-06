from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import uuid

import duckdb


ID_NAMESPACE = uuid.UUID("5f4294ea-34fd-4f8d-9b23-c6829dcfa965")


def stable_id(kind: str, *parts: object) -> str:
    value = "|".join([kind, *("" if part is None else str(part) for part in parts)])
    return str(uuid.uuid5(ID_NAMESPACE, value))


def normalized_name(value: str) -> str:
    value = str(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Warehouse:
    def __init__(
        self,
        database_path: Path,
        migrations_path: Path,
        aliases_path: Path | None = None,
    ) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.migrations_path = migrations_path
        self.team_aliases: dict[str, str] = {}
        if aliases_path and aliases_path.exists():
            aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
            self.team_aliases = {
                normalized_name(alias): normalized_name(canonical)
                for alias, canonical in aliases.get("teams", {}).items()
            }
        self.connection = duckdb.connect(str(database_path))
        self.connection.execute("SET preserve_insertion_order = false")
        self.connection.execute("SET threads = 2")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version VARCHAR PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
            )
            """
        )
        applied = {
            row[0]
            for row in self.connection.execute(
                "SELECT version FROM schema_migration"
            ).fetchall()
        }
        for path in sorted(self.migrations_path.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            with self.transaction():
                self.connection.execute(path.read_text(encoding="utf-8"))
                self.connection.execute(
                    "INSERT INTO schema_migration (version) VALUES (?)", [version]
                )

    def register_sources(self) -> None:
        rows = [
            ("api_football", "API-Football", "api", "https://v3.football.api-sports.io"),
            ("api_football_lineup", "API-Football lineup identities", "derived", None),
            ("api_football_event", "API-Football event identities", "derived", None),
            ("statsbomb_open", "StatsBomb Open Data", "dataset", "https://github.com/statsbomb/open-data"),
            ("football_data_uk", "Football-Data.co.uk", "dataset", "https://www.football-data.co.uk"),
            ("understat", "Understat", "scraped_json", "https://understat.com"),
            ("polymarket_gamma", "Polymarket Gamma", "api", "https://gamma-api.polymarket.com"),
            ("polymarket_clob", "Polymarket CLOB", "api", "https://clob.polymarket.com"),
        ]
        self.connection.executemany(
            """
            INSERT INTO source (source_code, source_name, source_type, base_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (source_code) DO UPDATE SET
                source_name = excluded.source_name,
                source_type = excluded.source_type,
                base_url = excluded.base_url
            """,
            rows,
        )

    def reconcile_team_aliases(self) -> int:
        """Merge already-created alias teams and any fixtures duplicated by that merge."""
        merged = 0
        team_reference_tables = [
            ("fixture", "home_team_id"),
            ("fixture", "away_team_id"),
            ("lineup_snapshot", "team_id"),
            ("appearance", "team_id"),
            ("match_event", "team_id"),
            ("team_match_stat_observation", "team_id"),
            ("player_match_stat_observation", "team_id"),
            ("player_season_stat", "team_id"),
        ]
        for alias_norm, canonical_norm in self.team_aliases.items():
            alias_rows = self.connection.execute(
                "SELECT team_id FROM team WHERE normalized_name = ?", [alias_norm]
            ).fetchall()
            canonical = self.connection.execute(
                "SELECT team_id FROM team WHERE normalized_name = ? LIMIT 1", [canonical_norm]
            ).fetchone()
            if not alias_rows or not canonical:
                continue
            canonical_id = canonical[0]
            for (alias_id,) in alias_rows:
                if alias_id == canonical_id:
                    continue
                for table, column in team_reference_tables:
                    self.connection.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                        [canonical_id, alias_id],
                    )
                self.connection.execute(
                    """
                    UPDATE source_entity_map SET internal_entity_id = ?
                    WHERE entity_type = 'team' AND internal_entity_id = ?
                    """,
                    [canonical_id, alias_id],
                )
                self.connection.execute("DELETE FROM team WHERE team_id = ?", [alias_id])
                merged += 1
        merged += self._merge_duplicate_fixtures()
        return merged

    def _merge_duplicate_fixtures(self) -> int:
        groups = self.connection.execute(
            """
            SELECT home_team_id, away_team_id, CAST(scheduled_kickoff AS DATE),
                   competition_id, list(fixture_id ORDER BY fixture_id)
            FROM fixture
            GROUP BY home_team_id, away_team_id, CAST(scheduled_kickoff AS DATE), competition_id
            HAVING count(*) > 1
            """
        ).fetchall()
        fixture_reference_tables = [
            "fixture_result_observation",
            "lineup_snapshot",
            "appearance",
            "match_event",
            "team_match_stat_observation",
            "player_match_stat_observation",
            "bookmaker_quote",
        ]
        merged = 0
        for _, _, _, _, fixture_ids in groups:
            keeper = fixture_ids[0]
            for duplicate in fixture_ids[1:]:
                duplicate_data = self.connection.execute(
                    """
                    SELECT scheduled_kickoff, venue_name, neutral_venue, stage, round_name, status
                    FROM fixture WHERE fixture_id = ?
                    """,
                    [duplicate],
                ).fetchone()
                self.connection.execute(
                    """
                    UPDATE fixture SET
                        scheduled_kickoff = coalesce(scheduled_kickoff, ?),
                        venue_name = coalesce(venue_name, ?),
                        neutral_venue = coalesce(neutral_venue, ?),
                        stage = coalesce(stage, ?),
                        round_name = coalesce(round_name, ?),
                        status = coalesce(status, ?)
                    WHERE fixture_id = ?
                    """,
                    [*duplicate_data, keeper],
                )
                for table in fixture_reference_tables:
                    self.connection.execute(
                        f"UPDATE {table} SET fixture_id = ? WHERE fixture_id = ?",
                        [keeper, duplicate],
                    )
                self.connection.execute(
                    "UPDATE prediction_market_event SET fixture_id = ? WHERE fixture_id = ?",
                    [keeper, duplicate],
                )
                self.connection.execute(
                    """
                    UPDATE source_entity_map SET internal_entity_id = ?
                    WHERE entity_type = 'fixture' AND internal_entity_id = ?
                    """,
                    [keeper, duplicate],
                )
                self.connection.execute("DELETE FROM fixture WHERE fixture_id = ?", [duplicate])
                merged += 1
        return merged

    def resolve_competition(
        self,
        source_code: str,
        source_id: object,
        name: str,
        *,
        country_code: str | None = None,
        competition_type: str | None = None,
    ) -> str:
        mapped = self._mapped_id(source_code, "competition", source_id)
        if mapped:
            return mapped
        norm = normalized_name(name)
        existing = self.connection.execute(
            "SELECT competition_id FROM competition WHERE lower(name) = lower(?) AND coalesce(country_code, '') = coalesce(?, '') LIMIT 1",
            [name, country_code],
        ).fetchone()
        internal_id = existing[0] if existing else stable_id("competition", norm, country_code)
        self.connection.execute(
            """
            INSERT INTO competition (competition_id, name, country_code, competition_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (competition_id) DO UPDATE SET
                name = excluded.name,
                country_code = coalesce(excluded.country_code, competition.country_code),
                competition_type = coalesce(excluded.competition_type, competition.competition_type)
            """,
            [internal_id, name, country_code, competition_type],
        )
        self._map_entity(source_code, "competition", source_id, internal_id, name)
        return internal_id

    def resolve_season(
        self,
        source_code: str,
        source_id: object,
        competition_id: str,
        name: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> str:
        mapped = self._mapped_id(source_code, "season", source_id)
        if mapped:
            return mapped
        existing = self.connection.execute(
            "SELECT season_id FROM season WHERE competition_id = ? AND name = ? LIMIT 1",
            [competition_id, name],
        ).fetchone()
        internal_id = existing[0] if existing else stable_id("season", competition_id, name)
        self.connection.execute(
            """
            INSERT INTO season (season_id, competition_id, name, start_date, end_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (season_id) DO UPDATE SET
                start_date = coalesce(excluded.start_date, season.start_date),
                end_date = coalesce(excluded.end_date, season.end_date)
            """,
            [internal_id, competition_id, name, start_date, end_date],
        )
        self._map_entity(source_code, "season", source_id, internal_id, name)
        return internal_id

    def resolve_team(
        self,
        source_code: str,
        source_id: object,
        name: str,
        *,
        team_type: str = "club",
        country_code: str | None = None,
    ) -> str:
        mapped = self._mapped_id(source_code, "team", source_id)
        if mapped:
            return mapped
        norm = normalized_name(name)
        norm = self.team_aliases.get(norm, norm)
        existing = self.connection.execute(
            "SELECT team_id FROM team WHERE normalized_name = ? AND team_type = ? LIMIT 1",
            [norm, team_type],
        ).fetchone()
        internal_id = existing[0] if existing else stable_id("team", team_type, norm, country_code)
        self.connection.execute(
            """
            INSERT INTO team (team_id, name, normalized_name, team_type, country_code)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (team_id) DO UPDATE SET
                name = excluded.name,
                country_code = coalesce(excluded.country_code, team.country_code)
            """,
            [internal_id, name, norm, team_type, country_code],
        )
        self._map_entity(source_code, "team", source_id, internal_id, name)
        return internal_id

    def resolve_player(
        self,
        source_code: str,
        source_id: object,
        name: str,
        *,
        nationality_code: str | None = None,
        primary_position: str | None = None,
    ) -> str:
        mapped = self._mapped_id(source_code, "player", source_id)
        if mapped:
            return mapped
        norm = normalized_name(name)
        # Provider player IDs are authoritative identities. Display names are
        # not: feeds frequently abbreviate them (for example "M. Sylla") and
        # many unrelated players share the same normalized name. Cross-source
        # linkage must therefore be explicit rather than inferred by name.
        internal_id = stable_id(
            "player", source_code,
            source_id if source_id is not None else norm,
            nationality_code if source_id is None else None,
        )
        self.connection.execute(
            """
            INSERT INTO player (player_id, full_name, normalized_name, nationality_code, primary_position)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (player_id) DO UPDATE SET
                full_name = excluded.full_name,
                nationality_code = coalesce(excluded.nationality_code, player.nationality_code),
                primary_position = coalesce(excluded.primary_position, player.primary_position)
            """,
            [internal_id, name, norm, nationality_code, primary_position],
        )
        if source_id is not None:
            self._map_entity(
                source_code, "player", source_id, internal_id, name,
                match_method="provider_source_id", confidence=1.0,
            )
        return internal_id

    def resolve_fixture(
        self,
        source_code: str,
        source_id: object,
        *,
        home_team_id: str,
        away_team_id: str,
        scheduled_kickoff: datetime | None,
        competition_id: str | None,
        season_id: str | None,
        status: str | None,
        venue_name: str | None = None,
        neutral_venue: bool | None = None,
        stage: str | None = None,
        round_name: str | None = None,
    ) -> str:
        mapped = self._mapped_id(source_code, "fixture", source_id)
        if mapped:
            internal_id = mapped
        else:
            match_date = scheduled_kickoff.date() if scheduled_kickoff else None
            existing = self.connection.execute(
                """
                SELECT fixture_id FROM fixture
                WHERE home_team_id = ? AND away_team_id = ?
                  AND (CAST(scheduled_kickoff AS DATE) = ? OR ? IS NULL)
                LIMIT 1
                """,
                [home_team_id, away_team_id, match_date, match_date],
            ).fetchone()
            internal_id = existing[0] if existing else stable_id(
                "fixture", home_team_id, away_team_id, match_date, competition_id
            )
            self._map_entity(source_code, "fixture", source_id, internal_id, None)
        self.connection.execute(
            """
            INSERT INTO fixture (
                fixture_id, competition_id, season_id, home_team_id, away_team_id,
                scheduled_kickoff, venue_name, neutral_venue, stage, round_name, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fixture_id) DO UPDATE SET
                competition_id = coalesce(excluded.competition_id, fixture.competition_id),
                season_id = coalesce(excluded.season_id, fixture.season_id),
                scheduled_kickoff = coalesce(excluded.scheduled_kickoff, fixture.scheduled_kickoff),
                venue_name = coalesce(excluded.venue_name, fixture.venue_name),
                neutral_venue = coalesce(excluded.neutral_venue, fixture.neutral_venue),
                stage = coalesce(excluded.stage, fixture.stage),
                round_name = coalesce(excluded.round_name, fixture.round_name),
                status = coalesce(excluded.status, fixture.status),
                updated_at = now()
            """,
            [
                internal_id,
                competition_id,
                season_id,
                home_team_id,
                away_team_id,
                scheduled_kickoff,
                venue_name,
                neutral_venue,
                stage,
                round_name,
                status,
            ],
        )
        return internal_id

    def mapped_id(self, source_code: str, entity_type: str, source_id: object) -> str | None:
        return self._mapped_id(source_code, entity_type, source_id)

    def _mapped_id(self, source_code: str, entity_type: str, source_id: object) -> str | None:
        row = self.connection.execute(
            """
            SELECT internal_entity_id FROM source_entity_map
            WHERE source_code = ? AND entity_type = ? AND source_entity_id = ?
            """,
            [source_code, entity_type, str(source_id)],
        ).fetchone()
        return row[0] if row else None

    def _map_entity(
        self,
        source_code: str,
        entity_type: str,
        source_id: object,
        internal_id: str,
        source_name: str | None,
        *,
        match_method: str = "normalized_name_or_context",
        confidence: float = 0.8,
        review_status: str = "automatic",
    ) -> None:
        self._map_entities([(
            source_code, entity_type, str(source_id), internal_id, source_name,
            match_method, confidence, review_status,
        )])

    def _map_entities(self, rows: list[tuple]) -> None:
        if not rows:
            return
        self.connection.executemany(
            """
            INSERT INTO source_entity_map (
                source_code, entity_type, source_entity_id, internal_entity_id,
                source_name, match_method, confidence, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_code, entity_type, source_entity_id) DO UPDATE SET
                internal_entity_id = excluded.internal_entity_id,
                source_name = coalesce(excluded.source_name, source_entity_map.source_name),
                match_method = excluded.match_method,
                confidence = excluded.confidence,
                review_status = excluded.review_status
            """,
            rows,
        )


def metadata_artifact_id(metadata_path: Path) -> str:
    return stable_id("raw_artifact", str(metadata_path.resolve()))


def build_id() -> str:
    return stable_id("database_build", datetime.now(timezone.utc).isoformat(), hashlib.sha256().hexdigest())
