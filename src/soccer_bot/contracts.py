from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
from typing import Mapping

from soccer_bot.config import load_json


SETTLEMENT_OUTCOMES = ("win", "half_win", "push", "half_loss", "loss")
CORE_REGULATION_FAMILIES = {
    "exact_score",
    "moneyline",
    "goal_handicap",
    "total_goals",
    "team_total_goals",
    "both_teams_to_score",
}


class ContractSpecificationError(ValueError):
    """Raised when a contract registry or pricing request is invalid."""


@dataclass(frozen=True)
class ContractDefinition:
    contract_key: str
    family: str
    display_name: str
    eligibility_flag: str
    specification: dict


@dataclass(frozen=True)
class ContractRegistry:
    registry_version: str
    sport: str
    period: str
    contracts: tuple[ContractDefinition, ...]
    specification: dict

    def contract(self, contract_key: str) -> ContractDefinition:
        matches = [
            contract
            for contract in self.contracts
            if contract.contract_key == contract_key
        ]
        if not matches:
            raise KeyError(contract_key)
        return matches[0]


def load_contract_registry(path: Path) -> ContractRegistry:
    return parse_contract_registry(load_json(path))


def parse_contract_registry(specification: dict) -> ContractRegistry:
    if not isinstance(specification, dict):
        raise ContractSpecificationError("Contract registry must be an object")

    registry_version = _required_string(specification, "registry_version")
    sport = _required_string(specification, "sport")
    period = _required_string(specification, "period")
    if sport != "soccer":
        raise ContractSpecificationError("Regulation v1 registry must be soccer")
    if period != "regulation":
        raise ContractSpecificationError(
            "Regulation v1 registry must use regulation period"
        )

    settlement = specification.get("settlement")
    if not isinstance(settlement, dict):
        raise ContractSpecificationError("settlement must be an object")
    expected_settlement = {
        "includes_stoppage_time": True,
        "includes_extra_time": False,
        "includes_penalty_shootout": False,
        "administrative_results_eligible": False,
    }
    for key, expected in expected_settlement.items():
        if settlement.get(key) is not expected:
            raise ContractSpecificationError(
                f"settlement.{key} must be {str(expected).lower()}"
            )

    raw_contracts = specification.get("contracts")
    if not isinstance(raw_contracts, list) or not raw_contracts:
        raise ContractSpecificationError("contracts must be a non-empty list")

    contracts = []
    keys = set()
    families = set()
    for raw_contract in raw_contracts:
        if not isinstance(raw_contract, dict):
            raise ContractSpecificationError("Each contract must be an object")
        contract_key = _required_string(raw_contract, "contract_key")
        if contract_key in keys:
            raise ContractSpecificationError(
                f"Duplicate contract_key: {contract_key}"
            )
        keys.add(contract_key)
        family = _required_string(raw_contract, "family")
        if family not in CORE_REGULATION_FAMILIES:
            raise ContractSpecificationError(f"Unsupported CORE family: {family}")
        if family in families:
            raise ContractSpecificationError(f"Duplicate CORE family: {family}")
        families.add(family)
        eligibility_flag = _required_string(raw_contract, "eligibility_flag")
        if eligibility_flag != "eligible_result_models":
            raise ContractSpecificationError(
                f"{contract_key} must use eligible_result_models"
            )
        if "line_increment" in raw_contract:
            if _as_decimal(raw_contract["line_increment"]) != Decimal("0.25"):
                raise ContractSpecificationError(
                    f"{contract_key} line_increment must be 0.25"
                )
            if tuple(raw_contract.get("settlement_outputs", [])) != SETTLEMENT_OUTCOMES:
                raise ContractSpecificationError(
                    f"{contract_key} must declare all settlement outputs in order"
                )
        if family == "goal_handicap" and raw_contract.get("line_convention") != (
            "handicap_added_to_selected_team_goal_difference"
        ):
            raise ContractSpecificationError(
                f"{contract_key} must declare the selected-team handicap convention"
            )
        contracts.append(
            ContractDefinition(
                contract_key=contract_key,
                family=family,
                display_name=_required_string(raw_contract, "display_name"),
                eligibility_flag=eligibility_flag,
                specification=raw_contract,
            )
        )

    if families != CORE_REGULATION_FAMILIES:
        missing = sorted(CORE_REGULATION_FAMILIES - families)
        extra = sorted(families - CORE_REGULATION_FAMILIES)
        raise ContractSpecificationError(
            f"CORE family mismatch; missing={missing}, extra={extra}"
        )

    states = specification.get("information_states")
    if states != [
        "pre_lineup_72h_clean_v1",
        "pre_lineup_24h_v1",
        "confirmed_lineup_v1",
    ]:
        raise ContractSpecificationError(
            "information_states must declare clean T-72h, T-24h, and confirmed lineups"
        )

    return ContractRegistry(
        registry_version=registry_version,
        sport=sport,
        period=period,
        contracts=tuple(contracts),
        specification=specification,
    )


