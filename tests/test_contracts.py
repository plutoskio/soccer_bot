from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.config import load_json
from soccer_bot.contracts import (
    ContractSpecificationError,
    ScoreGrid,
    load_contract_registry,
    parse_contract_registry,
    price_contract,
)


class ContractRegistryTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / "config" / "contracts" / "regulation_v1.json"

    def test_core_regulation_registry_is_complete_and_valid(self):
        registry = load_contract_registry(self.path)

        self.assertEqual(registry.registry_version, "regulation_v1")
        self.assertEqual(registry.sport, "soccer")
        self.assertEqual(registry.period, "regulation")
        self.assertEqual(
            {contract.contract_key for contract in registry.contracts},
            {
                "regulation_exact_score",
                "regulation_moneyline",
                "regulation_goal_handicap",
                "regulation_total_goals",
                "regulation_team_total_goals",
                "regulation_both_teams_to_score",
            },
        )
        self.assertEqual(
            registry.contract("regulation_moneyline").eligibility_flag,
            "eligible_result_models",
        )

    def test_duplicate_or_missing_core_family_is_rejected(self):
        specification = load_json(self.path)
        specification["contracts"][-1] = copy.deepcopy(
            specification["contracts"][0]
        )
        specification["contracts"][-1]["contract_key"] = "duplicate-family"

        with self.assertRaisesRegex(
            ContractSpecificationError, "Duplicate CORE family"
        ):
            parse_contract_registry(specification)

    def test_extra_time_cannot_be_enabled_in_regulation_registry(self):
        specification = load_json(self.path)
        specification["settlement"]["includes_extra_time"] = True

        with self.assertRaisesRegex(
            ContractSpecificationError,
            "includes_extra_time must be false",
        ):
            parse_contract_registry(specification)


class ScoreGridPricingTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_contract_registry(
            ROOT / "config" / "contracts" / "regulation_v1.json"
        )
        self.grid = ScoreGrid(
            {
                (0, 0): 0.10,
                (1, 0): 0.20,
                (0, 1): 0.15,
                (1, 1): 0.25,
                (2, 0): 0.10,
                (0, 2): 0.05,
                (2, 1): 0.10,
                (1, 2): 0.05,
            }
        )

    def assertProbabilitiesAlmostEqual(self, actual, expected):
        self.assertEqual(set(actual), set(expected))
        for key, value in expected.items():
            self.assertAlmostEqual(actual[key], value, places=12, msg=key)

    def test_exact_score_moneyline_and_btts_are_coherent(self):
        self.assertAlmostEqual(self.grid.exact_score(1, 1), 0.25)
        self.assertEqual(self.grid.exact_score(9, 9), 0.0)
        self.assertProbabilitiesAlmostEqual(
            self.grid.moneyline(),
            {"home_win": 0.40, "draw": 0.35, "away_win": 0.25},
        )
        self.assertProbabilitiesAlmostEqual(
            self.grid.both_teams_to_score(),
            {"yes": 0.40, "no": 0.60},
        )
        self.assertAlmostEqual(sum(self.grid.moneyline().values()), 1.0)

    def test_half_line_match_and_team_totals_have_no_push(self):
        over = self.grid.total_goals(line=1.5, selection="over")
        under = self.grid.total_goals(line=1.5, selection="under")
        self.assertProbabilitiesAlmostEqual(
            over.probabilities,
            {
                "win": 0.55,
                "half_win": 0.0,
                "push": 0.0,
                "half_loss": 0.0,
                "loss": 0.45,
            },
        )
        self.assertAlmostEqual(over.probability("win"), 1 - under.probability("win"))

        home_over = self.grid.team_total_goals(
            team="home", line=0.5, selection="over"
        )
        self.assertAlmostEqual(home_over.probability("win"), 0.70)
        self.assertAlmostEqual(home_over.probability("loss"), 0.30)

    def test_integer_handicap_preserves_push_probability(self):
        home_minus_one = self.grid.goal_handicap(team="home", line=-1)
        self.assertProbabilitiesAlmostEqual(
            home_minus_one.probabilities,
            {
                "win": 0.10,
                "half_win": 0.0,
                "push": 0.30,
                "half_loss": 0.0,
                "loss": 0.60,
            },
        )
        self.assertAlmostEqual(
            home_minus_one.conditional_win_probability(),
            0.10 / 0.70,
        )

    def test_quarter_total_is_split_into_adjacent_lines(self):
        over = self.grid.total_goals(line=1.25, selection="over")
        under = self.grid.total_goals(line=1.25, selection="under")
        self.assertProbabilitiesAlmostEqual(
            over.probabilities,
            {
                "win": 0.55,
                "half_win": 0.0,
                "push": 0.0,
                "half_loss": 0.35,
                "loss": 0.10,
            },
        )
        self.assertProbabilitiesAlmostEqual(
            under.probabilities,
            {
                "win": 0.10,
                "half_win": 0.35,
                "push": 0.0,
                "half_loss": 0.0,
                "loss": 0.55,
            },
        )
        self.assertAlmostEqual(over.fair_decimal_odds(), 1.5)
        with self.assertRaisesRegex(
            ContractSpecificationError, "undefined for quarter-line"
        ):
            over.conditional_win_probability()

    def test_invalid_grid_and_line_fail_closed(self):
        with self.assertRaisesRegex(ContractSpecificationError, "sum to 1"):
            ScoreGrid({(0, 0): 0.9})
        with self.assertRaisesRegex(ContractSpecificationError, "nonnegative"):
            ScoreGrid({(-1, 0): 1.0})
        with self.assertRaisesRegex(ContractSpecificationError, "multiple of 0.25"):
            self.grid.total_goals(line=2.1, selection="over")

    def test_registry_contracts_route_to_the_shared_score_grid(self):
        exact = price_contract(
            self.grid,
            self.registry.contract("regulation_exact_score"),
            {"home_goals": 1, "away_goals": 1},
        )
        moneyline = price_contract(
            self.grid,
            self.registry.contract("regulation_moneyline"),
            {"outcome": "home_win"},
        )
        handicap = price_contract(
            self.grid,
            self.registry.contract("regulation_goal_handicap"),
            {"team": "home", "line": -1},
        )
        total = price_contract(
            self.grid,
            self.registry.contract("regulation_total_goals"),
            {"side": "over", "line": 1.5},
        )
        team_total = price_contract(
            self.grid,
            self.registry.contract("regulation_team_total_goals"),
            {"team": "home", "side": "over", "line": 0.5},
        )
        btts = price_contract(
            self.grid,
            self.registry.contract("regulation_both_teams_to_score"),
            {"outcome": "yes"},
        )

        self.assertAlmostEqual(exact, 0.25)
        self.assertAlmostEqual(moneyline, 0.40)
        self.assertAlmostEqual(handicap.probability("push"), 0.30)
        self.assertAlmostEqual(total.probability("win"), 0.55)
        self.assertAlmostEqual(team_total.probability("win"), 0.70)
        self.assertAlmostEqual(btts, 0.40)


if __name__ == "__main__":
    unittest.main()
