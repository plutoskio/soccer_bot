CREATE TABLE IF NOT EXISTS historical_backfill_run (
    run_id VARCHAR PRIMARY KEY,
    manifest_sha256 VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    dry_run BOOLEAN NOT NULL DEFAULT false,
    maximum_batches INTEGER NOT NULL,
    batches_attempted INTEGER NOT NULL DEFAULT 0,
    api_calls INTEGER NOT NULL DEFAULT 0,
    cache_hits INTEGER NOT NULL DEFAULT 0,
    summary JSON,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS historical_backfill_batch_checkpoint (
    manifest_sha256 VARCHAR NOT NULL,
    batch_id VARCHAR NOT NULL,
    batch_fingerprint VARCHAR NOT NULL,
    league_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    fixture_ids JSON NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_run_id VARCHAR,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_http_status INTEGER,
    raw_artifact_id VARCHAR,
    requested_count INTEGER,
    returned_count INTEGER,
    validated_count INTEGER,
    validation JSON,
    last_error VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (manifest_sha256, batch_id)
);

CREATE INDEX IF NOT EXISTS historical_backfill_batch_status_idx
    ON historical_backfill_batch_checkpoint (manifest_sha256, status);
