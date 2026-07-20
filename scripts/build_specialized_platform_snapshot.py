#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from apps.api.snapshot_store import validate_snapshot
from soccer_bot.contracts import ScoreGrid
from soccer_bot.datasets.corner_features import (
    ChronologicalCornerFeatureBuilder,
    load_corner_feature_config,
)
from soccer_bot.datasets.corners import build_corner_targets
from soccer_bot.datasets.features import RegulationInferenceFixture
from soccer_bot.modeling.corners import (
    corner_model_sha256,
    corner_score_grid,
    load_corner_model,
    load_corner_model_config,
)
from soccer_bot.modeling.family_registry import load_specialized_family_registry
from soccer_bot.modeling.platform_markets import (
    corner_family_markets,
    first_team_markets,
    moneyline_markets,
    score_family_markets,
)
from soccer_bot.modeling.score_specialist import (
    load_score_specialist_config,
    load_score_specialist_model,
    score_specialist_sha256,
    specialist_score_grid,
)
from soccer_bot.modeling.timing import (
    first_score_model_sha256,
    first_team_probabilities,
    load_first_score_config,
    load_first_score_model,
)
from soccer_bot.platform_market_quotes import attach_polymarket_quotes
from soccer_bot.polymarket_contracts import load_polymarket_contract_policy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compose the fixture-first specialized-family research snapshot "
            "from immutable model artifacts."
        )
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=ROOT / "data" / "warehouse" / "soccer.duckdb",
    )
    parser.add_argument(
        "--moneyline-snapshot",
        type=Path,
        default=(
            ROOT
            / "data"
            / "predictions"
            / "regulation_champion_v1"
            / "latest.json"
        ),
    )
    parser.add_argument(
        "--score-model",
        type=Path,
        default=ROOT / "artifacts/research/regulation_score_specialist_v1/model.json",
    )
    parser.add_argument(
        "--score-config",
        type=Path,
        default=ROOT / "config/models/regulation_score_specialist_v1.json",
    )
    parser.add_argument(
        "--corner-model",
        type=Path,
        default=ROOT / "artifacts/research/joint_corners_v1/model.json",
    )
    parser.add_argument(
        "--corner-selection",
        type=Path,
        default=ROOT / "config/models/joint_corners_v1_forward_shadow.json",
    )
    parser.add_argument(
        "--corner-config",
        type=Path,
        default=ROOT / "config/models/joint_corners_v1.json",
    )
    parser.add_argument(
        "--timing-model",
        type=Path,
        default=ROOT / "artifacts/research/first_score_timing_v1/model.json",
    )
    parser.add_argument(
        "--timing-config",
        type=Path,
        default=ROOT / "config/models/first_score_timing_v1.json",
    )
    parser.add_argument(
        "--family-registry",
        type=Path,
        default=ROOT / "config/models/specialized_family_registry_v1.json",
    )
    parser.add_argument(
        "--polymarket-policy",
        type=Path,
        default=ROOT / "config/contracts/polymarket_regulation_v1.json",
    )
    parser.add_argument(
        "--live-market-max-age-minutes",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data/predictions/specialized_platform_v1",
    )
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="Write only latest.json; immutable forward evidence is stored separately.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    moneyline_snapshot = json.loads(args.moneyline_snapshot.read_text(encoding="utf-8"))
    validate_snapshot(moneyline_snapshot)
    source_rows = moneyline_snapshot["predictions"]
    created_at = datetime.now(timezone.utc)

    score_config = load_score_specialist_config(args.score_config)
    score_model = load_score_specialist_model(args.score_model)
    corner_config = load_corner_model_config(args.corner_config)
    corner_feature_config = load_corner_feature_config(args.corner_config)
    corner_model = load_corner_model(args.corner_model)
    timing_config = load_first_score_config(args.timing_config)
    timing_model = load_first_score_model(args.timing_model)
    family_registry = load_specialized_family_registry(args.family_registry)
    _require_registered_artifact(
        family_registry,
        family_key="regulation_moneyline",
        model_version=moneyline_snapshot["model_version"],
        logical_sha256=moneyline_snapshot["logical_model_sha256"],
    )
    _require_registered_artifact(
        family_registry,
        family_key="regulation_score",
        model_version=score_model.model_version,
        logical_sha256=score_specialist_sha256(score_model),
    )
    _require_registered_artifact(
        family_registry,
        family_key="corners",
        model_version=corner_model.model_version,
        logical_sha256=corner_model_sha256(corner_model),
    )
    _require_registered_artifact(
        family_registry,
        family_key="first_score_timing",
        model_version=timing_model.model_version,
        logical_sha256=first_score_model_sha256(timing_model),
    )
    corner_selection = json.loads(args.corner_selection.read_text(encoding="utf-8"))
    if corner_selection.get("status") != "frozen_before_first_eligible_forward_prediction":
        raise RuntimeError("Corner forward selection is not frozen")
    if corner_selection.get("logical_model_sha256") != corner_model_sha256(corner_model):
        raise RuntimeError("Corner forward selection and model hash differ")
    corner_candidate = corner_selection.get("selected_forward_candidate")
    if corner_candidate not in {
        "independent_poisson",
        "negative_binomial_marginals",
        "dependent_bivariate_count",
    }:
        raise RuntimeError("Corner manifest has no supported forward candidate")

    upcoming = _upcoming_from_moneyline(source_rows)
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        corner_target_build = build_corner_targets(connection)
    finally:
        connection.close()
    corner_rows = ChronologicalCornerFeatureBuilder(
        corner_feature_config
    ).build_inference(
        list(corner_target_build.targets),
        upcoming,
        as_of=_timestamp(moneyline_snapshot["as_of"]),
    )
    corners = {
        (row.fixture_id, row.information_state): row for row in corner_rows
    }

    state_records = []
    for source in sorted(
        source_rows,
        key=lambda row: (row["kickoff"], row["fixture_id"], row["information_state"]),
    ):
        kickoff = _timestamp(source["kickoff"])
        if kickoff <= created_at:
            continue
        key = (source["fixture_id"], source["information_state"])
        fixture = source["fixture"]
        families = []
        families.append(
            _family(
                family_key="regulation_moneyline",
                display_name="Match result",
                status="validated",
                model_version=source.get("model_version", moneyline_snapshot["model_version"]),
                logical_model_sha256=moneyline_snapshot["logical_model_sha256"],
                eligible_for_ranking=True,
                evidence={
                    "training_fixtures": moneyline_snapshot["training_evidence"][
                        "horizon_training_fixtures"
                    ][source["information_state"]],
                    "home_history_matches": source["home_history_matches"],
                    "away_history_matches": source["away_history_matches"],
                    "home_xg_history": source["home_xg_history"],
                    "away_xg_history": source["away_xg_history"],
                    "home_shots_history": source["home_shots_history"],
                    "away_shots_history": source["away_shots_history"],
                    "warnings": source["warnings"],
                },
                markets=moneyline_markets(
                    {
                        "home_win": source["home_win_probability"],
                        "draw": source["draw_probability"],
                        "away_win": source["away_win_probability"],
                    },
                    home_name=fixture["home_team_name"],
                    away_name=fixture["away_team_name"],
                ),
            )
        )

        experimental_allowed = (
            created_at < kickoff
            and created_at >= score_model.prospective_holdout_start
            and created_at >= corner_model.prospective_holdout_start
            and created_at >= timing_model.prospective_holdout_start
            and kickoff >= score_model.prospective_holdout_start
            and kickoff >= corner_model.prospective_holdout_start
            and kickoff >= timing_model.prospective_holdout_start
        )
        unavailable_reason = (
            None
            if experimental_allowed
            else "prospective_holdout_not_started_for_this_fixture"
        )
        if experimental_allowed:
            score_grid = ScoreGrid(
                specialist_score_grid(
                    score_model,
                    score_config,
                    information_state=source["information_state"],
                    expected_home_goals=source["expected_home_goals"],
                    expected_away_goals=source["expected_away_goals"],
                )
            )
            score_moneyline = score_grid.moneyline()
            designated_moneyline = {
                "home_win": source["home_win_probability"],
                "draw": source["draw_probability"],
                "away_win": source["away_win_probability"],
            }
            families.append(
                _family(
                    family_key="regulation_score",
                    display_name="Score and goals",
                    status="experimental",
                    model_version=score_model.model_version,
                    logical_model_sha256=score_specialist_sha256(score_model),
                    eligible_for_ranking=False,
                    evidence={
                        "training_fixtures": _training_count(
                            score_model.horizons, source["information_state"]
                        ),
                        "prospective_holdout_start": score_model.prospective_holdout_start.isoformat(),
                        "moneyline_disagreement_probability_points": {
                            outcome: 100
                            * (score_moneyline[outcome] - designated_moneyline[outcome])
                            for outcome in score_moneyline
                        },
                        "warnings": [
                            "experimental_not_eligible_for_automatic_ranking",
                            "score_family_may_disagree_with_designated_1x2",
                        ],
                    },
                    markets=score_family_markets(score_grid),
                )
            )
            timing_probabilities = first_team_probabilities(
                timing_model,
                information_state=source["information_state"],
                expected_home_goals=source["expected_home_goals"],
                expected_away_goals=source["expected_away_goals"],
            )
            families.append(
                _family(
                    family_key="first_score_timing",
                    display_name="First team to score",
                    status="experimental",
                    model_version=timing_model.model_version,
                    logical_model_sha256=first_score_model_sha256(timing_model),
                    eligible_for_ranking=False,
                    evidence={
                        "training_fixtures": _training_count(
                            timing_model.horizons, source["information_state"]
                        ),
                        "prospective_holdout_start": timing_model.prospective_holdout_start.isoformat(),
                        "warnings": [
                            "experimental_not_eligible_for_automatic_ranking",
                            "first_player_to_score_unavailable",
                        ],
                    },
                    markets=first_team_markets(timing_probabilities),
                )
            )
        else:
            families.extend(
                [
                    _unavailable(
                        "regulation_score",
                        "Score and goals",
                        score_model.model_version,
                        unavailable_reason,
                        evidence={
                            "prospective_holdout_start": score_model.prospective_holdout_start.isoformat()
                        },
                    ),
                    _unavailable(
                        "first_score_timing",
                        "First team to score",
                        timing_model.model_version,
                        unavailable_reason,
                        evidence={
                            "prospective_holdout_start": timing_model.prospective_holdout_start.isoformat()
                        },
                    ),
                ]
            )

        corner_row = corners.get(key)
        if experimental_allowed and corner_row is not None:
            corner_grid = ScoreGrid(
                corner_score_grid(
                    corner_model,
                    corner_config,
                    candidate=corner_candidate,
                    information_state=source["information_state"],
                    expected_home_corners=corner_row.expected_home_corners,
                    expected_away_corners=corner_row.expected_away_corners,
                )
            )
            corner_warnings = ["experimental_not_eligible_for_automatic_ranking"]
            if corner_row.home_cold_start:
                corner_warnings.append("home_corner_history_cold_start")
            if corner_row.away_cold_start:
                corner_warnings.append("away_corner_history_cold_start")
            families.append(
                _family(
                    family_key="corners",
                    display_name="Corners",
                    status="experimental",
                    model_version=corner_model.model_version,
                    logical_model_sha256=corner_model_sha256(corner_model),
                    eligible_for_ranking=False,
                    evidence={
                        "selected_candidate": corner_candidate,
                        "training_fixtures": _training_count(
                            corner_model.horizons, source["information_state"]
                        ),
                        "expected_home_corners": corner_row.expected_home_corners,
                        "expected_away_corners": corner_row.expected_away_corners,
                        "home_history_matches": corner_row.home_history_matches,
                        "away_history_matches": corner_row.away_history_matches,
                        "competition_history_matches": corner_row.competition_history_matches,
                        "prospective_holdout_start": corner_model.prospective_holdout_start.isoformat(),
                        "warnings": corner_warnings,
                    },
                    markets=corner_family_markets(corner_grid),
                )
            )
        else:
            families.append(
                _unavailable(
                    "corners",
                    "Corners",
                    corner_model.model_version,
                    unavailable_reason or "corner_feature_not_available_at_horizon",
                    evidence={
                        "prospective_holdout_start": corner_model.prospective_holdout_start.isoformat()
                    },
                )
            )
        families.append(
            _unavailable(
                "player_events",
                "Player goals and assists",
                "confirmed_lineup_player_v1",
                "requires_two_timestamp_safe_confirmed_lineups",
            )
        )
        state_records.append(
            {
                "fixture_id": source["fixture_id"],
                "fixture": fixture,
                "kickoff": source["kickoff"],
                "information_state": source["information_state"],
                "prediction_at": source["prediction_at"],
                "issued_at": created_at.isoformat(),
                "families": families,
            }
        )

    market_policy, market_policy_hash = load_polymarket_contract_policy(
        args.polymarket_policy
    )
    connection = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        market_summary = attach_polymarket_quotes(
            connection,
            states=state_records,
            policy=market_policy,
            policy_sha256=market_policy_hash,
            created_at=created_at,
            live_max_age_minutes=args.live_market_max_age_minutes,
        )
    finally:
        connection.close()

    if market_summary["live_market_fixture_count"]:
        market_status = "live_market_available"
    elif market_summary["linked_fixture_count"]:
        market_status = "linked_waiting_for_valid_books"
    else:
        market_status = "unavailable_without_linked_polymarket_event"

    snapshot = {
        "snapshot_version": "specialized_bet_platform_snapshot_v1",
        "created_at": created_at.isoformat(),
        "as_of": moneyline_snapshot["as_of"],
        "source_moneyline_snapshot_version": moneyline_snapshot["snapshot_version"],
        "family_registry_version": family_registry.registry_version,
        "market_comparison_status": market_status,
        "market_data": {
            **market_summary,
            "live_refresh_policy": "display_only_not_model_evidence",
            "cutoff_policy": "exact_prediction_time_only",
        },
        "ranking_policy": "validated_families_only",
        "states": state_records,
        "models": {
            "regulation_moneyline": {
                "model_version": moneyline_snapshot["model_version"],
                "logical_sha256": moneyline_snapshot["logical_model_sha256"],
                "status": "validated",
            },
            "regulation_score": {
                "model_version": score_model.model_version,
                "logical_sha256": score_specialist_sha256(score_model),
                "status": "experimental",
            },
            "corners": {
                "model_version": corner_model.model_version,
                "logical_sha256": corner_model_sha256(corner_model),
                "status": "experimental",
            },
            "first_score_timing": {
                "model_version": timing_model.model_version,
                "logical_sha256": first_score_model_sha256(timing_model),
                "status": "experimental",
            },
            "player_events": {
                "model_version": "confirmed_lineup_player_v1",
                "status": "unavailable",
            },
        },
        "target_audit": {
            "corner_safe_fixtures": len(corner_target_build.targets),
            "corner_conflicts_excluded": len(corner_target_build.conflicts),
        },
        "source_hashes": {
            "moneyline_snapshot": _file_sha256(args.moneyline_snapshot),
            "score_model": _file_sha256(args.score_model),
            "score_config": _file_sha256(args.score_config),
            "corner_model": _file_sha256(args.corner_model),
            "corner_config": _file_sha256(args.corner_config),
            "corner_selection": _file_sha256(args.corner_selection),
            "timing_model": _file_sha256(args.timing_model),
            "timing_config": _file_sha256(args.timing_config),
            "family_registry": _file_sha256(args.family_registry),
        },
    }
    snapshot["state_rows_sha256"] = _logical_hash(state_records)
    _validate_platform_snapshot(snapshot)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    latest_path = args.output_dir / "latest.json"
    timestamp_path = None
    if not args.latest_only:
        timestamp_path = args.output_dir / created_at.strftime(
            "%Y%m%dT%H%M%S%fZ.json"
        )
        _write_once_json(timestamp_path, snapshot)
    _atomic_write_json(latest_path, snapshot)
    print(
        json.dumps(
            {
                "snapshot": (
                    None if timestamp_path is None else str(timestamp_path.resolve())
                ),
                "latest": str(latest_path.resolve()),
                "state_rows": len(state_records),
                "fixtures": len({row["fixture_id"] for row in state_records}),
                "family_status_counts": _status_counts(state_records),
                "state_rows_sha256": snapshot["state_rows_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _upcoming_from_moneyline(rows: list[dict]) -> list[RegulationInferenceFixture]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["fixture_id"]].append(row)
    fixtures = []
    for fixture_id, values in grouped.items():
        first = values[0]
        fixtures.append(
            RegulationInferenceFixture(
                fixture_id=fixture_id,
                competition_id=str(first["competition_id"]),
                season_id=(
                    None if first.get("season_id") is None else str(first["season_id"])
                ),
                home_team_id=str(first["home_team_id"]),
                away_team_id=str(first["away_team_id"]),
                neutral_venue=bool(first["fixture"].get("neutral_venue", False)),
                kickoff=_timestamp(first["kickoff"]),
                allowed_information_states=tuple(
                    sorted({row["information_state"] for row in values})
                ),
            )
        )
    return fixtures


def _family(
    *,
    family_key: str,
    display_name: str,
    status: str,
    model_version: str,
    logical_model_sha256: str,
    eligible_for_ranking: bool,
    evidence: dict,
    markets: list[dict],
) -> dict:
    return {
        "family_key": family_key,
        "display_name": display_name,
        "status": status,
        "model_version": model_version,
        "logical_model_sha256": logical_model_sha256,
        "eligible_for_ranking": eligible_for_ranking,
        "unavailable_reason": None,
        "evidence": evidence,
        "markets": markets,
    }


def _unavailable(
    family_key: str,
    display_name: str,
    model_version: str,
    reason: str,
    *,
    evidence: dict | None = None,
) -> dict:
    unavailable_evidence = dict(evidence or {})
    unavailable_evidence["warnings"] = [reason]
    return {
        "family_key": family_key,
        "display_name": display_name,
        "status": "unavailable",
        "model_version": model_version,
        "logical_model_sha256": None,
        "eligible_for_ranking": False,
        "unavailable_reason": reason,
        "evidence": unavailable_evidence,
        "markets": [],
    }


def _training_count(horizons, information_state: str) -> int:
    matches = [
        row.training_fixtures
        for row in horizons
        if row.information_state == information_state
    ]
    if len(matches) != 1:
        raise RuntimeError(f"No unique training count for {information_state}")
    return matches[0]


def _require_registered_artifact(
    registry,
    *,
    family_key: str,
    model_version: str,
    logical_sha256: str,
) -> None:
    try:
        model = registry.family(family_key).model(model_version)
    except KeyError as error:
        raise RuntimeError(
            f"{family_key} model {model_version} is not in the family registry"
        ) from error
    if model.logical_sha256 != logical_sha256:
        raise RuntimeError(
            f"{family_key} artifact hash differs from the family registry"
        )


def _validate_platform_snapshot(snapshot: dict) -> None:
    states = snapshot.get("states")
    if not isinstance(states, list):
        raise RuntimeError("Platform snapshot states must be a list")
    keys = set()
    for row in states:
        key = (row["fixture_id"], row["information_state"])
        if key in keys:
            raise RuntimeError(f"Duplicate platform state: {key}")
        keys.add(key)
        family_keys = [family["family_key"] for family in row["families"]]
        if len(family_keys) != len(set(family_keys)):
            raise RuntimeError(f"Duplicate family in platform state: {key}")
        for family in row["families"]:
            if family["status"] == "validated" and not family["eligible_for_ranking"]:
                raise RuntimeError("Validated family must be eligible for ranking")
            if family["status"] != "validated" and family["eligible_for_ranking"]:
                raise RuntimeError("Non-validated family cannot enter ranking")
            market_ids = [market["market_id"] for market in family["markets"]]
            if len(market_ids) != len(set(market_ids)):
                raise RuntimeError("Duplicate market identifier in family")
    if snapshot.get("state_rows_sha256") != _logical_hash(states):
        raise RuntimeError("Platform state hash mismatch")


def _status_counts(rows: list[dict]) -> dict[str, int]:
    counts = defaultdict(int)
    for row in rows:
        for family in row["families"]:
            counts[f"{family['family_key']}:{family['status']}"] += 1
    return dict(sorted(counts.items()))


def _logical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _write_once_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
