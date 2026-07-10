ALTER TABLE lineup_snapshot
    ADD COLUMN IF NOT EXISTS schedule_observation_id VARCHAR;

ALTER TABLE lineup_snapshot
    ADD COLUMN IF NOT EXISTS kickoff_known_at_retrieval TIMESTAMPTZ;

ALTER TABLE lineup_snapshot
    ADD COLUMN IF NOT EXISTS captured_before_kickoff BOOLEAN;

ALTER TABLE lineup_snapshot
    ADD COLUMN IF NOT EXISTS identity_state VARCHAR;

CREATE INDEX IF NOT EXISTS lineup_snapshot_schedule_idx
    ON lineup_snapshot (fixture_id, schedule_observation_id, retrieved_at);

CREATE INDEX IF NOT EXISTS lineup_snapshot_pregame_idx
    ON lineup_snapshot (fixture_id, captured_before_kickoff, retrieved_at);
