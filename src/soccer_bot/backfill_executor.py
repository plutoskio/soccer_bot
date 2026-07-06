from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time
import uuid

from .coverage_audit import FINAL_STATUSES, audit_match
from .database import Warehouse, json_text, optional_int
from .http import HttpClient, HttpResponse
from .loaders import (
    RawCatalog,
    WarehouseLoader,
    api_player_identity_key,
    metadata_artifact_id,
    parse_api_passes,
    parse_datetime,
)
from .player_linking import deduplicate_api_lineup_entries
from .raw_store import RawArtifactStore


RESOURCE = "historical_backfill_batch"


class BackfillValidationError(RuntimeError):
    pass


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def unavailable_player_stats_shape(match: dict) -> str | None:
    """Identify absent or provider-placeholder fixture player statistics.

    Placeholder blocks contain player identities and lineup context but report
    zero/no minutes for everyone and no meaningful performance values.  They
    must not be stored as genuine zero-minute match performances.
    """
    blocks = match.get("players") or []
    if not blocks:
        return "absent_blocks"
    if len(blocks) != 2:
        return None
    records = [record for block in blocks for record in (block.get("players") or [])]
    if not records:
        return "empty_blocks"

    performance_paths = (
        ("games", "rating"),
        ("goals", "total"), ("goals", "assists"),
        ("shots", "total"), ("shots", "on"),
        ("passes", "total"), ("passes", "accuracy"), ("passes", "key"),
        ("tackles", "total"), ("tackles", "interceptions"),
        ("duels", "total"), ("duels", "won"),
        ("dribbles", "attempts"), ("dribbles", "success"),
    )
    positive_minutes = False
    meaningful_values = False
    for record in records:
        statistics = (record.get("statistics") or [{}])[0]
        minutes = (statistics.get("games") or {}).get("minutes")
        if minutes is not None and int(minutes) > 0:
            positive_minutes = True
        if any((statistics.get(section) or {}).get(field) is not None
               for section, field in performance_paths):
            meaningful_values = True
    if not positive_minutes and not meaningful_values:
        return "placeholder_zero_minutes"
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def batch_fingerprint(batch: dict) -> str:
    material = {
        "batch_id": batch["batch_id"],
        "league_id": int(batch["league_id"]),
        "season": int(batch["season"]),
        "fixture_ids": [int(value) for value in batch["fixture_ids"]],
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_batch_definition(batch: dict, *, maximum_size: int = 20) -> None:
    required = {"batch_id", "league_id", "season", "fixture_ids"}
    missing = required - set(batch)
    if missing:
        raise BackfillValidationError(f"Batch definition is missing: {sorted(missing)}")
    ids = batch["fixture_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= maximum_size:
        raise BackfillValidationError(
            f"Batch {batch['batch_id']} must contain 1-{maximum_size} fixture IDs"
        )
    normalized = [int(value) for value in ids]
    if len(normalized) != len(set(normalized)):
        raise BackfillValidationError(f"Batch {batch['batch_id']} contains duplicate IDs")
    expected_prefix = f"api-football-{int(batch['league_id'])}-{int(batch['season'])}-"
    if not str(batch["batch_id"]).startswith(expected_prefix):
        raise BackfillValidationError(
            f"Batch {batch['batch_id']} does not match its league and season"
        )


def validate_manifest(batches: list[dict], rows: list[dict], *, maximum_size: int = 20) -> dict[int, dict]:
    if not batches:
        raise BackfillValidationError("Backfill batch manifest is empty")
    row_by_id: dict[int, dict] = {}
    for row in rows:
        fixture_id = int(row["api_fixture_id"])
        if fixture_id in row_by_id:
            raise BackfillValidationError(f"Manifest repeats fixture {fixture_id}")
        row_by_id[fixture_id] = row

    requested: list[int] = []
    batch_ids: set[str] = set()
    for batch in batches:
        validate_batch_definition(batch, maximum_size=maximum_size)
        if batch["batch_id"] in batch_ids:
            raise BackfillValidationError(f"Duplicate batch ID: {batch['batch_id']}")
        batch_ids.add(batch["batch_id"])
        for value in batch["fixture_ids"]:
            fixture_id = int(value)
            row = row_by_id.get(fixture_id)
            if not row:
                raise BackfillValidationError(f"Batch references unknown fixture {fixture_id}")
            if row.get("action") not in {"REQUEST_API", "NEW_FIXTURE"}:
                raise BackfillValidationError(
                    f"Fixture {fixture_id} has non-requestable action {row.get('action')}"
                )
            if int(row["league_id"]) != int(batch["league_id"]) or int(row["season"]) != int(batch["season"]):
                raise BackfillValidationError(f"Fixture {fixture_id} is in the wrong batch")
            requested.append(fixture_id)
    if len(requested) != len(set(requested)):
        raise BackfillValidationError("A fixture is assigned to more than one batch")
    requestable = {
        fixture_id for fixture_id, row in row_by_id.items()
        if row.get("action") in {"REQUEST_API", "NEW_FIXTURE"}
    }
    if set(requested) != requestable:
        missing = sorted(requestable - set(requested))
        raise BackfillValidationError(
            f"Batch manifest omits {len(missing)} requestable fixtures"
        )
    return row_by_id


class HistoricalBackfillExecutor:
    API_URL = "https://v3.football.api-sports.io"

    def __init__(
        self,
        *,
        warehouse: Warehouse,
        raw_store: RawArtifactStore,
        http_client: HttpClient,
        api_key: str,
        config: dict,
        batches: list[dict],
        manifest_rows: list[dict],
        manifest_sha256: str,
    ) -> None:
        if not api_key:
            raise ValueError("API_FOOTBALL_KEY is missing")
        self.warehouse = warehouse
        self.connection = warehouse.connection
        self.raw_store = raw_store
        self.http = http_client
        self.api_key = api_key
        self.config = config
        self.api_config = config["api_football"]
        self.validation_config = config["validation"]
        self.batches = batches
        self.row_by_id = validate_manifest(
            batches, manifest_rows,
            maximum_size=int(self.api_config["fixture_batch_size"]),
        )
        self.manifest_sha256 = manifest_sha256
        self.loader = WarehouseLoader(warehouse, RawCatalog.__new__(RawCatalog))
        self.loader.enable_api_backfill_identity_cache()
        self.api_calls = 0
        self.cache_hits = 0
        self.last_request_at: float | None = None

    def run(
        self,
        *,
        maximum_batches: int,
        execute: bool,
        batch_id: str | None = None,
        retry_failed: bool = False,
    ) -> dict:
        if maximum_batches <= 0:
            raise ValueError("maximum_batches must be positive")
        selected = self._select_batches(batch_id=batch_id, retry_failed=retry_failed)
        selected = selected[:maximum_batches]
        summary = {
            "mode": "execute" if execute else "dry_run",
            "manifest_sha256": self.manifest_sha256,
            "eligible_batches": len(self._select_batches(batch_id=batch_id, retry_failed=retry_failed)),
            "selected_batches": [batch["batch_id"] for batch in selected],
            "attempted_batches": 0,
            "completed_batches": 0,
            "api_calls": 0,
            "cache_hits": 0,
            "global_quality_audits": 0,
        }
        if not execute:
            return summary

        run_id = str(uuid.uuid4())
        run_started_monotonic = time.monotonic()
        total_batches = len(selected)
        total_fixtures = sum(len(batch["fixture_ids"]) for batch in selected)
        completed_fixtures = 0
        last_quality_audit_index = -1
        print(
            f"Starting historical backfill: {total_batches:,} batches, "
            f"{total_fixtures:,} fixture requests selected.",
            flush=True,
        )
        now = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO historical_backfill_run (
                run_id, manifest_sha256, started_at, status, dry_run, maximum_batches
            ) VALUES (?, ?, ?, 'running', false, ?)
            """,
            [run_id, self.manifest_sha256, now, maximum_batches],
        )
        try:
            for index, batch in enumerate(selected, 1):
                summary["attempted_batches"] += 1
                try:
                    self._execute_batch(batch, run_id)
                except Exception as error:
                    elapsed = time.monotonic() - run_started_monotonic
                    print(
                        f"FAILED batch {index:,}/{total_batches:,} "
                        f"({batch['batch_id']}) after {format_duration(elapsed)}: "
                        f"{type(error).__name__}: {error}",
                        flush=True,
                    )
                    raise
                summary["completed_batches"] += 1
                completed_fixtures += len(batch["fixture_ids"])
                if index % 25 == 0:
                    self._run_global_quality_audit(index, total_batches)
                    summary["global_quality_audits"] += 1
                    last_quality_audit_index = index
                elapsed = time.monotonic() - run_started_monotonic
                average = elapsed / index
                eta = average * (total_batches - index)
                print(
                    f"Progress {index:,}/{total_batches:,} batches "
                    f"({100 * index / total_batches:.1f}%) | "
                    f"fixtures {completed_fixtures:,}/{total_fixtures:,} | "
                    f"API calls {self.api_calls:,} | cache hits {self.cache_hits:,} | "
                    f"elapsed {format_duration(elapsed)} | ETA {format_duration(eta)} | "
                    f"last {batch['batch_id']}",
                    flush=True,
                )
            if last_quality_audit_index != total_batches:
                self._run_global_quality_audit(total_batches, total_batches)
                summary["global_quality_audits"] += 1
            summary["api_calls"] = self.api_calls
            summary["cache_hits"] = self.cache_hits
            self.connection.execute(
                """
                UPDATE historical_backfill_run SET finished_at=?, status='completed',
                    batches_attempted=?, api_calls=?, cache_hits=?, summary=?
                WHERE run_id=?
                """,
                [datetime.now(timezone.utc), summary["attempted_batches"], self.api_calls,
                 self.cache_hits, json_text(summary), run_id],
            )
            return summary
        except Exception as error:
            summary["api_calls"] = self.api_calls
            summary["cache_hits"] = self.cache_hits
            self.connection.execute(
                """
                UPDATE historical_backfill_run SET finished_at=?, status='failed',
                    batches_attempted=?, api_calls=?, cache_hits=?, summary=?, error_message=?
                WHERE run_id=?
                """,
                [datetime.now(timezone.utc), summary["attempted_batches"], self.api_calls,
                 self.cache_hits, json_text(summary), f"{type(error).__name__}: {error}", run_id],
            )
            raise

    def _run_global_quality_audit(self, completed: int, total: int) -> None:
        print(
            f"Running global warehouse quality audit after "
            f"{completed:,}/{total:,} batches...",
            flush=True,
        )
        from scripts.build_database import run_quality_checks
        run_quality_checks(
            self.warehouse,
            passing_coverage_warning_threshold=float(
                self.validation_config["passing_coverage_warning_threshold"]
            ),
        )
        blocking = self.connection.execute(
            """SELECT count(*) FROM data_quality_issue
               WHERE status='open' AND severity='blocking'"""
        ).fetchone()[0]
        if blocking:
            raise BackfillValidationError(
                f"Warehouse has {blocking} blocking quality issues after global audit"
            )
        print("Global warehouse quality audit passed.", flush=True)

    def _select_batches(self, *, batch_id: str | None, retry_failed: bool) -> list[dict]:
        selected = []
        for batch in self.batches:
            if batch_id and batch["batch_id"] != batch_id:
                continue
            checkpoint = self.connection.execute(
                """
                SELECT batch_fingerprint, status
                FROM historical_backfill_batch_checkpoint
                WHERE manifest_sha256=? AND batch_id=?
                """,
                [self.manifest_sha256, batch["batch_id"]],
            ).fetchone()
            fingerprint = batch_fingerprint(batch)
            if checkpoint and checkpoint[0] != fingerprint:
                raise BackfillValidationError(
                    f"Checkpoint fingerprint mismatch for {batch['batch_id']}"
                )
            if checkpoint and checkpoint[1] == "succeeded":
                continue
            if checkpoint and checkpoint[1] == "failed" and not retry_failed:
                continue
            selected.append(batch)
        if batch_id and not any(batch["batch_id"] == batch_id for batch in self.batches):
            raise BackfillValidationError(f"Unknown batch ID: {batch_id}")
        return selected

    def _execute_batch(self, batch: dict, run_id: str) -> None:
        fixture_ids = [int(value) for value in batch["fixture_ids"]]
        params = {"ids": "-".join(str(value) for value in fixture_ids)}
        started_at = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO historical_backfill_batch_checkpoint (
                manifest_sha256, batch_id, batch_fingerprint, league_id, season,
                fixture_ids, status, attempts, last_run_id, started_at,
                requested_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', 1, ?, ?, ?, ?)
            ON CONFLICT (manifest_sha256, batch_id) DO UPDATE SET
                status='running', attempts=historical_backfill_batch_checkpoint.attempts+1,
                last_run_id=excluded.last_run_id, started_at=excluded.started_at,
                completed_at=NULL, last_error=NULL, validation=NULL,
                requested_count=excluded.requested_count, updated_at=excluded.updated_at
            """,
            [self.manifest_sha256, batch["batch_id"], batch_fingerprint(batch),
             int(batch["league_id"]), int(batch["season"]), json_text(fixture_ids),
             run_id, started_at, len(fixture_ids), started_at],
        )

        item = None
        response_status = None
        try:
            cached = self._cached_artifact(params)
            if cached:
                payload, item = cached
                response_status = int(item["http_status"])
                self.cache_hits += 1
            else:
                self._enforce_budget()
                response = self._request(params)
                response_status = response.status
                item = self._store_item(response, params)
                payload = self._validated_http_payload(response)

            raw_validation = self._validate_response(payload, batch)
            unavailable_fixture_ids = {
                int(state["fixture_id"])
                for state in raw_validation["fixtures"]
                if state.get("player_stats_unavailable")
            }
            administrative_fixture_ids = {
                int(state["fixture_id"])
                for state in raw_validation["fixtures"]
                if state.get("administrative_result_unplayed")
            }
            load_payload = payload
            if unavailable_fixture_ids or administrative_fixture_ids:
                # Preserve the immutable raw response, but prevent absent or
                # placeholder player blocks from becoming false observations.
                load_payload = {
                    **payload,
                    "response": [
                        (
                            {
                                **match,
                                "players": [], "lineups": [], "events": [],
                                "statistics": [],
                                "_administrative_result_unplayed": True,
                            }
                            if int((match.get("fixture") or {}).get("id"))
                               in administrative_fixture_ids
                            else (
                                {**match, "players": []}
                                if int((match.get("fixture") or {}).get("id"))
                                   in unavailable_fixture_ids
                                else match
                            )
                        )
                        for match in payload["response"]
                    ],
                }
            raw_artifact_id = item["_raw_artifact_id"]
            with self.warehouse.transaction():
                RawCatalog.register_item(self.warehouse, item)
                self.loader.load_api_football_payload(load_payload, item, RESOURCE)
                self.warehouse.reconcile_team_aliases()
                relational_validation = self._validate_loaded_batch(
                    batch, raw_artifact_id, load_payload
                )
                validation = {"raw": raw_validation, "relational": relational_validation}
                completed_at = datetime.now(timezone.utc)
                self.connection.execute(
                    """
                    UPDATE historical_backfill_batch_checkpoint SET status='succeeded',
                        completed_at=?, last_http_status=?, raw_artifact_id=?,
                        returned_count=?, validated_count=?, validation=?,
                        last_error=NULL, updated_at=?
                    WHERE manifest_sha256=? AND batch_id=?
                    """,
                    [completed_at, response_status, raw_artifact_id,
                     raw_validation["returned_count"], relational_validation["validated_count"],
                     json_text(validation), completed_at, self.manifest_sha256, batch["batch_id"]],
                )
        except Exception as error:
            if item is not None:
                RawCatalog.register_item(self.warehouse, item)
            failed_at = datetime.now(timezone.utc)
            self.connection.execute(
                """
                UPDATE historical_backfill_batch_checkpoint SET status='failed',
                    completed_at=?, last_http_status=?, raw_artifact_id=?,
                    last_error=?, updated_at=?
                WHERE manifest_sha256=? AND batch_id=?
                """,
                [failed_at, response_status,
                 item.get("_raw_artifact_id") if item else None,
                 f"{type(error).__name__}: {error}", failed_at,
                 self.manifest_sha256, batch["batch_id"]],
            )
            raise

    def _validate_response(self, payload: dict, batch: dict) -> dict:
        if not isinstance(payload, dict):
            raise BackfillValidationError("API response is not an object")
        if payload.get("errors"):
            raise BackfillValidationError(f"API response contains errors: {payload['errors']}")
        response = payload.get("response")
        if not isinstance(response, list):
            raise BackfillValidationError("API response field is not a list")
        expected_ids = [int(value) for value in batch["fixture_ids"]]
        returned_ids = [
            int(match.get("fixture", {}).get("id"))
            for match in response if isinstance(match, dict) and match.get("fixture", {}).get("id") is not None
        ]
        duplicates = sorted(value for value in set(returned_ids) if returned_ids.count(value) > 1)
        missing = sorted(set(expected_ids) - set(returned_ids))
        unexpected = sorted(set(returned_ids) - set(expected_ids))
        if duplicates or missing or unexpected or len(returned_ids) != len(response):
            raise BackfillValidationError(
                f"Response identity mismatch: missing={missing}, unexpected={unexpected}, duplicates={duplicates}"
            )

        audits = []
        failures = []
        for match in response:
            fixture_id = int(match["fixture"]["id"])
            expectation = self.row_by_id[fixture_id]
            league = match.get("league") or {}
            teams = match.get("teams") or {}
            fixture = match.get("fixture") or {}
            identity_failures = []
            if int(league.get("id") or -1) != int(batch["league_id"]):
                identity_failures.append("league")
            if int(league.get("season") or -1) != int(batch["season"]):
                identity_failures.append("season")
            if int((teams.get("home") or {}).get("id") or -1) != int(expectation["home_api_team_id"]):
                identity_failures.append("home_team")
            if int((teams.get("away") or {}).get("id") or -1) != int(expectation["away_api_team_id"]):
                identity_failures.append("away_team")
            expected_kickoff = parse_datetime(expectation.get("kickoff"))
            actual_kickoff = parse_datetime(fixture.get("date"))
            tolerance = int(self.validation_config["kickoff_tolerance_seconds"])
            if (
                not expected_kickoff or not actual_kickoff
                or abs((actual_kickoff - expected_kickoff).total_seconds()) > tolerance
            ):
                identity_failures.append("kickoff")
            if fixture.get("status", {}).get("short") not in FINAL_STATUSES:
                identity_failures.append("final_status")
            # Passing fields are useful but optional provider data. Preserve
            # structurally valid fixtures and let the warehouse quality checks
            # record sub-threshold passing coverage as a warning.
            audit = audit_match(match, minimum_passing_coverage=None)
            audit_data = asdict(audit)
            audit_data["identity_failures"] = identity_failures
            lineup_duplicate_entries = []
            unrecoverable_lineup_duplicates = False
            for lineup_team in match.get("lineups") or []:
                _, duplicates, unrecoverable = deduplicate_api_lineup_entries(
                    lineup_team
                )
                lineup_duplicate_entries.extend(duplicates)
                unrecoverable_lineup_duplicates = bool(
                    unrecoverable_lineup_duplicates or unrecoverable
                )
            audit_data["lineup_duplicate_entries"] = lineup_duplicate_entries
            audit_data["unrecoverable_lineup_duplicates"] = (
                unrecoverable_lineup_duplicates
            )
            administrative_exception = (
                self.validation_config.get("administrative_result_fixtures", {})
                .get(str(fixture_id))
            )
            # Preserve structurally sound finished fixtures when an entire
            # optional statistics section is unavailable.  A partially
            # populated/malformed section is still rejected.
            unavailable_shape = unavailable_player_stats_shape(match)
            player_stats_unavailable = bool(
                unavailable_shape is not None
                and audit.result
                and audit.lineups
            )
            team_stat_blocks = match.get("statistics") or []
            team_stats_unavailable = bool(
                len(team_stat_blocks) == 0 and audit.result and audit.lineups
            )
            player_component_valid = bool(
                (audit.player_blocks and audit.minutes and audit.core_player_fields)
                or player_stats_unavailable
            )
            team_component_valid = bool(
                audit.team_statistics or team_stats_unavailable
            )
            accepted_partial = bool(
                not audit.complete
                and audit.result
                and audit.lineups
                and not unrecoverable_lineup_duplicates
                and player_component_valid
                and team_component_valid
            )
            administrative_result_unplayed = bool(
                administrative_exception
                and administrative_exception.get("classification")
                    == "administrative_result_unplayed"
                and audit.result
                and not (match.get("events") or [])
                and not audit.lineups
                and unavailable_shape is not None
            )
            audit_data["player_stats_unavailable"] = player_stats_unavailable
            audit_data["player_stats_unavailable_reason"] = (
                unavailable_shape if player_stats_unavailable else None
            )
            audit_data["team_stats_unavailable"] = team_stats_unavailable
            audit_data["team_stats_unavailable_reason"] = (
                "absent_blocks" if team_stats_unavailable else None
            )
            audit_data["accepted_partial"] = accepted_partial
            audit_data["administrative_result_unplayed"] = (
                administrative_result_unplayed
            )
            audit_data["administrative_result_reason"] = (
                administrative_exception.get("reason")
                if administrative_result_unplayed else None
            )
            audits.append(audit_data)
            raw_complete = bool(
                audit.complete and not unrecoverable_lineup_duplicates
            )
            if identity_failures or not (
                raw_complete or accepted_partial or administrative_result_unplayed
            ):
                failures.append({"fixture_id": fixture_id, **audit_data})
        if failures:
            raise BackfillValidationError(
                "Raw fixture validation failed: " + json.dumps(failures, sort_keys=True)
            )
        return {"returned_count": len(response), "fixtures": audits}

    def _validate_loaded_batch(
        self, batch: dict, raw_artifact_id: str, payload: dict
    ) -> dict:
        validations = []
        failures = []
        minimum_players = int(self.validation_config["minimum_participating_players"])
        raw_matches = {
            int(match["fixture"]["id"]): match for match in payload["response"]
        }
        for api_fixture_id in [int(value) for value in batch["fixture_ids"]]:
            raw_match = raw_matches[api_fixture_id]
            administrative_result_unplayed = bool(
                raw_match.get("_administrative_result_unplayed")
            )
            mapping = self.connection.execute(
                """
                SELECT internal_entity_id FROM source_entity_map
                WHERE source_code='api_football' AND entity_type='fixture' AND source_entity_id=?
                """,
                [str(api_fixture_id)],
            ).fetchall()
            if len(mapping) != 1:
                failures.append({"fixture_id": api_fixture_id, "reason": "fixture_mapping_count", "value": len(mapping)})
                continue
            fixture_id = mapping[0][0]
            fixture_row = self.connection.execute(
                "SELECT home_team_id, away_team_id FROM fixture WHERE fixture_id=?",
                [fixture_id],
            ).fetchone()
            if not fixture_row:
                failures.append({"fixture_id": api_fixture_id, "reason": "fixture_row_missing"})
                continue
            result_rows = self.connection.execute(
                """SELECT home_score_regulation, away_score_regulation
                   FROM fixture_result_observation
                   WHERE fixture_id=? AND source_code='api_football' AND raw_artifact_id=?
                     AND home_score_regulation IS NOT NULL AND away_score_regulation IS NOT NULL""",
                [fixture_id, raw_artifact_id],
            ).fetchall()
            raw_score = raw_match.get("score", {}).get("fulltime", {})
            expected_score = (
                optional_int(raw_score.get("home")), optional_int(raw_score.get("away"))
            )
            score_matches = len(result_rows) == 1 and tuple(result_rows[0]) == expected_score

            raw_teams = raw_match.get("teams") or {}
            source_team_ids = [
                str((raw_teams.get(side) or {}).get("id")) for side in ("home", "away")
            ]
            mapped_teams = []
            for source_team_id in source_team_ids:
                row = self.connection.execute(
                    """
                    SELECT internal_entity_id FROM source_entity_map
                    WHERE source_code='api_football' AND entity_type='team'
                      AND source_entity_id=?
                    """,
                    [source_team_id],
                ).fetchone()
                mapped_teams.append(row[0] if row else None)
            team_identity_matches = tuple(mapped_teams) == tuple(fixture_row)
            lineup_rows = self.connection.execute(
                """
                SELECT s.team_id, s.is_complete,
                       count(*) FILTER (WHERE p.selection_role='starter')
                FROM lineup_snapshot s
                LEFT JOIN lineup_player p USING (lineup_snapshot_id)
                WHERE s.fixture_id=? AND s.source_code='api_football' AND s.raw_artifact_id=?
                GROUP BY s.team_id, s.is_complete
                """,
                [fixture_id, raw_artifact_id],
            ).fetchall()
            team_stat_rows = self.connection.execute(
                """SELECT count(DISTINCT team_id), count(*),
                          count(*) FILTER (WHERE shots<0 OR shots_on_target<0
                                            OR corners<0 OR fouls<0)
                   FROM team_match_stat_observation
                   WHERE fixture_id=? AND source_code='api_football' AND raw_artifact_id=?""",
                [fixture_id, raw_artifact_id],
            ).fetchone()
            player_row = self.connection.execute(
                """
                SELECT count(*), count(DISTINCT player_id),
                       count(*) FILTER (WHERE minutes_played>0),
                       count(*) FILTER (WHERE minutes_played>0 AND passes IS NOT NULL
                                         AND accurate_passes IS NOT NULL),
                       count(*) FILTER (WHERE team_id NOT IN (?, ?)),
                       count(*) FILTER (WHERE accurate_passes>passes OR pass_accuracy_pct<0
                                         OR pass_accuracy_pct>100 OR minutes_played<0
                                         OR minutes_played>130)
                FROM player_match_stat_observation
                WHERE fixture_id=? AND source_code='api_football' AND raw_artifact_id=?
                """,
                [fixture_row[0], fixture_row[1], fixture_id, raw_artifact_id],
            ).fetchone()
            participating_not_in_lineup = self.connection.execute(
                """
                SELECT count(*)
                FROM player_match_stat_observation pm
                WHERE pm.fixture_id=? AND pm.source_code='api_football'
                  AND pm.raw_artifact_id=? AND pm.minutes_played>0
                  AND NOT EXISTS (
                      SELECT 1 FROM lineup_snapshot ls
                      JOIN lineup_player lp USING (lineup_snapshot_id)
                      WHERE ls.fixture_id=pm.fixture_id AND ls.team_id=pm.team_id
                        AND ls.source_code='api_football' AND ls.raw_artifact_id=?
                        AND lp.player_id=pm.player_id
                  )
                """,
                [fixture_id, raw_artifact_id, raw_artifact_id],
            ).fetchone()[0]
            participants = player_row[2]
            passing_coverage = player_row[3] / participants if participants else 0.0
            player_stats_expected = bool(raw_match.get("players") or [])
            team_stats_expected = bool(raw_match.get("statistics") or [])
            player_value_mismatches = self._player_value_mismatches(
                raw_match, fixture_id, raw_artifact_id
            )
            state = {
                "fixture_id": api_fixture_id,
                "administrative_result_unplayed": administrative_result_unplayed,
                "result_rows": len(result_rows),
                "score_matches_raw": score_matches,
                "team_identity_matches_raw": team_identity_matches,
                "home_away_teams_distinct": fixture_row[0] != fixture_row[1],
                "lineup_teams": len(lineup_rows),
                "starter_counts": sorted(row[2] for row in lineup_rows),
                "team_stat_teams": team_stat_rows[0],
                "team_stat_rows": team_stat_rows[1],
                "invalid_team_stat_rows": team_stat_rows[2],
                "team_stats_expected": team_stats_expected,
                "team_stats_unavailable": not team_stats_expected,
                "player_rows": player_row[0],
                "distinct_players": player_row[1],
                "participating_players": participants,
                "player_stats_expected": player_stats_expected,
                "player_stats_unavailable": not player_stats_expected,
                "passing_coverage": passing_coverage,
                "wrong_team_rows": player_row[4],
                "invalid_player_rows": player_row[5],
                "participating_players_not_in_lineup": participating_not_in_lineup,
                "player_value_mismatches": player_value_mismatches,
            }
            validations.append(state)
            player_data_valid = (
                (
                    player_stats_expected
                    and player_row[0] == player_row[1]
                    and participants >= minimum_players
                    and player_row[4] == 0 and player_row[5] == 0
                    and not player_value_mismatches
                )
                or (
                    not player_stats_expected
                    and player_row[0] == 0 and participants == 0
                    and player_row[4] == 0 and player_row[5] == 0
                    and not player_value_mismatches
                )
            )
            team_data_valid = (
                (
                    team_stats_expected
                    and team_stat_rows[0] == 2 and team_stat_rows[1] == 2
                    and team_stat_rows[2] == 0
                )
                or (
                    not team_stats_expected
                    and team_stat_rows[0] == 0 and team_stat_rows[1] == 0
                    and team_stat_rows[2] == 0
                )
            )
            if administrative_result_unplayed:
                valid = bool(
                    len(result_rows) == 1 and score_matches
                    and team_identity_matches
                    and fixture_row[0] != fixture_row[1]
                    and len(lineup_rows) == 0
                    and team_stat_rows[0] == 0 and team_stat_rows[1] == 0
                    and player_row[0] == 0 and participants == 0
                    and not player_value_mismatches
                )
            else:
                valid = (
                    len(result_rows) == 1 and score_matches
                    and team_identity_matches
                    and fixture_row[0] != fixture_row[1]
                    and len(lineup_rows) == 2
                    and all(bool(row[1]) and row[2] == 11 for row in lineup_rows)
                    and team_data_valid
                    and player_data_valid
                )
            if not valid:
                failures.append(state)
        if failures:
            raise BackfillValidationError(
                "Relational fixture validation failed: " + json.dumps(failures, sort_keys=True)
            )
        return {"validated_count": len(validations), "fixtures": validations}

    def _player_value_mismatches(
        self, raw_match: dict, fixture_id: str, raw_artifact_id: str
    ) -> list[dict]:
        expected_by_internal_id = {}
        identity_collisions = []
        for team_block in raw_match.get("players") or []:
            api_team_id = str((team_block.get("team") or {}).get("id"))
            team_mapping = self.connection.execute(
                """
                SELECT internal_entity_id FROM source_entity_map
                WHERE source_code='api_football' AND entity_type='team'
                  AND source_entity_id=?
                """,
                [api_team_id],
            ).fetchone()
            for record in team_block.get("players") or []:
                player = record.get("player") or {}
                api_player_id = str(player.get("id"))
                player_mapping = self.connection.execute(
                    """
                    SELECT internal_entity_id FROM source_entity_map
                    WHERE source_code='api_football' AND entity_type='player'
                      AND source_entity_id=?
                    """,
                    [api_player_identity_key(api_player_id, player.get("name", "Unknown"))],
                ).fetchone()
                if not player_mapping or not team_mapping:
                    identity_collisions.append({
                        "player_id": api_player_id,
                        "reason": "source_identity_mapping_missing",
                    })
                    continue
                internal_player_id = player_mapping[0]
                statistics = (record.get("statistics") or [{}])[0]
                games = statistics.get("games") or {}
                goals = statistics.get("goals") or {}
                passes = statistics.get("passes") or {}
                total_passes, accurate_passes, pass_accuracy_pct = parse_api_passes(passes)
                values = {
                    "team_id": team_mapping[0],
                    "minutes": optional_int(games.get("minutes")),
                    "goals": optional_int(goals.get("total")) or 0,
                    "assists": optional_int(goals.get("assists")) or 0,
                    "passes": total_passes,
                    "accurate_passes": accurate_passes,
                    "pass_accuracy_pct": pass_accuracy_pct,
                }
                if internal_player_id in expected_by_internal_id:
                    identity_collisions.append({
                        "player_id": api_player_id,
                        "reason": "multiple_source_players_share_internal_id_in_fixture",
                    })
                expected_by_internal_id[internal_player_id] = (api_player_id, values)

        actual_rows = self.connection.execute(
            """
            SELECT pm.player_id, pm.team_id, pm.minutes_played, pm.goals, pm.assists,
                   pm.passes, pm.accurate_passes, pm.pass_accuracy_pct
            FROM player_match_stat_observation pm
            WHERE pm.fixture_id=? AND pm.source_code='api_football'
              AND pm.raw_artifact_id=?
            """,
            [fixture_id, raw_artifact_id],
        ).fetchall()
        actual = {
            row[0]: {
                "team_id": row[1],
                "minutes": row[2], "goals": row[3], "assists": row[4],
                "passes": row[5], "accurate_passes": row[6],
                "pass_accuracy_pct": row[7],
            }
            for row in actual_rows
        }
        mismatches = list(identity_collisions)
        for internal_player_id in sorted(set(expected_by_internal_id) | set(actual)):
            expected_record = expected_by_internal_id.get(internal_player_id)
            expected_values = expected_record[1] if expected_record else None
            api_player_id = expected_record[0] if expected_record else None
            actual_values = actual.get(internal_player_id)
            if expected_values is None or actual_values is None:
                mismatches.append({
                    "player_id": api_player_id,
                    "internal_player_id": internal_player_id,
                    "reason": "missing_player_row",
                    "expected": expected_values is not None,
                    "actual": actual_values is not None,
                })
                continue
            unequal = []
            for field, expected_value in expected_values.items():
                actual_value = actual_values[field]
                if field == "pass_accuracy_pct" and expected_value is not None and actual_value is not None:
                    equal = abs(float(expected_value) - float(actual_value)) < 1e-9
                else:
                    equal = expected_value == actual_value
                if not equal:
                    unequal.append(field)
            if unequal:
                mismatches.append({"player_id": api_player_id, "fields": unequal})
        return mismatches

    def _enforce_budget(self) -> None:
        today = datetime.now(timezone.utc).date()
        used = self.connection.execute(
            """
            SELECT count(*) FROM raw_artifact
            WHERE source_code='api_football' AND CAST(retrieved_at AS DATE)=?
              AND resource_name != 'status'
            """,
            [today],
        ).fetchone()[0]
        usable = int(self.api_config["daily_limit"]) - int(self.api_config["reserve_calls"])
        if used >= usable:
            raise RuntimeError(
                f"API-Football daily reserve reached: observed={used}, usable={usable}"
            )

    def _request(self, params: dict[str, object]) -> HttpResponse:
        if self.last_request_at is not None:
            wait = float(self.api_config["minimum_interval_seconds"]) - (
                time.monotonic() - self.last_request_at
            )
            if wait > 0:
                time.sleep(wait)
        response = self.http.get(
            self.API_URL, "/fixtures", params=params,
            headers={"x-apisports-key": self.api_key},
            timeout=float(self.api_config["request_timeout_seconds"]),
        )
        self.last_request_at = time.monotonic()
        self.api_calls += 1
        return response

    @staticmethod
    def _validated_http_payload(response: HttpResponse) -> dict:
        if response.status != 200:
            raise RuntimeError(f"API-Football returned HTTP {response.status}")
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("API-Football returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("API-Football returned a non-object response")
        if payload.get("errors"):
            raise RuntimeError(f"API-Football returned errors: {payload['errors']}")
        return payload

    def _store_item(self, response: HttpResponse, params: dict[str, object]) -> dict:
        artifact = self.raw_store.store(
            source="api_football", resource=RESOURCE,
            response=response, request_params=params,
        )
        item = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
        item["_metadata_path"] = artifact.metadata_path
        item["_raw_artifact_id"] = metadata_artifact_id(artifact.metadata_path)
        return item

    def _cached_artifact(self, params: dict[str, object]) -> tuple[dict, dict] | None:
        ids = str(params["ids"])
        row = self.connection.execute(
            """
            SELECT metadata_path FROM raw_artifact
            WHERE source_code='api_football' AND resource_name=? AND http_status=200
              AND json_extract_string(request_parameters, '$.ids')=?
            ORDER BY retrieved_at DESC LIMIT 1
            """,
            [RESOURCE, ids],
        ).fetchone()
        metadata_path = Path(row[0]) if row else self._unregistered_cached_metadata(ids)
        if metadata_path is None:
            return None
        if not metadata_path.exists():
            return None
        item = json.loads(metadata_path.read_text(encoding="utf-8"))
        item["_metadata_path"] = metadata_path
        item["_raw_artifact_id"] = metadata_artifact_id(metadata_path)
        with gzip.open(item["data_path"], "rb") as handle:
            body = handle.read()
        if str((item.get("response_headers") or {}).get("content-encoding", "")).lower() == "gzip":
            body = gzip.decompress(body)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload, item

    def _unregistered_cached_metadata(self, ids: str) -> Path | None:
        candidates = []
        resource_root = self.raw_store.root / "api_football" / RESOURCE
        for path in resource_root.rglob("*.meta.json") if resource_root.exists() else []:
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                metadata.get("http_status") == 200
                and str((metadata.get("request_parameters") or {}).get("ids")) == ids
            ):
                candidates.append((str(metadata.get("retrieved_at") or ""), path))
        return max(candidates)[1] if candidates else None
