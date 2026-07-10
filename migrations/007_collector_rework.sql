CREATE TABLE IF NOT EXISTS fixture_schedule_observation (
    schedule_observation_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    fixture_source_id VARCHAR NOT NULL,
    provider_status VARCHAR,
    canonical_status VARCHAR NOT NULL,
    scheduled_kickoff TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    raw_artifact_id VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    UNIQUE (source_code, fixture_source_id, raw_artifact_id)
);

CREATE INDEX IF NOT EXISTS fixture_schedule_observation_fixture_idx
    ON fixture_schedule_observation (fixture_id, retrieved_at);

CREATE INDEX IF NOT EXISTS fixture_schedule_observation_status_idx
    ON fixture_schedule_observation (canonical_status, scheduled_kickoff);
