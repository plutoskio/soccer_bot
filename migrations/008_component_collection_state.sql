CREATE TABLE IF NOT EXISTS fixture_collection_component (
    fixture_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    component_code VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    required_for_fixture_terminal BOOLEAN NOT NULL,
    reason_code VARCHAR,
    details JSON,
    first_attempt_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    validated_at TIMESTAMPTZ,
    last_raw_artifact_id VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (fixture_id, source_code, component_code)
);

ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS fixture_id VARCHAR;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS component_code VARCHAR;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS maximum_attempts INTEGER DEFAULT 1;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 2;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS terminal_reason VARCHAR;
ALTER TABLE collection_checkpoint
    ADD COLUMN IF NOT EXISTS last_run_id VARCHAR;

CREATE TABLE IF NOT EXISTS collection_attempt (
    collection_attempt_id VARCHAR PRIMARY KEY,
    job_key VARCHAR NOT NULL,
    collection_run_id VARCHAR NOT NULL,
    attempt_number INTEGER NOT NULL,
    source_code VARCHAR NOT NULL,
    job_type VARCHAR NOT NULL,
    fixture_id VARCHAR,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    http_status INTEGER,
    retry_after_seconds INTEGER,
    quota_cost INTEGER NOT NULL DEFAULT 1,
    raw_artifact_id VARCHAR,
    error_class VARCHAR,
    error_message VARCHAR,
    metadata JSON,
    UNIQUE (job_key, collection_run_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS fixture_collection_component_state_idx
    ON fixture_collection_component (state, updated_at);

CREATE INDEX IF NOT EXISTS fixture_collection_component_fixture_state_idx
    ON fixture_collection_component (fixture_id, state);

CREATE INDEX IF NOT EXISTS collection_checkpoint_schedule_idx
    ON collection_checkpoint (status, next_attempt_at, priority);

CREATE INDEX IF NOT EXISTS collection_checkpoint_fixture_component_idx
    ON collection_checkpoint (fixture_id, component_code, status);

CREATE INDEX IF NOT EXISTS collection_attempt_run_source_idx
    ON collection_attempt (collection_run_id, source_code);

CREATE INDEX IF NOT EXISTS collection_attempt_job_started_idx
    ON collection_attempt (job_key, started_at);
