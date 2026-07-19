from __future__ import annotations

import math
from typing import Iterable

from soccer_bot.contracts import ScoreGrid, SettlementDistribution


class PlatformMarketError(RuntimeError):
    """Raised when a frontend market catalogue is incoherent."""


def moneyline_markets(
    probabilities: dict[str, float], *, home_name: str, away_name: str
) -> list[dict]:
    labels = {
        "home_win": home_name,
        "draw": "Draw",
        "away_win": away_name,
    }
    return [
        _binary(
            market_id=f"regulation_moneyline:{outcome}",
            contract_key="regulation_moneyline",
            group="Match result",
            label=label,
            selection={"outcome": outcome},
            probability=probabilities[outcome],
        )
        for outcome, label in labels.items()
    ]


def score_family_markets(grid: ScoreGrid) -> list[dict]:
    markets = []
    ordered_scores = sorted(
        grid.probabilities.items(), key=lambda item: (-item[1], item[0])
    )
    for (home, away), probability in ordered_scores[:25]:
        markets.append(
            _binary(
                market_id=f"regulation_exact_score:{home}-{away}",
                contract_key="regulation_exact_score",
                group="Exact score",
                label=f"{home}–{away}",
                selection={"home_goals": home, "away_goals": away},
                probability=probability,
            )
        )
    btts = grid.both_teams_to_score()
    for outcome in ("yes", "no"):
        markets.append(
            _binary(
                market_id=f"regulation_both_teams_to_score:{outcome}",
                contract_key="regulation_both_teams_to_score",
                group="Both teams score",
                label="Yes" if outcome == "yes" else "No",
                selection={"outcome": outcome},
                probability=btts[outcome],
            )
        )
    for line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
        for side in ("over", "under"):
            markets.append(
                _settlement(
                    market_id=f"regulation_total_goals:{side}:{line:g}",
                    contract_key="regulation_total_goals",
                    group="Total goals",
                    label=f"{side.title()} {line:g}",
                    selection={"side": side, "line": line},
                    distribution=grid.total_goals(line=line, selection=side),
                    line=line,
                )
            )
    for team in ("home", "away"):
        for line in (0.5, 1.5, 2.5, 3.5):
            for side in ("over", "under"):
                markets.append(
                    _settlement(
                        market_id=(
                            f"regulation_team_total_goals:{team}:{side}:{line:g}"
                        ),
                        contract_key="regulation_team_total_goals",
                        group=f"{team.title()} team goals",
                        label=f"{side.title()} {line:g}",
                        selection={"team": team, "side": side, "line": line},
                        distribution=grid.team_total_goals(
                            team=team, line=line, selection=side
                        ),
                        line=line,
                    )
                )
    for team in ("home", "away"):
        for line in _quarter_lines(-2.5, 2.5):
            markets.append(
                _settlement(
                    market_id=f"regulation_goal_handicap:{team}:{line:+g}",
                    contract_key="regulation_goal_handicap",
                    group="Goal handicap",
                    label=f"{team.title()} {line:+g}",
                    selection={"team": team, "line": line},
                    distribution=grid.goal_handicap(team=team, line=line),
                    line=line,
                )
            )
    _validate_markets(markets)
    return markets


def corner_family_markets(grid: ScoreGrid) -> list[dict]:
    markets = []
    for line in tuple(value + 0.5 for value in range(4, 16)):
        for side in ("over", "under"):
            markets.append(
                _settlement(
                    market_id=f"match_corner_total:{side}:{line:g}",
                    contract_key="match_corner_total",
                    group="Match corners",
                    label=f"{side.title()} {line:g}",
                    selection={"side": side, "line": line},
                    distribution=grid.total_goals(line=line, selection=side),
                    line=line,
                )
            )
    for team in ("home", "away"):
        for line in tuple(value + 0.5 for value in range(0, 10)):
            for side in ("over", "under"):
                markets.append(
                    _settlement(
                        market_id=f"team_corner_total:{team}:{side}:{line:g}",
                        contract_key="team_corner_total",
                        group=f"{team.title()} team corners",
                        label=f"{side.title()} {line:g}",
                        selection={"team": team, "side": side, "line": line},
                        distribution=grid.team_total_goals(
                            team=team, line=line, selection=side
                        ),
                        line=line,
                    )
                )
        for line in tuple(value + 0.5 for value in range(-6, 6)):
            markets.append(
                _settlement(
                    market_id=f"corner_handicap:{team}:{line:+g}",
                    contract_key="corner_handicap",
                    group="Corner handicap",
                    label=f"{team.title()} {line:+g}",
                    selection={"team": team, "line": line},
                    distribution=grid.goal_handicap(team=team, line=line),
                    line=line,
                )
            )
    _validate_markets(markets)
    return markets


def first_team_markets(probabilities: dict[str, float]) -> list[dict]:
    labels = {
        "home_first": "Home team scores first",
        "away_first": "Away team scores first",
        "no_goal": "No goal",
    }
    markets = [
        _binary(
            market_id=f"first_team_to_score:{outcome}",
            contract_key="first_team_to_score",
            group="First team to score",
            label=labels[outcome],
            selection={"outcome": outcome},
            probability=probabilities[outcome],
        )
        for outcome in labels
    ]
    _validate_markets(markets)
    return markets


def _binary(
    *,
    market_id: str,
    contract_key: str,
    group: str,
    label: str,
    selection: dict,
    probability: float,
) -> dict:
    if not math.isfinite(probability) or not 0 < probability <= 1:
        raise PlatformMarketError(f"Invalid binary probability for {market_id}")
    return {
        "market_id": market_id,
        "contract_key": contract_key,
        "group": group,
        "label": label,
        "selection": selection,
        "line": None,
        "probability": probability,
        "fair_decimal_multiplier": 1.0 / probability,
        "settlement_probabilities": None,
        "market_comparison": None,
    }


def _settlement(
    *,
    market_id: str,
    contract_key: str,
    group: str,
    label: str,
    selection: dict,
    distribution: SettlementDistribution,
    line: float,
) -> dict:
    fair = distribution.fair_decimal_odds()
    if math.isnan(fair) or fair < 1:
        raise PlatformMarketError(f"Invalid fair multiplier for {market_id}")
    probabilities = distribution.probabilities
    binary = (
        probabilities["half_win"] == 0
        and probabilities["push"] == 0
        and probabilities["half_loss"] == 0
    )
    return {
        "market_id": market_id,
        "contract_key": contract_key,
        "group": group,
        "label": label,
        "selection": selection,
        "line": line,
        "probability": probabilities["win"] if binary else None,
        "fair_decimal_multiplier": None if math.isinf(fair) else fair,
        "settlement_probabilities": probabilities,
        "market_comparison": None,
    }


def _quarter_lines(start: float, end: float) -> Iterable[float]:
    value = round(start * 4)
    final = round(end * 4)
    while value <= final:
        yield value / 4.0
        value += 1


def _validate_markets(markets: list[dict]) -> None:
    identifiers = [market["market_id"] for market in markets]
    if len(identifiers) != len(set(identifiers)):
        raise PlatformMarketError("Market catalogue contains duplicate identifiers")
