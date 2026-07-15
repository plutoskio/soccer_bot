from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile

import duckdb

from soccer_bot.config import load_json
from soccer_bot.database import normalized_name
from soccer_bot.datasets.features import RegulationFeatureRow
from soccer_bot.modeling.walk_forward import (
    EvaluationFold,
    WalkForwardPrediction,
    assign_fold,
    block_bootstrap_interval,
    comparison_seed,
)


class MarketBenchmarkError(RuntimeError):
    """Raised when a market benchmark violates its timing or mapping policy."""


@dataclass(frozen=True)
class MarketBenchmarkConfig:
    benchmark_version: str
    probability_floor: float
    polymarket_market_type: str
    polymarket_outcome_name: str
    require_bid_and_ask: bool
    require_known_kickoff: bool
    maximum_bid_ask_spread: float
    polymarket_minimum_fixtures: int
    bookmaker_source_code: str
    bookmaker_name: str
    bookmaker_quote_type: str
    bookmaker_minimum_fixtures: int
    bootstrap_replicates: int
    bootstrap_seed: int


@dataclass(frozen=True)
class MarketBenchmarkRow:
    benchmark_version: str
    model_key: str
    timing_class: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    fold_key: str
    snapshot_at: datetime | None
    worst_staleness_minutes: float | None
    home_goals: int
    away_goals: int
    result: str
    raw_probability_sum: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    moneyline_log_loss: float
    moneyline_brier: float


def load_market_benchmark_config(path: Path) -> MarketBenchmarkConfig:
    raw = load_json(path)
    polymarket = raw.get("timestamped_polymarket", {})
    bookmaker = raw.get("retrospective_bookmaker", {})
    uncertainty = raw.get("uncertainty", {})
    if raw.get("contract") != "regulation_moneyline":
        raise MarketBenchmarkError("Market benchmark must use regulation moneyline")
    if polymarket.get("price") != "midpoint_then_normalize_three_yes_prices":
        raise MarketBenchmarkError("Unexpected Polymarket price policy")
    if bookmaker.get("price") != "inverse_decimal_odds_then_normalize":
        raise MarketBenchmarkError("Unexpected bookmaker price policy")
    if bookmaker.get("feature_eligible") is not False:
        raise MarketBenchmarkError("Untimestamped closing quotes cannot be features")
    if uncertainty.get("paired_block_unit") != "calendar_month":
        raise MarketBenchmarkError("Market comparison must use calendar-month blocks")
    config = MarketBenchmarkConfig(
        benchmark_version=str(raw.get("benchmark_version", "")),
        probability_floor=float(raw["probability_floor"]),
        polymarket_market_type=str(polymarket["market_type"]),
        polymarket_outcome_name=str(polymarket["outcome_name"]),
        require_bid_and_ask=bool(polymarket["require_bid_and_ask"]),
        require_known_kickoff=bool(
            polymarket["require_known_kickoff_at_retrieval"]
        ),
        maximum_bid_ask_spread=float(polymarket["maximum_bid_ask_spread"]),
        polymarket_minimum_fixtures=int(
            polymarket["minimum_complete_fixtures_to_score"]
        ),
        bookmaker_source_code=str(bookmaker["source_code"]),
        bookmaker_name=str(bookmaker["bookmaker_name"]),
        bookmaker_quote_type=str(bookmaker["quote_type"]),
        bookmaker_minimum_fixtures=int(
            bookmaker["minimum_complete_fixtures_to_score"]
        ),
        bootstrap_replicates=int(uncertainty["bootstrap_replicates"]),
        bootstrap_seed=int(uncertainty["bootstrap_seed"]),
    )
    if not config.benchmark_version:
        raise MarketBenchmarkError("benchmark_version is required")
    if not 0 < config.probability_floor < 1:
        raise MarketBenchmarkError("probability_floor must be in (0, 1)")
    if not 0 < config.maximum_bid_ask_spread < 1:
        raise MarketBenchmarkError("maximum_bid_ask_spread must be in (0, 1)")
    if min(
        config.polymarket_minimum_fixtures,
        config.bookmaker_minimum_fixtures,
        config.bootstrap_replicates,
    ) <= 0:
        raise MarketBenchmarkError("Coverage and bootstrap counts must be positive")
    return config


