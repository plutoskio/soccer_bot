ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS xg DOUBLE;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS xa DOUBLE;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS npxg DOUBLE;

CREATE TABLE IF NOT EXISTS collection_run (
    collection_run_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    dry_run BOOLEAN NOT NULL DEFAULT false,
    api_football_calls INTEGER NOT NULL DEFAULT 0,
    polymarket_calls INTEGER NOT NULL DEFAULT 0,
    summary JSON,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS collection_checkpoint (
    job_key VARCHAR PRIMARY KEY,
    source_code VARCHAR NOT NULL,
    job_type VARCHAR NOT NULL,
    fixture_source_id VARCHAR,
    scheduled_for TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_http_status INTEGER,
    last_error VARCHAR,
    metadata JSON,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS collection_checkpoint_due_idx
    ON collection_checkpoint (status, scheduled_for);
