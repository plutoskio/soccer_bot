from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import duckdb


class PlayerDatasetError(RuntimeError):
    """Raised when player targets or confirmed lineups are not leakage-safe."""


@dataclass(frozen=True)
class PlayerMatchTarget:
    fixture_id: str
    competition_id: str
    season_id: str | None
    kickoff: datetime
    result_available_at: datetime
    team_id: str
    opponent_team_id: str
    is_home: bool
    player_id: str
    position_code: str
    started: bool
    minutes_played: int
    goals: int
    assists: int
    team_goals: int


@dataclass(frozen=True)
class ConfirmedLineupPlayer:
    player_id: str
    team_id: str
    selection_role: str
    position_code: str


@dataclass(frozen=True)
class ConfirmedLineupFixture:
    fixture_id: str
    competition_id: str
    season_id: str | None
    home_team_id: str
    away_team_id: str
    kickoff: datetime
    prediction_at: datetime
    raw_artifact_id: str
    schedule_observation_id: str
    players: tuple[ConfirmedLineupPlayer, ...]


def load_player_match_targets(
    connection,
    *,
    result_availability_delay_minutes: int,
    source_code: str = "api_football",
    supported_positions: tuple[str, ...] = ("G", "D", "M", "F"),
    kickoff_end_exclusive: datetime | None = None,
) -> tuple[list[PlayerMatchTarget], dict]:
    """Load positive-exposure player labels from player-eligible fixtures.

    Missing minutes remain missing and are excluded. In particular, this function
    does not convert an unused substitute's NULL minutes to zero. Conflicting
    final scores and conflicting duplicate player rows are excluded and counted.
    """

    if result_availability_delay_minutes <= 0:
        raise PlayerDatasetError("Result availability delay must be positive")
    if not supported_positions or len(set(supported_positions)) != len(
        supported_positions
    ):
        raise PlayerDatasetError("Supported positions must be unique and non-empty")
    conditions = [
        "e.eligible_player_models",
        "f.scheduled_kickoff IS NOT NULL",
        "p.source_code = ?",
    ]
    parameters: list[object] = [source_code]
    if kickoff_end_exclusive is not None:
        conditions.append("f.scheduled_kickoff < ?")
        parameters.append(kickoff_end_exclusive)
    rows = connection.execute(
        f"""
        WITH score AS (
            SELECT
                fixture_id,
                min(home_score_regulation) AS home_goals,
                min(away_score_regulation) AS away_goals,
                count(DISTINCT struct_pack(
                    home := home_score_regulation,
                    away := away_score_regulation
                )) AS score_versions
            FROM fixture_result_observation
            WHERE result_status='final'
              AND home_score_regulation IS NOT NULL
              AND away_score_regulation IS NOT NULL
              AND home_score_regulation >= 0
              AND away_score_regulation >= 0
            GROUP BY fixture_id
        )
        SELECT
            f.fixture_id,
            f.competition_id,
            f.season_id,
            f.scheduled_kickoff,
            f.home_team_id,
            f.away_team_id,
            s.home_goals,
            s.away_goals,
            s.score_versions,
            p.team_id,
            p.player_id,
            p.position_code,
            p.started,
            p.minutes_played,
            p.goals,
            p.assists,
            coalesce(ids.is_identity_placeholder, false) AS identity_placeholder,
            count(*) OVER (
                PARTITION BY p.fixture_id, p.team_id, p.player_id
            ) AS duplicate_rows,
            count(DISTINCT struct_pack(
                minutes := p.minutes_played,
                started := p.started,
                goals := p.goals,
                assists := p.assists,
                pos := p.position_code
            )) OVER (
                PARTITION BY p.fixture_id, p.team_id, p.player_id
            ) AS duplicate_versions
        FROM fixture_model_eligibility e
        JOIN fixture f USING (fixture_id)
        JOIN score s USING (fixture_id)
        JOIN player_match_stat_observation p USING (fixture_id)
        LEFT JOIN player_identity_state ids USING (player_id)
        WHERE {' AND '.join(conditions)}
        ORDER BY f.scheduled_kickoff, f.fixture_id, p.team_id, p.player_id,
                 p.observation_id
        """,
        parameters,
    ).fetchall()
    positions = set(supported_positions)
    output: list[PlayerMatchTarget] = []
    seen: set[tuple[str, str, str]] = set()
    exclusions: Counter[str] = Counter()
    for row in rows:
        key = (str(row[0]), str(row[9]), str(row[10]))
        if key in seen:
            continue
        seen.add(key)
        fixture_id = str(row[0])
        home_team_id, away_team_id = str(row[4]), str(row[5])
        team_id = str(row[9])
        if row[8] != 1:
            exclusions["conflicting_final_score"] += 1
            continue
        if row[18] != 1:
            raise PlayerDatasetError(
                f"Conflicting player observations for {key}: versions={row[18]}"
            )
        if row[17] > 1:
            exclusions["identical_duplicate_player_observation"] += row[17] - 1
        if team_id not in {home_team_id, away_team_id}:
            raise PlayerDatasetError(f"Player target has wrong team: {key}")
        if bool(row[16]):
            exclusions["unsafe_player_identity"] += 1
            continue
        position = str(row[11]) if row[11] is not None else ""
        if position not in positions:
            exclusions["unsupported_or_missing_position"] += 1
            continue
        minutes, goals, assists = row[13], row[14], row[15]
        if minutes is None:
            exclusions["minutes_missing_not_assumed_zero"] += 1
            continue
        if not 1 <= minutes <= 130:
            exclusions["nonpositive_or_invalid_minutes"] += 1
            continue
        if goals is None or assists is None:
            exclusions["goal_or_assist_missing"] += 1
            continue
        if goals < 0 or assists < 0:
            raise PlayerDatasetError(f"Negative player target for {key}")
        is_home = team_id == home_team_id
        team_goals = int(row[6] if is_home else row[7])
        output.append(
            PlayerMatchTarget(
                fixture_id=fixture_id,
                competition_id=str(row[1]),
                season_id=None if row[2] is None else str(row[2]),
                kickoff=row[3],
                result_available_at=row[3]
                + timedelta(minutes=result_availability_delay_minutes),
                team_id=team_id,
                opponent_team_id=away_team_id if is_home else home_team_id,
                is_home=is_home,
                player_id=str(row[10]),
                position_code=position,
                started=bool(row[12]),
                minutes_played=int(minutes),
                goals=int(goals),
                assists=int(assists),
                team_goals=team_goals,
            )
        )
    if not output:
        raise PlayerDatasetError("No eligible positive-minute player targets")
    ordered = sorted(
        output,
        key=lambda item: (
            item.kickoff,
            item.fixture_id,
            item.team_id,
            item.player_id,
        ),
    )
    return ordered, {
        "eligibility_flag": "eligible_player_models",
        "source_code": source_code,
        "rows": len(ordered),
        "fixtures": len({row.fixture_id for row in ordered}),
        "players": len({row.player_id for row in ordered}),
        "kickoff_start": ordered[0].kickoff.isoformat(),
        "kickoff_end": ordered[-1].kickoff.isoformat(),
        "exclusions": dict(sorted(exclusions.items())),
        "missing_minutes_policy": "exclude_never_impute_zero",
    }