class ScoreGrid:
    """A validated finite joint home/away regulation-score distribution."""

    def __init__(
        self,
        probabilities: Mapping[tuple[int, int], float],
        *,
        tolerance: float = 1e-9,
    ) -> None:
        if not probabilities:
            raise ContractSpecificationError("Score grid cannot be empty")
        validated: dict[tuple[int, int], float] = {}
        for score, probability in probabilities.items():
            if (
                not isinstance(score, tuple)
                or len(score) != 2
                or isinstance(score[0], bool)
                or isinstance(score[1], bool)
                or not isinstance(score[0], int)
                or not isinstance(score[1], int)
                or score[0] < 0
                or score[1] < 0
            ):
                raise ContractSpecificationError(
                    f"Invalid nonnegative integer score: {score!r}"
                )
            value = float(probability)
            if not math.isfinite(value) or value < 0:
                raise ContractSpecificationError(
                    f"Invalid probability for {score}: {probability!r}"
                )
            validated[score] = value
        total = math.fsum(validated.values())
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=tolerance):
            raise ContractSpecificationError(
                f"Score-grid probability must sum to 1, got {total:.12g}"
            )
        self._probabilities = validated

    @property
    def probabilities(self) -> dict[tuple[int, int], float]:
        return dict(self._probabilities)

    def exact_score(self, home_goals: int, away_goals: int) -> float:
        _validate_nonnegative_int(home_goals, "home_goals")
        _validate_nonnegative_int(away_goals, "away_goals")
        return self._probabilities.get((home_goals, away_goals), 0.0)

    def moneyline(self) -> dict[str, float]:
        result = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
        for (home_goals, away_goals), probability in self._probabilities.items():
            if home_goals > away_goals:
                result["home_win"] += probability
            elif home_goals == away_goals:
                result["draw"] += probability
            else:
                result["away_win"] += probability
        return result

    def both_teams_to_score(self) -> dict[str, float]:
        yes = math.fsum(
            probability
            for (home_goals, away_goals), probability in self._probabilities.items()
            if home_goals > 0 and away_goals > 0
        )
        return {"yes": yes, "no": 1.0 - yes}

    def total_goals(self, *, line: object, selection: str) -> SettlementDistribution:
        if selection not in {"over", "under"}:
            raise ContractSpecificationError("selection must be over or under")
        return self._settlement_distribution(
            values={score: sum(score) for score in self._probabilities},
            line=line,
            direction=selection,
        )

    def team_total_goals(
        self,
        *,
        team: str,
        line: object,
        selection: str,
    ) -> SettlementDistribution:
        if team not in {"home", "away"}:
            raise ContractSpecificationError("team must be home or away")
        if selection not in {"over", "under"}:
            raise ContractSpecificationError("selection must be over or under")
        index = 0 if team == "home" else 1
        return self._settlement_distribution(
            values={score: score[index] for score in self._probabilities},
            line=line,
            direction=selection,
        )

    def goal_handicap(
        self,
        *,
        team: str,
        line: object,
    ) -> SettlementDistribution:
        if team not in {"home", "away"}:
            raise ContractSpecificationError("team must be home or away")
        if team == "home":
            values = {
                score: score[0] - score[1] for score in self._probabilities
            }
        else:
            values = {
                score: score[1] - score[0] for score in self._probabilities
            }
        return self._settlement_distribution(
            values=values,
            line=-_as_decimal(line),
            direction="over",
        )

    def _settlement_distribution(
        self,
        *,
        values: Mapping[tuple[int, int], int],
        line: object,
        direction: str,
    ) -> SettlementDistribution:
        decimal_line = _as_decimal(line)
        legs = _quarter_line_legs(decimal_line)
        probabilities = {outcome: 0.0 for outcome in SETTLEMENT_OUTCOMES}
        for score, probability in self._probabilities.items():
            leg_results = [
                _settle_leg(Decimal(values[score]), leg, direction)
                for leg in legs
            ]
            outcome = _combine_leg_results(leg_results)
            probabilities[outcome] += probability
        return SettlementDistribution(probabilities)


