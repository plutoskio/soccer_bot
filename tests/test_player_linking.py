from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.player_linking import (
    LineupAlias,
    StatCandidate,
    link_team_players,
    score_candidate,
)


def alias(
    index: int, source_id: int, name: str, role: str, shirt: int | None
) -> LineupAlias:
    return LineupAlias(index, str(source_id), name, role, shirt, None)


def candidate(
    player_id: str, source_id: int, name: str, minutes: int | None,
    started: bool, shirt: int | None,
) -> StatCandidate:
    return StatCandidate(
        player_id, (f"{source_id}|identity",), name, minutes, started, shirt, None
    )


class PlayerLinkingTests(unittest.TestCase):
    def test_shirt_conflict_rejects_loose_surname_match(self):
        edge = score_candidate(
            alias(0, 15858, "M. Frokjaer-Jensen", "starter", 29),
            candidate("mathias", 510351, "Mathias Jensen", None, False, 42),
        )
        self.assertEqual("shirt_number_conflict", edge.rejected_reason)

    def test_compound_name_id_shirt_and_role_select_correct_player(self):
        decisions = link_team_players(
            [alias(0, 15858, "M. Frokjaer-Jensen", "starter", 29)],
            [
                candidate("mads", 15858, "Mads Frøkjær", 73, True, 29),
                candidate("mathias", 510351, "Mathias Jensen", None, False, 42),
            ],
        )
        self.assertEqual("mads", decisions[0].player_id)
        self.assertIn("compound_name", decisions[0].evidence)

    def test_different_names_with_only_shirt_and_role_remain_unresolved(self):
        decisions = link_team_players(
            [alias(0, 570158, "R. Pedersen", "substitute", 37)],
            [candidate("raphael", 634890, "Raphael Canut", 17, False, 37)],
        )
        self.assertIsNone(decisions[0].player_id)
        self.assertEqual("raphael", decisions[0].best_candidate_id)

    def test_provider_id_with_incompatible_name_is_rejected(self):
        decisions = link_team_players(
            [alias(0, 144624, "G. Duru", "substitute", 23)],
            [candidate("duru", 144624, "Chidiebube Duru", 31, False, 23)],
        )
        self.assertIsNone(decisions[0].player_id)
        self.assertEqual("provider_id_name_conflict", decisions[0].unresolved_reason)

    def test_provider_id_with_compatible_name_is_accepted(self):
        decisions = link_team_players(
            [alias(0, 144624, "C. Duru", "substitute", 23)],
            [candidate("duru", 144624, "Chidiebube Duru", 31, False, 23)],
        )
        self.assertEqual("duru", decisions[0].player_id)
        self.assertIn("provider_id", decisions[0].evidence)

    def test_recent_same_team_candidate_requires_compatible_name(self):
        decisions = link_team_players(
            [alias(0, 777, "A. Sæther", "starter", None)],
            [StatCandidate(
                "adrian", (), "Adrian Saether", None, None, None, "G",
                recent_same_team=True,
            )],
        )
        self.assertEqual("adrian", decisions[0].player_id)
        self.assertIn("recent_same_team", decisions[0].evidence)

    def test_equal_candidates_are_not_guessed(self):
        decisions = link_team_players(
            [alias(0, 999, "R. Garcia", "substitute", 9)],
            [
                candidate("raul", 1, "Raúl García de Haro", 20, False, 9),
                candidate("ruben", 2, "Rubén García", 20, False, 9),
            ],
        )
        self.assertIsNone(decisions[0].player_id)


if __name__ == "__main__":
    unittest.main()
