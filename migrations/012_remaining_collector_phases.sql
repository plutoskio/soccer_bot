ALTER TABLE prediction_market_event
    ADD COLUMN IF NOT EXISTS fixture_link_method VARCHAR;
ALTER TABLE prediction_market_event
    ADD COLUMN IF NOT EXISTS fixture_link_confidence DOUBLE;
ALTER TABLE prediction_market_event
    ADD COLUMN IF NOT EXISTS fixture_linked_at TIMESTAMPTZ;
ALTER TABLE prediction_market_event
    ADD COLUMN IF NOT EXISTS fixture_link_conflict VARCHAR;

ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS cadence_stage VARCHAR;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS kickoff_known_at_retrieval TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS prediction_market_event_observation (
    event_observation_id VARCHAR PRIMARY KEY,
    prediction_market_event_id VARCHAR NOT NULL,
    raw_artifact_id VARCHAR NOT NULL,
    active BOOLEAN,
    closed BOOLEAN,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    title VARCHAR,
    description VARCHAR,
    resolution_source VARCHAR,
    observed_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_market_event_id, raw_artifact_id)
);

CREATE INDEX IF NOT EXISTS prediction_market_event_observation_time_idx
    ON prediction_market_event_observation
       (prediction_market_event_id, retrieved_at);

CREATE TABLE IF NOT EXISTS prediction_market_observation (
    market_observation_id VARCHAR PRIMARY KEY,
    prediction_market_id VARCHAR NOT NULL,
    raw_artifact_id VARCHAR NOT NULL,
    active BOOLEAN,
    closed BOOLEAN,
    question VARCHAR,
    rules_text VARCHAR,
    volume DOUBLE,
    liquidity DOUBLE,
    observed_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_market_id, raw_artifact_id)
);

CREATE INDEX IF NOT EXISTS prediction_market_observation_time_idx
    ON prediction_market_observation (prediction_market_id, retrieved_at);

CREATE TABLE IF NOT EXISTS collection_health_report (
    report_date DATE PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    collection_run_id VARCHAR,
    severity VARCHAR NOT NULL,
    metrics JSON NOT NULL,
    markdown_path VARCHAR,
    blocking_reason VARCHAR
);

CREATE INDEX IF NOT EXISTS collection_health_report_severity_idx
    ON collection_health_report (severity, generated_at);
