from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Mapping


def attach_polymarket_quotes(
    connection,
    *,
    states: list[dict],
    policy: Mapping[str, object],
    policy_sha256: str,
    created_at: datetime,
    live_max_age_minutes: int,
) -> dict[str, object]:
    """Attach audited cutoff books and display-only live books to UI markets.

    Cutoff books retain the exact prediction-time constraints used by research.
    Live books are deliberately separate and can never fill a missing cutoff.
    """

    created_at = created_at.astimezone(timezone.utc)
    mapping_rows = connection.execute(
        """
        SELECT cm.fixture_id,cm.contract_key,cm.parameters,
               om.canonical_selection,om.polarity,o.outcome_id,
               o.source_token_id,e.slug
        FROM polymarket_contract_mapping cm
        JOIN polymarket_contract_outcome_mapping om USING (mapping_id)
        JOIN prediction_market_outcome o USING (outcome_id)
        JOIN prediction_market m
          ON m.prediction_market_id=cm.prediction_market_id
        JOIN prediction_market_event e USING (prediction_market_event_id)
        WHERE cm.mapping_version=? AND cm.mapping_policy_sha256=?
          AND cm.mapping_status='accepted'
          AND o.source_token_id IS NOT NULL
          AND coalesce(m.active,true) AND NOT coalesce(m.closed,false)
        ORDER BY cm.fixture_id,cm.contract_key,cm.prediction_market_id,
                 om.canonical_selection,om.polarity
        """,
        [policy["mapping_version"], policy_sha256],
    ).fetchall()
    mappings_by_fixture: dict[str, list[dict[str, object]]] = defaultdict(list)
    outcome_ids: set[str] = set()
    for row in mapping_rows:
        parameters = row[2]
        if isinstance(parameters, str):
            parameters = json.loads(parameters)
        mapping = {
            "fixture_id": str(row[0]),
            "contract_key": str(row[1]),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "canonical_selection": str(row[3]),
            "polarity": int(row[4]),
            "outcome_id": str(row[5]),
            "source_token_id": str(row[6]),
            "event_slug": str(row[7] or ""),
        }
        mappings_by_fixture[mapping["fixture_id"]].append(mapping)
        outcome_ids.add(mapping["outcome_id"])

    books_by_outcome: dict[str, list[dict[str, object]]] = defaultdict(list)
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        rows = connection.execute(
            f"""
            SELECT outcome_id,source_token_id,cadence_stage,observed_at,
                   retrieved_at,best_bid,best_ask,book_complete,
                   capture_target_at,capture_deadline_at,capture_timing_valid,
                   kickoff_known_at_retrieval,orderbook_snapshot_id
            FROM orderbook_snapshot
            WHERE outcome_id IN ({placeholders})
              AND cadence_stage IS NOT NULL
            ORDER BY outcome_id,retrieved_at DESC,orderbook_snapshot_id
            """,
            sorted(outcome_ids),
        ).fetchall()
        for row in rows:
            books_by_outcome[str(row[0])].append(
                {
                    "source_token_id": str(row[1]),
                    "cadence_stage": str(row[2]),
                    "observed_at": _aware(row[3]),
                    "retrieved_at": _aware(row[4]),
                    "best_bid": row[5],
                    "best_ask": row[6],
                    "book_complete": row[7],
                    "capture_target_at": _aware_or_none(row[8]),
                    "capture_deadline_at": _aware_or_none(row[9]),
                    "capture_timing_valid": row[10],
                    "kickoff_known_at_retrieval": _aware_or_none(row[11]),
                    "orderbook_snapshot_id": str(row[12]),
                }
            )

    maximum_spread = float(policy["capture"]["maximum_bid_ask_spread"])
    horizon_stages = policy["horizon_stage"]
    live_cutoff = created_at - timedelta(minutes=live_max_age_minutes)
    live_retrieved: list[datetime] = []
    live_fixtures: set[str] = set()
    cutoff_fixtures: set[str] = set()

    for state in states:
        fixture_id = str(state["fixture_id"])
        kickoff = _timestamp(state["kickoff"])
        prediction_at = _timestamp(state["prediction_at"])
        stage = horizon_stages.get(state["information_state"])
        fixture_mappings = mappings_by_fixture.get(fixture_id, [])
        for family in state["families"]:
            for market in family["markets"]:
                matches = [
                    mapping
                    for mapping in fixture_mappings
                    if _mapping_matches_market(mapping, market)
                ]
                if len(matches) != 1:
                    market["market_comparison"] = None
                    market["live_market"] = None
                    continue
                mapping = matches[0]
                books = books_by_outcome.get(mapping["outcome_id"], [])
                cutoff_book = next(
                    (
                        book
                        for book in books
                        if isinstance(stage, str)
                        and book["cadence_stage"] == stage
                        and book["capture_timing_valid"] is True
                        and book["capture_target_at"] == prediction_at
                        and book["capture_deadline_at"] == prediction_at
                        and book["retrieved_at"] < prediction_at
                        and book["kickoff_known_at_retrieval"] == kickoff
                        and _valid_top_of_book(book, maximum_spread)
                    ),
                    None,
                )
                live_book = next(
                    (
                        book
                        for book in books
                        if book["cadence_stage"] == "market_live"
                        and book["capture_timing_valid"] is True
                        and live_cutoff <= book["retrieved_at"] <= created_at
                        and book["retrieved_at"] < kickoff
                        and book["kickoff_known_at_retrieval"] == kickoff
                        and _valid_top_of_book(book, maximum_spread)
                    ),
                    None,
                )
                market["market_comparison"] = (
                    _public_quote(cutoff_book, mapping, "cutoff")
                    if cutoff_book is not None
                    else None
                )
                market["live_market"] = (
                    _public_quote(live_book, mapping, "live")
                    if live_book is not None
                    else None
                )
                if cutoff_book is not None:
                    cutoff_fixtures.add(fixture_id)
                if live_book is not None:
                    live_fixtures.add(fixture_id)
                    live_retrieved.append(live_book["retrieved_at"])

    return {
        "linked_fixture_count": len(mappings_by_fixture),
        "cutoff_market_fixture_count": len(cutoff_fixtures),
        "live_market_fixture_count": len(live_fixtures),
        "live_market_as_of": max(live_retrieved).isoformat() if live_retrieved else None,
    }