def load_first_valid_confirmed_lineups(
    connection,
    *,
    as_of: datetime,
    fixture_ids: set[str] | None = None,
) -> list[ConfirmedLineupFixture]:
    """Return the first strictly pre-kickoff, two-team, identity-safe lineup."""

    if as_of.tzinfo is None:
        raise PlayerDatasetError("as_of must be timezone-aware")
    parameters: list[object] = [as_of]
    fixture_filter = ""
    if fixture_ids is not None:
        if not fixture_ids:
            return []
        placeholders = ",".join("?" for _ in fixture_ids)
        fixture_filter = f" AND ls.fixture_id IN ({placeholders})"
        parameters.extend(sorted(fixture_ids))
    parameters.append(as_of)
    rows = connection.execute(
        f"""
        WITH team_shape AS (
            SELECT
                ls.fixture_id,
                ls.raw_artifact_id,
                ls.schedule_observation_id,
                ls.team_id,
                max(ls.retrieved_at) AS retrieved_at,
                count(*) FILTER (WHERE lp.selection_role='starter') AS starters,
                count(DISTINCT lp.player_id) FILTER (
                    WHERE lp.selection_role='starter'
                ) AS distinct_starters,
                count(*) FILTER (
                    WHERE lp.selection_role='starter'
                      AND coalesce(ids.is_identity_placeholder, false)
                ) AS unsafe_starters,
                bool_and(ls.is_complete) AS complete,
                bool_and(ls.captured_before_kickoff) AS captured_before_kickoff,
                bool_and(ls.identity_state='resolved') AS identities_resolved
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            LEFT JOIN player_identity_state ids USING (player_id)
            WHERE ls.lineup_type='confirmed'
              AND ls.raw_artifact_id IS NOT NULL
              AND ls.schedule_observation_id IS NOT NULL
              AND ls.retrieved_at <= ?
              {fixture_filter}
            GROUP BY ls.fixture_id, ls.raw_artifact_id,
                     ls.schedule_observation_id, ls.team_id
        ), candidate AS (
            SELECT
                t.fixture_id,
                t.raw_artifact_id,
                t.schedule_observation_id,
                max(t.retrieved_at) AS prediction_at,
                count(*) AS team_rows,
                count(DISTINCT t.team_id) AS represented_teams,
                bool_and(
                    t.complete AND t.captured_before_kickoff
                    AND t.identities_resolved AND t.starters=11
                    AND t.distinct_starters=11 AND t.unsafe_starters=0
                ) AS valid_shape
            FROM team_shape t
            GROUP BY t.fixture_id, t.raw_artifact_id,
                     t.schedule_observation_id
        ), valid AS (
            SELECT
                c.*,
                f.competition_id,
                f.season_id,
                f.home_team_id,
                f.away_team_id,
                f.scheduled_kickoff
            FROM candidate c
            JOIN fixture f USING (fixture_id)
            JOIN fixture_schedule_observation fso
              ON fso.schedule_observation_id=c.schedule_observation_id
             AND fso.fixture_id=c.fixture_id
            WHERE c.team_rows=2
              AND c.represented_teams=2
              AND c.valid_shape
              AND c.prediction_at < fso.scheduled_kickoff
              AND fso.scheduled_kickoff=f.scheduled_kickoff
              AND ? < f.scheduled_kickoff
        )
        SELECT * FROM valid
        ORDER BY fixture_id, prediction_at, raw_artifact_id
        """,
        parameters,
    ).fetchall()
    first: dict[str, tuple] = {}
    for row in rows:
        first.setdefault(str(row[0]), row)
    output = []
    for fixture_id, row in sorted(first.items(), key=lambda item: (item[1][11], item[0])):
        player_rows = connection.execute(
            """
            SELECT lp.player_id, ls.team_id, lp.selection_role,
                   coalesce(nullif(lp.position_code, ''), p.primary_position)
            FROM lineup_snapshot ls
            JOIN lineup_player lp USING (lineup_snapshot_id)
            JOIN player p USING (player_id)
            LEFT JOIN player_identity_state ids USING (player_id)
            WHERE ls.fixture_id=? AND ls.raw_artifact_id=?
              AND ls.schedule_observation_id=?
              AND NOT coalesce(ids.is_identity_placeholder, false)
            ORDER BY ls.team_id, lp.selection_role, lp.player_id
            """,
            [fixture_id, row[1], row[2]],
        ).fetchall()
        players = tuple(
            ConfirmedLineupPlayer(
                player_id=str(value[0]),
                team_id=str(value[1]),
                selection_role=str(value[2]),
                position_code=str(value[3] or ""),
            )
            for value in player_rows
        )
        starters = [item for item in players if item.selection_role == "starter"]
        if len(starters) != 22 or len({item.player_id for item in starters}) != 22:
            raise PlayerDatasetError(f"Confirmed lineup changed during read: {fixture_id}")
        if {item.team_id for item in starters} != {str(row[9]), str(row[10])}:
            raise PlayerDatasetError(f"Confirmed lineup team mismatch: {fixture_id}")
        output.append(
            ConfirmedLineupFixture(
                fixture_id=fixture_id,
                competition_id=str(row[7]),
                season_id=None if row[8] is None else str(row[8]),
                home_team_id=str(row[9]),
                away_team_id=str(row[10]),
                kickoff=row[11],
                prediction_at=row[3],
                raw_artifact_id=str(row[1]),
                schedule_observation_id=str(row[2]),
                players=players,
            )
        )
    return output


