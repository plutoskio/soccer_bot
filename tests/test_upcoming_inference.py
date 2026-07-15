from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.datasets.features import load_team_state_feature_config
from soccer_bot.datasets.upcoming import load_upcoming_inference_fixtures


class UpcomingInferenceLoaderTests(unittest.TestCase):
    def setUp(self):
        self.config = load_team_state_feature_config(
            ROOT / "config" / "features" / "regulation_team_state_v1.json"
        )
        self.as_of = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
        self.kickoff = self.as_of + timedelta(hours=72)
        self.connection = duckdb.connect(":memory:")
        self.connection.execute(
            """
            CREATE TABLE fixture(
                fixture_id VARCHAR, competition_id VARCHAR, season_id VARCHAR,
                home_team_id VARCHAR, away_team_id VARCHAR,
                neutral_venue BOOLEAN, scheduled_kickoff TIMESTAMPTZ,
                status VARCHAR
            );
            CREATE TABLE competition(competition_id VARCHAR, name VARCHAR);
            CREATE TABLE team(team_id VARCHAR, name VARCHAR);
            CREATE TABLE fixture_schedule_observation(
                fixture_id VARCHAR, retrieved_at TIMESTAMPTZ,
                scheduled_kickoff TIMESTAMPTZ, canonical_status VARCHAR,
                schedule_observation_id VARCHAR
            );
            INSERT INTO competition VALUES ('competition', 'League');
            INSERT INTO team VALUES ('home', 'Home'), ('away', 'Away');
            """
        )
        self.connection.execute(
            """
            INSERT INTO fixture VALUES (
                'fixture','competition','season','home','away',false,?,'scheduled'
            )
            """,
            [self.kickoff],
        )

    def tearDown(self):
        self.connection.close()

    def test_schedule_must_be_known_by_the_exact_prediction_cutoff(self):
        self.connection.execute(
            """
            INSERT INTO fixture_schedule_observation VALUES (
                'fixture',?,?, 'scheduled','observation'
            )
            """,
            [self.as_of - timedelta(hours=1), self.kickoff],
        )

        fixtures, metadata, audit = load_upcoming_inference_fixtures(
            self.connection,
            as_of=self.as_of,
            lookahead_days=7,
            feature_config=self.config,
        )

        self.assertEqual(
            fixtures[0].allowed_information_states,
            ("pre_lineup_72h_clean_v1",),
        )
        self.assertEqual(metadata["fixture"].home_team_name, "Home")
        by_horizon = {
            row["information_state"]: row for row in audit["horizons"]
        }
        self.assertEqual(
            by_horizon["pre_lineup_24h_v1"]["reason"], "horizon_not_due"
        )

    def test_latest_pre_cutoff_kickoff_mismatch_fails_closed(self):
        self.connection.execute(
            """
            INSERT INTO fixture_schedule_observation VALUES
                ('fixture',?,?, 'scheduled','one'),
                ('fixture',?,?, 'scheduled','two')
            """,
            [
                self.as_of - timedelta(hours=2),
                self.kickoff,
                self.as_of - timedelta(hours=1),
                self.kickoff + timedelta(hours=1),
            ],
        )

        fixtures, _, audit = load_upcoming_inference_fixtures(
            self.connection,
            as_of=self.as_of,
            lookahead_days=7,
            feature_config=self.config,
        )

        self.assertEqual(fixtures[0].allowed_information_states, ())
        eligible = next(
            row
            for row in audit["horizons"]
            if row["information_state"] == "pre_lineup_72h_clean_v1"
        )
        self.assertEqual(
            eligible["reason"], "observed_kickoff_differs_at_prediction_at"
        )


if __name__ == "__main__":
    unittest.main()