def build_market_benchmarks(
    connection,
    feature_rows: list[RegulationFeatureRow],
    *,
    config: MarketBenchmarkConfig,
    folds: tuple[EvaluationFold, ...],
) -> tuple[list[MarketBenchmarkRow], dict]:
    polymarket_rows, polymarket_audit = _build_polymarket_rows(
        connection, feature_rows, config=config, folds=folds
    )
    bookmaker_rows, bookmaker_audit = _build_bookmaker_rows(
        connection, feature_rows, config=config, folds=folds
    )
    return (
        sorted(
            [*polymarket_rows, *bookmaker_rows],
            key=lambda row: (
                row.prediction_at,
                row.fixture_id,
                row.information_state,
                row.model_key,
            ),
        ),
        {
            "timestamped_polymarket": polymarket_audit,
            "retrospective_bookmaker": bookmaker_audit,
        },
    )


def summarize_market_benchmarks(
    market_rows: list[MarketBenchmarkRow],
    baseline_predictions: list[WalkForwardPrediction] | list[object],
    *,
    audit: dict,
    config: MarketBenchmarkConfig,
) -> dict:
    metrics = []
    grouped: dict[tuple[str, str, str], list[MarketBenchmarkRow]] = defaultdict(list)
    for row in market_rows:
        grouped[(row.model_key, row.information_state, row.fold_key)].append(row)
    for (model_key, information_state, fold_key), values in sorted(grouped.items()):
        minimum = _minimum_coverage(model_key, config)
        metrics.append(
            {
                "model_key": model_key,
                "information_state": information_state,
                "fold_key": fold_key,
                "fixtures": len(values),
                "minimum_fixtures_to_score": minimum,
                "coverage_gate_passed": len(values) >= minimum,
                "mean_moneyline_log_loss": (
                    math.fsum(row.moneyline_log_loss for row in values) / len(values)
                ),
                "mean_moneyline_brier": (
                    math.fsum(row.moneyline_brier for row in values) / len(values)
                ),
            }
        )

    preferred_baseline = next(
        (
            model_key
            for model_key in (
                "independent_poisson_xg_shots_correction_v1_temperature_calibrated",
                "independent_poisson_temperature_calibrated",
                "independent_poisson",
            )
            if any(row.model_key == model_key for row in baseline_predictions)
        ),
        None,
    )
    if preferred_baseline is None:
        raise MarketBenchmarkError("No supported baseline model was supplied")
    baseline = {
        (row.fixture_id, row.information_state): row
        for row in baseline_predictions
        if row.model_key == preferred_baseline
    }
    comparisons = []
    for (model_key, information_state, fold_key), values in sorted(grouped.items()):
        pairs = [
            (row, baseline[(row.fixture_id, row.information_state)])
            for row in values
            if (row.fixture_id, row.information_state) in baseline
            and baseline[(row.fixture_id, row.information_state)].fold_key == fold_key
        ]
        minimum = _minimum_coverage(model_key, config)
        for metric in ("moneyline_log_loss", "moneyline_brier"):
            blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
            for market, model in pairs:
                blocks[(market.kickoff.year, market.kickoff.month)].append(
                    getattr(market, metric) - getattr(model, metric)
                )
            comparison = {
                "challenger_model": model_key,
                "baseline_model": preferred_baseline,
                "information_state": information_state,
                "fold_key": fold_key,
                "metric": metric,
                "paired_fixtures": len(pairs),
                "coverage_gate_passed": len(pairs) >= minimum,
                "lower_is_better": True,
            }
            if pairs:
                differences = [value for block in blocks.values() for value in block]
                lower, upper, probability = block_bootstrap_interval(
                    blocks,
                    replicates=config.bootstrap_replicates,
                    seed=comparison_seed(
                        config.bootstrap_seed,
                        model_key,
                        information_state,
                        fold_key,
                        metric,
                    ),
                )
                comparison.update(
                    {
                        "calendar_month_blocks": len(blocks),
                        "mean_delta_market_minus_model": math.fsum(differences)
                        / len(differences),
                        "paired_month_block_bootstrap_95_lower": lower,
                        "paired_month_block_bootstrap_95_upper": upper,
                        "bootstrap_probability_market_is_better": probability,
                    }
                )
            comparisons.append(comparison)
    return {
        "benchmark_version": config.benchmark_version,
        "audit": audit,
        "metrics": metrics,
        "paired_model_comparisons": comparisons,
    }


