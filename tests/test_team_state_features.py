from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.features import (
    ChronologicalTeamStateBuilder,
    RegulationInferenceFixture,
    feature_rows_sha256,
    load_team_state_feature_config,
)
from soccer_bot.datasets.targets import RegulationScoreTarget


BASELINE_GOALS = 1.25


def target(
    fixture_id: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_goals: int,
    away_goals: int,
    *,
    competition: str = "competition",
    neutral: bool = False,
) -> RegulationScoreTarget:
    result = (
        "home_win"
        if home_goals > away_goals
        else "draw" if home_goals == away_goals else "away_win"
    )
    return RegulationScoreTarget(
        fixture_id=fixture_id,
        competition_id=competition,
        season_id="season",
        home_team_id=home,
        away_team_id=away,
        neutral_venue=neutral,
        kickoff=kickoff,
        prediction_at=kickoff - timedelta(hours=24),
        home_goals=home_goals,
        away_goals=away_goals,
        result=result,
        total_goals=home_goals + away_goals,
        goal_difference=home_goals - away_goals,
        both_teams_to_score=home_goals > 0 and away_goals > 0,
        agreeing_source_codes=("test",),
    )


class ChronologicalTeamStateTests(unittest.TestCase):
    def setUp(self):
        self.config = load_team_state_feature_config(
            ROOT / "config" / "features" / "regulation_team_state_v1.json"
        )
        self.start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)

    def build(self, targets):
        return ChronologicalTeamStateBuilder(self.config).build(targets)

    def row(self, rows, fixture_id, information_state="pre_lineup_24h_v1"):
        return next(
            row
            for row in rows
            if row.fixture_id == fixture_id
            and row.information_state == information_state
        )

    def feature_signature(self, row):
        return (
            row.home_attack_mean,
            row.home_attack_std,
            row.home_defense_mean,
            row.home_defense_std,
            row.away_attack_mean,
            row.away_attack_std,
            row.away_defense_mean,
            row.away_defense_std,
            row.expected_home_goals,
            row.expected_away_goals,
            row.home_history_matches,
            row.away_history_matches,
            row.competition_history_matches,
        )

    def test_target_result_cannot_change_its_own_features(self):
        original = target("match", self.start, "A", "B", 0, 0)
        changed = replace(
            original,
            home_goals=5,
            away_goals=0,
            result="home_win",
            total_goals=5,
            goal_difference=5,
        )

        original_row = self.row(self.build([original]), "match")
        changed_row = self.row(self.build([changed]), "match")

        self.assertEqual(
            self.feature_signature(original_row),
            self.feature_signature(changed_row),
        )

    def test_future_result_cannot_change_earlier_features_but_changes_later_state(self):
        first = target("first", self.start, "A", "B", 1, 1)
        second = target("second", self.start + timedelta(days=7), "A", "C", 0, 0)
        third = target("third", self.start + timedelta(days=14), "A", "D", 1, 0)

        baseline_rows = self.build([first, second, third])
        changed_rows = self.build(
            [first, replace(second, home_goals=5, total_goals=5, goal_difference=5), third]
        )

        self.assertEqual(
            self.feature_signature(self.row(baseline_rows, "second")),
            self.feature_signature(self.row(changed_rows, "second")),
        )
        self.assertNotEqual(
            self.feature_signature(self.row(baseline_rows, "third")),
            self.feature_signature(self.row(changed_rows, "third")),
        )

    def test_clean_72h_is_omitted_when_either_team_has_an_intervening_match(self):
        intervening = target(
            "intervening", self.start + timedelta(days=8), "A", "X", 1, 0
        )
        blocked = target("blocked", self.start + timedelta(days=10), "A", "B", 1, 1)
        clean = target("clean", self.start + timedelta(days=10), "C", "D", 0, 0)

        rows = self.build([intervening, blocked, clean])
        states = {
            row.information_state for row in rows if row.fixture_id == "blocked"
        }
        clean_states = {
            row.information_state for row in rows if row.fixture_id == "clean"
        }

        self.assertEqual(states, {"pre_lineup_24h_v1"})
        self.assertEqual(
            clean_states,
            {"pre_lineup_72h_clean_v1", "pre_lineup_24h_v1"},
        )

    def test_fixture_kicking_off_exactly_at_72h_anchor_blocks_clean_horizon(self):
        prior = target("prior", self.start + timedelta(days=7), "A", "X", 1, 0)
        current = target("current", self.start + timedelta(days=10), "A", "B", 1, 1)

        rows = self.build([prior, current])
        states = {
            row.information_state for row in rows if row.fixture_id == "current"
        }

        self.assertEqual(states, {"pre_lineup_24h_v1"})

    def test_simultaneous_results_are_batched_and_input_order_is_irrelevant(self):
        first = target("one", self.start, "A", "B", 4, 0)
        second = target("two", self.start, "C", "D", 0, 3)
        later = target("later", self.start + timedelta(days=7), "A", "C", 1, 1)

        forward = self.build([first, second, later])
        reverse = self.build([later, second, first])

        self.assertEqual(feature_rows_sha256(forward), feature_rows_sha256(reverse))
        self.assertEqual(self.row(forward, "one").competition_history_matches, 0)
        self.assertEqual(self.row(forward, "two").competition_history_matches, 0)
        self.assertEqual(self.row(forward, "later").competition_history_matches, 2)

    def test_reusing_builder_does_not_carry_state_between_builds(self):
        first = target("first", self.start, "A", "B", 4, 0)
        later = target("later", self.start + timedelta(days=7), "A", "C", 1, 1)
        builder = ChronologicalTeamStateBuilder(self.config)

        first_hash = feature_rows_sha256(builder.build([first, later]))
        second_hash = feature_rows_sha256(builder.build([first, later]))

        self.assertEqual(first_hash, second_hash)

    def test_result_is_not_available_until_configured_delay_has_elapsed(self):
        target_kickoff = self.start + timedelta(days=3)
        cutoff = target_kickoff - timedelta(hours=24)
        recent = target("recent", cutoff - timedelta(hours=1), "A", "X", 2, 0)
        current = target("current", target_kickoff, "A", "B", 1, 0)

        current_row = self.row(self.build([recent, current]), "current")

        self.assertEqual(current_row.home_history_matches, 0)
        self.assertIsNone(current_row.home_rest_days)

    def test_strong_result_updates_attack_defense_and_reduces_uncertainty(self):
        first = target("first", self.start, "A", "B", 4, 0)
        later = target("later", self.start + timedelta(days=7), "A", "C", 1, 0)

        later_row = self.row(self.build([first, later]), "later")

        self.assertGreater(later_row.home_attack_mean, 0)
        self.assertLess(later_row.home_attack_std, self.config.team_prior_variance ** 0.5)
        self.assertGreater(later_row.expected_home_goals, BASELINE_GOALS)
        self.assertEqual(later_row.home_history_matches, 1)
        self.assertAlmostEqual(later_row.home_rest_days, 7.0)
        self.assertEqual(later_row.home_matches_last_7d, 1)

    def test_neutral_venue_removes_home_advantage(self):
        neutral = target("neutral", self.start, "A", "B", 1, 0, neutral=True)

        row = self.row(self.build([neutral]), "neutral")

        self.assertEqual(row.applied_home_advantage, 0.0)
        self.assertAlmostEqual(row.expected_home_goals, row.expected_away_goals)

    def test_inference_rows_have_no_fake_outcome_and_emit_only_due_horizons(self):
        historical = target("history", self.start, "A", "B", 4, 0)
        kickoff = self.start + timedelta(days=10)
        fixture = RegulationInferenceFixture(
            fixture_id="upcoming",
            competition_id="competition",
            season_id="season",
            home_team_id="A",
            away_team_id="C",
            neutral_venue=False,
            kickoff=kickoff,
            allowed_information_states=(
                "pre_lineup_72h_clean_v1",
                "pre_lineup_24h_v1",
            ),
        )

        rows = ChronologicalTeamStateBuilder(self.config).build_inference(
            [historical],
            [fixture],
            as_of=kickoff - timedelta(hours=48),
        )

        self.assertEqual(
            {row.information_state for row in rows},
            {"pre_lineup_72h_clean_v1"},
        )
        self.assertFalse(hasattr(rows[0], "home_goals"))
        self.assertEqual(rows[0].home_history_matches, 1)
        self.assertGreater(rows[0].expected_home_goals, BASELINE_GOALS)


if __name__ == "__main__":
    unittest.main()