def _mapping_matches_market(mapping: Mapping[str, object], market: Mapping[str, object]) -> bool:
    if mapping["contract_key"] != market.get("contract_key"):
        return False
    parameters = mapping["parameters"]
    selection = market.get("selection")
    if not isinstance(parameters, Mapping) or not isinstance(selection, Mapping):
        return False
    contract = str(mapping["contract_key"])
    canonical = str(mapping["canonical_selection"])
    polarity = int(mapping["polarity"])
    if contract == "regulation_moneyline":
        return polarity == 1 and canonical == selection.get("outcome")
    if contract == "regulation_total_goals":
        return (
            polarity == 1
            and canonical == selection.get("side")
            and _same_number(parameters.get("line"), selection.get("line"))
        )
    if contract == "regulation_team_total_goals":
        return (
            polarity == 1
            and canonical == selection.get("side")
            and parameters.get("team") == selection.get("team")
            and _same_number(parameters.get("line"), selection.get("line"))
        )
    if contract == "regulation_both_teams_to_score":
        desired_polarity = 1 if selection.get("outcome") == "yes" else -1
        return canonical == "yes" and polarity == desired_polarity
    if contract == "regulation_exact_score":
        return (
            polarity == 1
            and canonical
            == f"score_{selection.get('home_goals')}_{selection.get('away_goals')}"
        )
    if contract == "regulation_goal_handicap":
        team = selection.get("team")
        line = selection.get("line")
        home_handicap = parameters.get("home_handicap")
        try:
            away_handicap = -float(home_handicap)
        except (TypeError, ValueError):
            return False
        return polarity == 1 and (
            (
                team == "home"
                and canonical == "home_cover"
                and _same_number(line, home_handicap)
            )
            or (
                team == "away"
                and canonical == "away_cover"
                and _same_number(line, away_handicap)
            )
        )
    return False


def _valid_top_of_book(book: Mapping[str, object], maximum_spread: float) -> bool:
    if book.get("book_complete") is not True:
        return False
    try:
        bid = float(book["best_bid"])
        ask = float(book["best_ask"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(bid)
        and math.isfinite(ask)
        and 0 < bid <= ask < 1
        and ask - bid <= maximum_spread + 1e-12
    )


def _public_quote(
    book: Mapping[str, object], mapping: Mapping[str, object], quote_type: str
) -> dict[str, object]:
    bid = float(book["best_bid"])
    ask = float(book["best_ask"])
    slug = str(mapping.get("event_slug") or "")
    return {
        "source": "polymarket",
        "quote_type": quote_type,
        "market_probability": (bid + ask) / 2.0,
        "market_decimal_multiplier": 1.0 / ask,
        "best_bid_probability": bid,
        "best_ask_probability": ask,
        "bid_ask_spread": ask - bid,
        "observed_at": book["observed_at"].isoformat(),
        "retrieved_at": book["retrieved_at"].isoformat(),
        "event_url": f"https://polymarket.com/event/{slug}" if slug else None,
    }


def _same_number(left: object, right: object) -> bool:
    try:
        return math.isclose(float(left), float(right), abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)