def player_target_rows_sha256(rows: list[PlayerMatchTarget]) -> str:
    digest = hashlib.sha256()
    for row in sorted(
        rows,
        key=lambda item: (item.kickoff, item.fixture_id, item.team_id, item.player_id),
    ):
        value = asdict(row)
        value["kickoff"] = row.kickoff.isoformat()
        value["result_available_at"] = row.result_available_at.isoformat()
        digest.update(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def write_player_target_artifact(
    rows: list[PlayerMatchTarget],
    audit: dict,
    *,
    output_dir: Path,
    warehouse_path: Path,
    source_files: dict[str, Path],
) -> dict:
    if not rows:
        raise PlayerDatasetError("Cannot freeze an empty player dataset")
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / "targets.parquet"
    temporary_parquet = output_dir / ".targets.parquet.tmp"
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".jsonl", dir=output_dir, delete=False
    )
    json_path = Path(handle.name)
    try:
        with handle:
            for row in rows:
                value = asdict(row)
                value["kickoff"] = row.kickoff.timestamp()
                value["result_available_at"] = row.result_available_at.timestamp()
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (
                        to_timestamp(kickoff) AS kickoff,
                        to_timestamp(result_available_at) AS result_available_at
                    )
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY kickoff, fixture_id, team_id, player_id
                ) TO {_sql_literal(temporary_parquet)}
                (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary_parquet, parquet_path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary_parquet.unlink(missing_ok=True)
    verification = duckdb.connect(":memory:")
    try:
        result = verification.execute(
            f"""SELECT count(*), count(DISTINCT fixture_id || '|' || team_id || '|' || player_id)
                 FROM read_parquet({_sql_literal(parquet_path)})"""
        ).fetchone()
    finally:
        verification.close()
    if result[0] != len(rows) or result[0] != result[1]:
        raise PlayerDatasetError("Frozen player artifact failed row verification")
    stat = warehouse_path.stat()
    manifest = {
        "artifact_version": "player_match_targets_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "eligibility_flag": "eligible_player_models",
        "target_semantics": "positive_exposure_only_missing_minutes_never_zero",
        "dataset": {
            "path": str(parquet_path.resolve()),
            "sha256": _file_sha256(parquet_path),
            "logical_rows_sha256": player_target_rows_sha256(rows),
            "rows": len(rows),
            "fixtures": len({row.fixture_id for row in rows}),
            "players": len({row.player_id for row in rows}),
            "columns": [item.name for item in fields(PlayerMatchTarget)],
        },
        "audit": audit,
        "warehouse_snapshot": {
            "path": str(warehouse_path.resolve()),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        },
        "source_files": {
            name: {"path": str(path.resolve()), "sha256": _file_sha256(path)}
            for name, path in sorted(source_files.items())
        },
    }
    _atomic_write_json(output_dir / "manifest.json", manifest)
    return manifest


def read_player_target_artifact(path: Path) -> list[PlayerMatchTarget]:
    connection = duckdb.connect(":memory:")
    try:
        relation = connection.execute(
            f"SELECT * FROM read_parquet({_sql_literal(path)}) ORDER BY kickoff, fixture_id, team_id, player_id"
        )
        names = [item[0] for item in relation.description]
        expected = [item.name for item in fields(PlayerMatchTarget)]
        if names != expected:
            raise PlayerDatasetError(
                f"Unexpected player target schema: expected={expected}, actual={names}"
            )
        output = []
        identifiers = {
            "fixture_id", "competition_id", "season_id", "team_id",
            "opponent_team_id", "player_id", "position_code",
        }
        for row in relation.fetchall():
            value = dict(zip(names, row, strict=True))
            for key in identifiers:
                if value[key] is not None:
                    value[key] = str(value[key])
            output.append(PlayerMatchTarget(**value))
        return output
    finally:
        connection.close()


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"
