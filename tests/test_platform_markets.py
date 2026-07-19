from __future__ import annotations

import unittest

from soccer_bot.contracts import ScoreGrid
from soccer_bot.modeling.platform_markets import (
    corner_family_markets,
    first_team_markets,
    moneyline_markets,
    score_family_markets,
)


class PlatformMarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grid = ScoreGrid(
            {
                (0, 0): 0.15,
                (1, 0): 0.25,
                (0, 1): 0.2,
                (1, 1): 0.25,
                (2, 1): 0.15,
            }
        )

    def test_score_catalogue_has_unique_coherent_markets(self) -> None:
        markets = score_family_markets(self.grid)
        identifiers = {row["market_id"] for row in markets}
        self.assertEqual(len(identifiers), len(markets))
        self.assertIn("regulation_both_teams_to_score:yes", identifiers)
        over = next(
            row
            for row in markets
            if row["market_id"] == "regulation_total_goals:over:2.5"
        )
        self.assertAlmostEqual(over["probability"], 0.15)
        self.assertAlmostEqual(over["fair_decimal_multiplier"], 1 / 0.15)

    def test_corner_and_first_team_catalogues(self) -> None:
        corners = corner_family_markets(self.grid)
        self.assertTrue(any(row["contract_key"] == "corner_handicap" for row in corners))
        first = first_team_markets(
            {"home_first": 0.5, "away_first": 0.4, "no_goal": 0.1}
        )
        self.assertEqual(len(first), 3)
        self.assertAlmostEqual(sum(row["probability"] for row in first), 1.0)
        moneyline = moneyline_markets(
            {"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
            home_name="Home",
            away_name="Away",
        )
        self.assertEqual(moneyline[0]["label"], "Home")


if __name__ == "__main__":
    unittest.main()