def write_market_rows_parquet(rows: list[MarketBenchmarkRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    ordered = sorted(
        rows,
        key=lambda row: (
            row.prediction_at,
            row.fixture_id,
            row.information_state,
            row.model_key,
        ),
    )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".jsonl",
        prefix="market-benchmark-",
        dir=path.parent,
        delete=False,
    )
    json_path = Path(handle.name)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with handle:
            for row in ordered:
                value = asdict(row)
                value["prediction_at"] = row.prediction_at.timestamp()
                value["kickoff"] = row.kickoff.timestamp()
                value["snapshot_at"] = (
                    None if row.snapshot_at is None else row.snapshot_at.timestamp()
                )
                handle.write(
                    json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
                )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"""
                COPY (
                    SELECT * REPLACE (
                        to_timestamp(prediction_at) AS prediction_at,
                        to_timestamp(kickoff) AS kickoff,
                        to_timestamp(try_cast(snapshot_at AS DOUBLE)) AS snapshot_at
                    )
                    FROM read_json_auto({_sql_literal(json_path)},
                        format='newline_delimited')
                    ORDER BY prediction_at, fixture_id, information_state, model_key
                ) TO {_sql_literal(temporary)} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        finally:
            connection.close()
        os.replace(temporary, path)
    finally:
        json_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def _build_polymarket_rows(
    connection,
    feature_rows: list[RegulationFeatureRow],
    *,
    config: MarketBenchmarkConfig,
    folds: tuple[EvaluationFold, ...],
) -> tuple[list[MarketBenchmarkRow], dict]:
    raw = connection.execute(
        """
        SELECT
            e.fixture_id,
            ht.name AS home_name,
            away_team.name AS away_name,
            m.prediction_market_id,
            m.question,
            o.outcome_name,
            s.retrieved_at,
            s.best_bid,
            s.best_ask,
            s.kickoff_known_at_retrieval
        FROM prediction_market_event e
        JOIN fixture f USING (fixture_id)
        JOIN team ht ON ht.team_id=f.home_team_id
        JOIN team away_team ON away_team.team_id=f.away_team_id
        JOIN prediction_market m USING (prediction_market_event_id)
        JOIN prediction_market_outcome o USING (prediction_market_id)
        LEFT JOIN orderbook_snapshot s USING (outcome_id)
        WHERE m.market_type=? AND lower(o.outcome_name)=lower(?)
        ORDER BY e.fixture_id, m.prediction_market_id, s.retrieved_at
        """,
        [config.polymarket_market_type, config.polymarket_outcome_name],
    ).fetchall()
    by_fixture: dict[str, list[tuple]] = defaultdict(list)
    for item in raw:
        by_fixture[str(item[0])].append(item)

    reasons = Counter()
    rows = []
    for feature in feature_rows:
        candidates = by_fixture.get(feature.fixture_id, [])
        if not candidates:
            reasons["no_linked_moneyline_markets"] += 1
            continue
        classified: dict[str, list[tuple]] = defaultdict(list)
        for item in candidates:
            selection = _classify_moneyline_question(
                str(item[4] or ""), str(item[1]), str(item[2])
            )
            if selection is not None:
                classified[selection].append(item)
        if set(classified) != {"home_win", "draw", "away_win"}:
            reasons["incomplete_three_way_semantic_mapping"] += 1
            continue
        selected = {}
        invalid_book_seen = False
        for selection, items in classified.items():
            valid = []
            for item in items:
                retrieved_at, bid, ask, known_kickoff = item[6:10]
                if retrieved_at is None or retrieved_at > feature.prediction_at:
                    continue
                if retrieved_at >= feature.kickoff:
                    continue
                if config.require_known_kickoff and known_kickoff is None:
                    continue
                if config.require_bid_and_ask and (bid is None or ask is None):
                    continue
                if bid is None or ask is None:
                    continue
                if not (0 < bid <= ask < 1):
                    invalid_book_seen = True
                    continue
                if ask - bid > config.maximum_bid_ask_spread:
                    invalid_book_seen = True
                    continue
                valid.append(item)
            if valid:
                selected[selection] = max(valid, key=lambda item: item[6])
        if set(selected) != {"home_win", "draw", "away_win"}:
            reasons[
                "invalid_or_wide_orderbook"
                if invalid_book_seen
                else "incomplete_three_way_pre_cutoff_books"
            ] += 1
            continue
        raw_probabilities = {
            key: (item[7] + item[8]) / 2.0 for key, item in selected.items()
        }
        snapshot_times = [item[6] for item in selected.values()]
        rows.append(
            _market_row(
                feature,
                config=config,
                folds=folds,
                model_key="polymarket_timestamped_no_vig",
                timing_class="timestamped_pre_cutoff_orderbook",
                raw_probabilities=raw_probabilities,
                snapshot_at=max(snapshot_times),
                worst_staleness_minutes=max(
                    (feature.prediction_at - value).total_seconds() / 60.0
                    for value in snapshot_times
                ),
            )
        )
    unique_fixtures = len({row.fixture_id for row in rows})
    return rows, {
        "candidate_feature_rows": len(feature_rows),
        "eligible_rows": len(rows),
        "eligible_fixtures": unique_fixtures,
        "minimum_complete_fixtures_to_score": config.polymarket_minimum_fixtures,
        "coverage_gate_passed": unique_fixtures >= config.polymarket_minimum_fixtures,
        "exclusion_reasons": dict(sorted(reasons.items())),
    }


def _build_bookmaker_rows(
    connection,
    feature_rows: list[RegulationFeatureRow],
    *,
    config: MarketBenchmarkConfig,
    folds: tuple[EvaluationFold, ...],
) -> tuple[list[MarketBenchmarkRow], dict]:
    raw = connection.execute(
        """
        SELECT fixture_id, selection, decimal_odds, quoted_at
        FROM bookmaker_quote
        WHERE source_code=? AND bookmaker_name=? AND market_type='moneyline'
          AND quote_type=?
        ORDER BY fixture_id, selection
        """,
        [
            config.bookmaker_source_code,
            config.bookmaker_name,
            config.bookmaker_quote_type,
        ],
    ).fetchall()
    by_fixture: dict[str, dict[str, tuple[float, datetime | None]]] = defaultdict(dict)
    for fixture_id, selection, decimal_odds, quoted_at in raw:
        if decimal_odds is not None and decimal_odds > 1:
            by_fixture[str(fixture_id)][str(selection)] = (
                float(decimal_odds),
                quoted_at,
            )
    reasons = Counter()
    rows = []
    for feature in feature_rows:
        quotes = by_fixture.get(feature.fixture_id)
        if not quotes or set(quotes) != {"home", "draw", "away"}:
            reasons["missing_complete_three_way_closing_quotes"] += 1
            continue
        if any(value[1] is not None for value in quotes.values()):
            raise MarketBenchmarkError(
                "Retrospective benchmark unexpectedly contains quoted_at values"
            )
        raw_probabilities = {
            "home_win": 1.0 / quotes["home"][0],
            "draw": 1.0 / quotes["draw"][0],
            "away_win": 1.0 / quotes["away"][0],
        }
        rows.append(
            _market_row(
                feature,
                config=config,
                folds=folds,
                model_key="football_data_closing_consensus_no_vig",
                timing_class="retrospective_closing_without_quote_timestamp",
                raw_probabilities=raw_probabilities,
                snapshot_at=None,
                worst_staleness_minutes=None,
            )
        )
    unique_fixtures = len({row.fixture_id for row in rows})
    return rows, {
        "candidate_feature_rows": len(feature_rows),
        "eligible_rows": len(rows),
        "eligible_fixtures": unique_fixtures,
        "minimum_complete_fixtures_to_score": config.bookmaker_minimum_fixtures,
        "coverage_gate_passed": unique_fixtures >= config.bookmaker_minimum_fixtures,
        "feature_eligible": False,
        "feature_ineligible_reason": "quoted_at_is_null",
        "exclusion_reasons": dict(sorted(reasons.items())),
    }


def _market_row(
    feature: RegulationFeatureRow,
    *,
    config: MarketBenchmarkConfig,
    folds: tuple[EvaluationFold, ...],
    model_key: str,
    timing_class: str,
    raw_probabilities: dict[str, float],
    snapshot_at: datetime | None,
    worst_staleness_minutes: float | None,
) -> MarketBenchmarkRow:
    raw_sum = math.fsum(raw_probabilities.values())
    if not math.isfinite(raw_sum) or raw_sum <= 0:
        raise MarketBenchmarkError("Market probabilities cannot be normalized")
    probabilities = {key: value / raw_sum for key, value in raw_probabilities.items()}
    if any(not 0 < value < 1 for value in probabilities.values()):
        raise MarketBenchmarkError("Normalized market probability is invalid")
    result = _result(feature.home_goals, feature.away_goals)
    probability = max(probabilities[result], config.probability_floor)
    brier = math.fsum(
        (probabilities[key] - float(key == result)) ** 2
        for key in ("home_win", "draw", "away_win")
    )
    return MarketBenchmarkRow(
        benchmark_version=config.benchmark_version,
        model_key=model_key,
        timing_class=timing_class,
        fixture_id=feature.fixture_id,
        information_state=feature.information_state,
        prediction_at=feature.prediction_at,
        kickoff=feature.kickoff,
        fold_key=assign_fold(feature.kickoff, folds),
        snapshot_at=snapshot_at,
        worst_staleness_minutes=worst_staleness_minutes,
        home_goals=feature.home_goals,
        away_goals=feature.away_goals,
        result=result,
        raw_probability_sum=raw_sum,
        home_win_probability=probabilities["home_win"],
        draw_probability=probabilities["draw"],
        away_win_probability=probabilities["away_win"],
        moneyline_log_loss=-math.log(probability),
        moneyline_brier=brier,
    )


def _classify_moneyline_question(
    question: str, home_name: str, away_name: str
) -> str | None:
    value = normalized_name(question)
    if "end in a draw" in value or "end in draw" in value:
        return "draw"
    home = normalized_name(home_name)
    away = normalized_name(away_name)
    home_present = bool(home and home in value)
    away_present = bool(away and away in value)
    if " win " not in f" {value} ":
        return None
    if home_present and not away_present:
        return "home_win"
    if away_present and not home_present:
        return "away_win"
    return None


def _minimum_coverage(model_key: str, config: MarketBenchmarkConfig) -> int:
    if model_key == "polymarket_timestamped_no_vig":
        return config.polymarket_minimum_fixtures
    return config.bookmaker_minimum_fixtures


def _result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def _sql_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"