@dataclass(frozen=True)
class SettlementDistribution:
    probabilities: dict[str, float]

    def __post_init__(self) -> None:
        if set(self.probabilities) != set(SETTLEMENT_OUTCOMES):
            raise ContractSpecificationError(
                "Settlement distribution must contain all settlement outcomes"
            )
        values = [self.probabilities[key] for key in SETTLEMENT_OUTCOMES]
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ContractSpecificationError("Invalid settlement probability")
        total = math.fsum(values)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ContractSpecificationError(
                f"Settlement probability must sum to 1, got {total:.12g}"
            )

    def probability(self, outcome: str) -> float:
        if outcome not in SETTLEMENT_OUTCOMES:
            raise KeyError(outcome)
        return self.probabilities[outcome]

    def conditional_win_probability(self) -> float:
        if self.probabilities["half_win"] or self.probabilities["half_loss"]:
            raise ContractSpecificationError(
                "Conditional binary probability is undefined for quarter-line settlements"
            )
        decisive = self.probabilities["win"] + self.probabilities["loss"]
        if decisive == 0:
            raise ContractSpecificationError("Contract has no decisive probability mass")
        return self.probabilities["win"] / decisive

    def fair_decimal_odds(self) -> float:
        win_weight = (
            self.probabilities["win"]
            + 0.5 * self.probabilities["half_win"]
        )
        loss_weight = (
            self.probabilities["loss"]
            + 0.5 * self.probabilities["half_loss"]
        )
        if win_weight == 0:
            return math.inf
        return 1.0 + loss_weight / win_weight


def price_contract(
    score_grid: ScoreGrid,
    contract: ContractDefinition,
    selection: Mapping[str, object],
) -> float | SettlementDistribution:
    """Price one registry-defined contract from a joint score distribution."""

    family = contract.family
    if family == "exact_score":
        return score_grid.exact_score(
            _selection_int(selection, "home_goals"),
            _selection_int(selection, "away_goals"),
        )
    if family == "moneyline":
        outcome = _selection_string(selection, "outcome")
        probabilities = score_grid.moneyline()
        if outcome not in probabilities:
            raise ContractSpecificationError(f"Invalid moneyline outcome: {outcome}")
        return probabilities[outcome]
    if family == "goal_handicap":
        return score_grid.goal_handicap(
            team=_selection_string(selection, "team"),
            line=_selection_value(selection, "line"),
        )
    if family == "total_goals":
        return score_grid.total_goals(
            line=_selection_value(selection, "line"),
            selection=_selection_string(selection, "side"),
        )
    if family == "team_total_goals":
        return score_grid.team_total_goals(
            team=_selection_string(selection, "team"),
            line=_selection_value(selection, "line"),
            selection=_selection_string(selection, "side"),
        )
    if family == "both_teams_to_score":
        outcome = _selection_string(selection, "outcome")
        probabilities = score_grid.both_teams_to_score()
        if outcome not in probabilities:
            raise ContractSpecificationError(f"Invalid BTTS outcome: {outcome}")
        return probabilities[outcome]
    raise ContractSpecificationError(f"Unsupported contract family: {family}")


def _required_string(value: dict, key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ContractSpecificationError(f"{key} must be a non-empty string")
    return result


def _selection_value(selection: Mapping[str, object], key: str) -> object:
    if key not in selection:
        raise ContractSpecificationError(f"Selection requires {key}")
    return selection[key]


def _selection_string(selection: Mapping[str, object], key: str) -> str:
    value = _selection_value(selection, key)
    if not isinstance(value, str) or not value:
        raise ContractSpecificationError(f"Selection {key} must be a string")
    return value


def _selection_int(selection: Mapping[str, object], key: str) -> int:
    value = _selection_value(selection, key)
    _validate_nonnegative_int(value, key)
    return value


def _validate_nonnegative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractSpecificationError(f"{name} must be a nonnegative integer")


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ContractSpecificationError("Line must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ContractSpecificationError(f"Invalid numeric line: {value!r}") from None
    if not result.is_finite():
        raise ContractSpecificationError(f"Invalid numeric line: {value!r}")
    return result


def _quarter_line_legs(line: Decimal) -> tuple[Decimal, ...]:
    quarters = line * 4
    if quarters != quarters.to_integral_value():
        raise ContractSpecificationError("Line must be a multiple of 0.25")
    quarter_value = int(quarters)
    if quarter_value % 2 == 0:
        return (line,)
    return (
        Decimal(quarter_value - 1) / Decimal(4),
        Decimal(quarter_value + 1) / Decimal(4),
    )


def _settle_leg(value: Decimal, line: Decimal, direction: str) -> int:
    difference = value - line
    if direction == "under":
        difference = -difference
    if difference > 0:
        return 1
    if difference < 0:
        return -1
    return 0


def _combine_leg_results(results: list[int]) -> str:
    average = sum(results) / len(results)
    return {
        1.0: "win",
        0.5: "half_win",
        0.0: "push",
        -0.5: "half_loss",
        -1.0: "loss",
    }[average]
