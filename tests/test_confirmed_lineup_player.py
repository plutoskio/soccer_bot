from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from soccer_bot.datasets.players import (
    ConfirmedLineupFixture,
    ConfirmedLineupPlayer,
    PlayerMatchTarget,
    load_first_valid_confirmed_lineups,
)
from soccer_bot.modeling.player_hierarchy import (
    PlayerModelError,
    evaluate_player_components,
    fit_confirmed_lineup_player_model,
    load_confirmed_lineup_player_model,
    load_player_hierarchy_config,
    player_model_sha256,
    predict_confirmed_lineup,
)


ROOT = Path(__file__).resolve().parents[1]


class ConfirmedLineupPlayerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_player_hierarchy_config(
            ROOT / "config" / "models" / "confirmed_lineup_player_v1.json"
        )
        self.kickoff = datetime(2020, 1, 1, 15, tzinfo=timezone.utc)

    def _row(
        self,
        player_id: str,
        position: str,
        *,
        team: str = "home",
        fixture: str = "history",
        kickoff: datetime | None = None,
        minutes: int = 90,
        goals: int = 0,
        assists: int = 0,
        started: bool = True,
    ) -> PlayerMatchTarget:
        at = kickoff or self.kickoff
        return PlayerMatchTarget(
            fixture_id=fixture,
            competition_id="competition",
            season_id="season",
            kickoff=at,
            result_available_at=at + timedelta(minutes=150),
            team_id=team,
            opponent_team_id="away" if team == "home" else "home",
            is_home=team == "home",
            player_id=player_id,
            position_code=position,
            started=started,
            minutes_played=minutes,
            goals=goals,
            assists=assists,
            team_goals=2,
        )

    def _training_rows(self):
        positions = ["G"] + ["D"] * 4 + ["M"] * 4 + ["F"] * 2
        rows = []
        for team in ("home", "away"):
            for index, position in enumerate(positions):
                rows.append(
                    self._row(
                        f"{team}-{index}",
                        position,
                        team=team,
                        goals=int(position == "F" and index == 9),
                        assists=int(position == "M" and index == 7),
                    )
                )
        return rows

    def test_player_allocations_reconcile_exactly_to_team_rates(self):
        model = fit_confirmed_lineup_player_model(self._training_rows(), self.config)
        players = []
        for row in self._training_rows():
            players.append(
                ConfirmedLineupPlayer(
                    player_id=row.player_id,
                    team_id=row.team_id,
                    selection_role="starter",
                    position_code=row.position_code,
                )
            )
        players.append(ConfirmedLineupPlayer("home-bench", "home", "substitute", "F"))
        players.append(ConfirmedLineupPlayer("away-bench", "away", "substitute", "M"))
        kickoff = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
        lineup = ConfirmedLineupFixture(
            fixture_id="future",
            competition_id="competition",
            season_id="future-season",
            home_team_id="home",
            away_team_id="away",
            kickoff=kickoff,
            prediction_at=kickoff - timedelta(minutes=45),
            raw_artifact_id="raw",
            schedule_observation_id="schedule",
            players=tuple(players),
        )
        prediction = predict_confirmed_lineup(
            lineup,
            model,
            self.config,
            base_prediction_at=kickoff - timedelta(hours=24),
            base_model_version="regulation_champion_v1",
            base_model_sha256="a" * 64,
            base_home_expected_goals=2.0,
            base_away_expected_goals=1.0,
        )
        self.assertAlmostEqual(
            prediction.home.player_expected_goals_sum
            + prediction.home.residual_expected_goals,
            2.0,
            places=12,
        )
        self.assertAlmostEqual(
            prediction.away.player_expected_goals_sum
            + prediction.away.residual_expected_goals,
            1.0,
            places=12,
        )
        self.assertAlmostEqual(
            prediction.home.player_expected_assists_sum
            + prediction.home.expected_unassisted_goals,
            2.0,
            places=12,
        )
        self.assertFalse(prediction.home.authorized_to_replace_champion_rate)
        bench = next(row for row in prediction.players if row.player_id == "home-bench")
        self.assertIsNone(bench.anytime_goal_probability)
        self.assertIn("substitute_appearance_target_not_semantically_validated", bench.warnings)
        for row in prediction.players:
            if row.selection_role == "starter":
                self.assertAlmostEqual(sum(row.goal_count_probabilities_0_1_2_3_plus), 1.0)
                self.assertAlmostEqual(sum(row.assist_count_probabilities_0_1_2_3_plus), 1.0)
                self.assertGreaterEqual(row.score_or_assist_probability, row.anytime_goal_probability)

    def test_lineup_and_base_cutoffs_are_strict(self):
        model = fit_confirmed_lineup_player_model(self._training_rows(), self.config)
        kickoff = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
        lineup = ConfirmedLineupFixture(
            "future", "competition", None, "home", "away", kickoff, kickoff,
            "raw", "schedule",
            tuple(
                ConfirmedLineupPlayer(
                    row.player_id, row.team_id, "starter", row.position_code
                )
                for row in self._training_rows()
            ),
        )
        with self.assertRaises(PlayerModelError):
            predict_confirmed_lineup(
                lineup, model, self.config,
                base_prediction_at=kickoff - timedelta(hours=24),
                base_model_version="base", base_model_sha256="a" * 64,
                base_home_expected_goals=1.0, base_away_expected_goals=1.0,
            )
        before = replace(lineup, prediction_at=kickoff - timedelta(minutes=45))
        with self.assertRaises(PlayerModelError):
            predict_confirmed_lineup(
                before, model, self.config,
                base_prediction_at=before.prediction_at,
                base_model_version="base", base_model_sha256="a" * 64,
                base_home_expected_goals=1.0, base_away_expected_goals=1.0,
            )

    def test_target_outcome_cannot_change_its_own_diagnostic_prediction(self):
        config = replace(
            self.config,
            diagnostic_start=datetime(2021, 1, 1, tzinfo=timezone.utc),
            forbidden_prospective_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        warmup = self._training_rows()
        target_time = datetime(2022, 1, 1, 15, tzinfo=timezone.utc)
        target = self._row(
            "home-9", "F", fixture="target", kickoff=target_time, goals=0
        )
        low, _ = evaluate_player_components(warmup + [target], config)
        high, _ = evaluate_player_components(
            warmup + [replace(target, goals=5)], config
        )
        low_target = next(row for row in low if row.fixture_id == "target")
        high_target = next(row for row in high if row.fixture_id == "target")
        self.assertEqual(low_target.goal_probability, high_target.goal_probability)
        self.assertEqual(low_target.expected_minutes, high_target.expected_minutes)

    def test_model_round_trip_and_hash_tamper_detection(self):
        model = fit_confirmed_lineup_player_model(self._training_rows(), self.config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            value = asdict(model)
            value["fit_end_exclusive"] = model.fit_end_exclusive.isoformat()
            path.write_text(
                json.dumps(
                    {"logical_model_sha256": player_model_sha256(model), "model": value}
                )
            )
            loaded = load_confirmed_lineup_player_model(path)
            self.assertEqual(player_model_sha256(loaded), player_model_sha256(model))
            tampered = json.loads(path.read_text())
            tampered["model"]["assisted_goal_probability"] = 0.99
            path.write_text(json.dumps(tampered))
            with self.assertRaises(PlayerModelError):
                load_confirmed_lineup_player_model(path)

    def test_packaged_production_shadow_identity_is_frozen(self):
        model = load_confirmed_lineup_player_model(
            ROOT
            / "artifacts"
            / "production"
            / "confirmed_lineup_player_v1"
            / "model.json.gz"
        )
        self.assertEqual(model.model_version, self.config.model_version)
        self.assertEqual(
            player_model_sha256(model),
            "bca9a13af829032b43de9e7cbbd94e070f36fcfbda76675972565748b8e8963a",
        )
        self.assertFalse(model.apply_to_public_champion)
        self.assertLessEqual(
            model.fit_end_exclusive, self.config.forbidden_prospective_start
        )

    def test_only_strictly_pregame_two_team_lineup_is_loaded(self):
        connection = duckdb.connect(":memory:")
        connection.execute(
            """
            CREATE TABLE fixture(fixture_id VARCHAR, competition_id VARCHAR,
                season_id VARCHAR, home_team_id VARCHAR, away_team_id VARCHAR,
                scheduled_kickoff TIMESTAMPTZ);
            CREATE TABLE fixture_schedule_observation(schedule_observation_id VARCHAR,
                fixture_id VARCHAR, scheduled_kickoff TIMESTAMPTZ);
            CREATE TABLE lineup_snapshot(lineup_snapshot_id VARCHAR, fixture_id VARCHAR,
                team_id VARCHAR, raw_artifact_id VARCHAR, schedule_observation_id VARCHAR,
                retrieved_at TIMESTAMPTZ, lineup_type VARCHAR, is_complete BOOLEAN,
                captured_before_kickoff BOOLEAN, identity_state VARCHAR);
            CREATE TABLE lineup_player(lineup_snapshot_id VARCHAR, player_id VARCHAR,
                selection_role VARCHAR, position_code VARCHAR);
            CREATE TABLE player(player_id VARCHAR, primary_position VARCHAR);
            CREATE TABLE player_identity_state(player_id VARCHAR, is_identity_placeholder BOOLEAN);
            """
        )
        kickoff = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
        connection.execute(
            "INSERT INTO fixture VALUES ('f','c','s','h','a',?)", [kickoff]
        )
        connection.execute(
            "INSERT INTO fixture_schedule_observation VALUES ('schedule','f',?)",
            [kickoff],
        )
        positions = ["G"] + ["D"] * 4 + ["M"] * 4 + ["F"] * 2
        for team in ("h", "a"):
            snapshot = f"snapshot-{team}"
            connection.execute(
                "INSERT INTO lineup_snapshot VALUES (?,?,?,?,?,?, 'confirmed',true,true,'resolved')",
                [snapshot, "f", team, "raw", "schedule", kickoff - timedelta(minutes=45)],
            )
            for index, position in enumerate(positions):
                player = f"{team}-{index}"
                connection.execute("INSERT INTO player VALUES (?,?)", [player, position])
                connection.execute(
                    "INSERT INTO lineup_player VALUES (?,?, 'starter',?)",
                    [snapshot, player, position],
                )
        loaded = load_first_valid_confirmed_lineups(
            connection, as_of=kickoff - timedelta(minutes=30)
        )
        self.assertEqual(len(loaded), 1)
        self.assertEqual(len(loaded[0].players), 22)
        self.assertEqual(
            load_first_valid_confirmed_lineups(
                connection, as_of=kickoff
            ),
            [],
        )
        connection.execute(
            "UPDATE lineup_snapshot SET captured_before_kickoff=false"
        )
        self.assertEqual(
            load_first_valid_confirmed_lineups(
                connection, as_of=kickoff - timedelta(minutes=30)
            ),
            [],
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
