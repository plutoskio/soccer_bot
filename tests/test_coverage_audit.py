from __future__ import annotations

import unittest

from soccer_bot.coverage_audit import audit_match, deterministic_sample


def detailed_match(fixture_id: int, *, passing: bool = True) -> dict:
    players = []
    for team_id in (1, 2):
        team_players = []
        for index in range(11):
            team_players.append({
                "player": {"id": team_id * 100 + index},
                "statistics": [{
                    "games": {"minutes": 90, "substitute": False},
                    "goals": {"total": 0, "assists": 0},
                    "shots": {"total": 0, "on": 0},
                    "passes": {
                        "total": 20 if passing else None,
                        "accuracy": 15 if passing else None,
                    },
                }],
            })
        players.append({"team": {"id": team_id}, "players": team_players})
    return {
        "fixture": {"id": fixture_id, "status": {"short": "FT"}},
        "score": {"fulltime": {"home": 1, "away": 0}},
        "lineups": [
            {"startXI": [{} for _ in range(11)]},
            {"startXI": [{} for _ in range(11)]},
        ],
        "statistics": [{"team": {"id": 1}}, {"team": {"id": 2}}],
        "players": players,
    }


class CoverageAuditTests(unittest.TestCase):
    def test_sample_spans_the_season_deterministically(self):
        records = [
            {"fixture": {"id": index, "timestamp": index, "status": {"short": "FT"}}}
            for index in range(100)
        ]
        selected = deterministic_sample(records, 5)
        self.assertEqual([0, 25, 50, 74, 99], [item["fixture"]["id"] for item in selected])

    def test_complete_match_passes(self):
        result = audit_match(detailed_match(1))
        self.assertTrue(result.complete)
        self.assertEqual(22, result.participating_players)
        self.assertEqual(1.0, result.passing_coverage)

    def test_missing_pass_data_fails_without_treating_zero_stats_as_missing(self):
        result = audit_match(detailed_match(1, passing=False))
        self.assertFalse(result.complete)
        self.assertTrue(result.core_player_fields)
        self.assertEqual(0.0, result.passing_coverage)


if __name__ == "__main__":
    unittest.main()
