from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
from statistics import median
from typing import Mapping

from .database import stable_id


SELECTIONS = ("home", "draw", "away")
MODEL_OUTCOME_TO_SELECTION = {"home_win": "home", "draw": "draw", "away_win": "away"}
API_VALUE_TO_SELECTION = {"home": "home", "draw": "draw", "away": "away"}
INFORMATION_STATE_OFFSET = {
    "pre_lineup_72h_clean_v1": 4320,
    "pre_lineup_24h_v1": 1440,
}


def persist_api_football_moneyline_odds(
    connection,
    *,
    payload: Mapping[str, object],
    raw_item: Mapping[str, object],
    fixture_id: str,
    fixture_source_id: str,
    quote_type: str,
    bet_id: int,
) -> dict[str, int]:
    """Normalize one immutable API-Football odds page.

    Partial bookmaker responses are retained as observations. Consensus later
    requires a complete Home/Draw/Away triplet from the same raw response.
    """
    raw_artifact_id = str(raw_item["_raw_artifact_id"])
    retrieved_at = _timestamp(raw_item.get("retrieved_at"))
    if retrieved_at is None:
        raise ValueError("API-Football odds artifact has no retrieval timestamp")
    response = payload.get("response")
    records = response if isinstance(response, list) else []
    inserted = 0
    complete_books = 0
    for record in records:
        if not isinstance(record, Mapping):
            continue
        provider_fixture = record.get("fixture")
        provider_fixture_id = (
            provider_fixture.get("id")
            if isinstance(provider_fixture, Mapping)
            else None
        )
        if str(provider_fixture_id) != str(fixture_source_id):
            continue
        quoted_at = _timestamp(record.get("update"))
        bookmakers = record.get("bookmakers")
        if not isinstance(bookmakers, list):
            continue
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, Mapping):
                continue
            bookmaker_id = bookmaker.get("id")
            bookmaker_label = str(bookmaker.get("name") or f"bookmaker_{bookmaker_id}")
            bookmaker_key = f"{bookmaker_label} [api:{bookmaker_id}]"
            prices: dict[str, float] = {}
            bets = bookmaker.get("bets")
            if not isinstance(bets, list):
                continue
            for bet in bets:
                if not isinstance(bet, Mapping) or str(bet.get("id")) != str(bet_id):
                    continue
                values = bet.get("values")
                if not isinstance(values, list):
                    continue
                for value in values:
                    if not isinstance(value, Mapping):
                        continue
                    selection = API_VALUE_TO_SELECTION.get(
                        str(value.get("value") or "").strip().casefold()
                    )
                    odd = _positive_float(value.get("odd"))
                    if selection is not None and odd is not None and odd >= 1:
                        prices[selection] = odd
            if set(prices) == set(SELECTIONS):
                complete_books += 1
            for selection, decimal_odds in prices.items():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO bookmaker_quote (
                        quote_id,fixture_id,source_code,raw_artifact_id,
                        bookmaker_name,market_type,selection,line_value,
                        decimal_odds,quote_type,quoted_at,retrieved_at
                    ) VALUES (?, ?, 'api_football', ?, ?, 'moneyline', ?, NULL,
                              ?, ?, ?, ?)
                    """,
                    [
                        stable_id(
                            "quote",
                            "api_football",
                            fixture_id,
                            raw_artifact_id,
                            bookmaker_key,
                            quote_type,
                            selection,
                        ),
                        fixture_id,
                        raw_artifact_id,
                        bookmaker_key,
                        selection,
                        decimal_odds,
                        quote_type,
                        quoted_at,
                        retrieved_at,
                    ],
                )
                inserted += 1
    return {"inserted_quotes": inserted, "complete_bookmakers": complete_books}


def attach_bookmaker_consensus(
    connection,
    *,
    states: list[dict],
    stage_window_minutes: int,
    minimum_bookmakers: int,
) -> dict[str, object]:
    """Attach median, proportional-de-vigged 1X2 consensus at each cutoff."""
    quoted_fixtures: set[str] = set()
    quote_count = 0
    bookmaker_counts: list[int] = []

    for state in states:
        fixture_id = str(state["fixture_id"])
        information_state = str(state["information_state"])
        offset = INFORMATION_STATE_OFFSET.get(information_state)
        prediction_at = _timestamp(state.get("prediction_at"))
        consensus = None
        if offset is not None and prediction_at is not None:
            quote_type = f"bookmaker_t_minus_{offset}"
            window_start = prediction_at - timedelta(minutes=stage_window_minutes)
            rows = connection.execute(
                """
                SELECT bookmaker_name,raw_artifact_id,selection,decimal_odds,
                       quoted_at,retrieved_at
                FROM bookmaker_quote
                WHERE fixture_id=? AND source_code='api_football'
                  AND market_type='moneyline' AND quote_type=?
                  AND retrieved_at>=? AND retrieved_at<?
                  AND (quoted_at IS NULL OR quoted_at<=?)
                ORDER BY retrieved_at DESC,bookmaker_name,raw_artifact_id,selection
                """,
                [fixture_id, quote_type, window_start, prediction_at, prediction_at],
            ).fetchall()
            consensus = _consensus_from_rows(
                rows,
                minimum_bookmakers=minimum_bookmakers,
                information_state=information_state,
            )

        for family in state["families"]:
            for market in family["markets"]:
                market["live_market"] = None
                selection = market.get("selection")
                outcome = selection.get("outcome") if isinstance(selection, Mapping) else None
                bookmaker_selection = MODEL_OUTCOME_TO_SELECTION.get(str(outcome))
                if (
                    consensus is not None
                    and market.get("contract_key") == "regulation_moneyline"
                    and bookmaker_selection in SELECTIONS
                ):
                    probability = consensus["probabilities"][bookmaker_selection]
                    market["market_comparison"] = {
                        "source": "api_football",
                        "quote_type": "cutoff_consensus",
                        "market_probability": probability,
                        "market_decimal_multiplier": 1.0 / probability,
                        "bookmaker_count": consensus["bookmaker_count"],
                        "consensus_method": "median_proportional_devig",
                        "observed_at": consensus["observed_at"],
                        "retrieved_at": consensus["retrieved_at"],
                    }
                    quote_count += 1
                else:
                    market["market_comparison"] = None
        if consensus is not None:
            quoted_fixtures.add(fixture_id)
            bookmaker_counts.append(int(consensus["bookmaker_count"]))

    return {
        "source": "api_football",
        "consensus_method": "median_proportional_devig",
        "cutoff_market_fixture_count": len(quoted_fixtures),
        "cutoff_market_quote_count": quote_count,
        "minimum_bookmakers": minimum_bookmakers,
        "minimum_observed_bookmakers": min(bookmaker_counts) if bookmaker_counts else None,
        "maximum_observed_bookmakers": max(bookmaker_counts) if bookmaker_counts else None,
    }


def _consensus_from_rows(
    rows: list[tuple],
    *,
    minimum_bookmakers: int,
    information_state: str,
) -> dict[str, object] | None:
    snapshots: dict[tuple[str, str, datetime], dict[str, object]] = {}
    for bookmaker, artifact_id, selection, decimal_odds, quoted_at, retrieved_at in rows:
        retrieved = _aware(retrieved_at)
        key = (str(bookmaker), str(artifact_id), retrieved)
        snapshot = snapshots.setdefault(
            key,
            {"prices": {}, "quoted_at": _aware_or_none(quoted_at), "retrieved_at": retrieved},
        )
        odd = _positive_float(decimal_odds)
        if selection in SELECTIONS and odd is not None and odd >= 1:
            snapshot["prices"][str(selection)] = odd

    latest_by_bookmaker: dict[str, dict[str, object]] = {}
    for (bookmaker, _artifact, _retrieved), snapshot in snapshots.items():
        if set(snapshot["prices"]) != set(SELECTIONS):
            continue
        current = latest_by_bookmaker.get(bookmaker)
        if current is None or snapshot["retrieved_at"] > current["retrieved_at"]:
            latest_by_bookmaker[bookmaker] = snapshot
    if len(latest_by_bookmaker) < minimum_bookmakers:
        return None

    devigged: list[dict[str, float]] = []
    for snapshot in latest_by_bookmaker.values():
        inverse = {selection: 1.0 / snapshot["prices"][selection] for selection in SELECTIONS}
        overround = sum(inverse.values())
        if not math.isfinite(overround) or overround <= 0:
            continue
        devigged.append({selection: inverse[selection] / overround for selection in SELECTIONS})
    if len(devigged) < minimum_bookmakers:
        return None
    raw_medians = {
        selection: median(book[selection] for book in devigged) for selection in SELECTIONS
    }
    total = sum(raw_medians.values())
    probabilities = {selection: raw_medians[selection] / total for selection in SELECTIONS}
    retrieved_at = max(snapshot["retrieved_at"] for snapshot in latest_by_bookmaker.values())
    quoted_values = [
        snapshot["quoted_at"]
        for snapshot in latest_by_bookmaker.values()
        if snapshot["quoted_at"] is not None
    ]
    observed_at = max(quoted_values) if quoted_values else retrieved_at
    return {
        "information_state": information_state,
        "probabilities": probabilities,
        "bookmaker_count": len(devigged),
        "observed_at": observed_at.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
    }


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware_or_none(value: datetime | None) -> datetime | None:
    return _aware(value) if value is not None else None
