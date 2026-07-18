ALTER TABLE prediction_market
    ADD COLUMN IF NOT EXISTS fees_enabled BOOLEAN;
ALTER TABLE prediction_market_observation
    ADD COLUMN IF NOT EXISTS fees_enabled BOOLEAN;

ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS book_hash VARCHAR;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS last_trade_price DOUBLE;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS negative_risk BOOLEAN;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS book_complete BOOLEAN;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS capture_target_at TIMESTAMPTZ;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS capture_window_start_at TIMESTAMPTZ;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS capture_deadline_at TIMESTAMPTZ;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS capture_timing_valid BOOLEAN;
ALTER TABLE orderbook_snapshot
    ADD COLUMN IF NOT EXISTS capture_timing_failure_reason VARCHAR;

CREATE TABLE IF NOT EXISTS polymarket_contract_mapping (
    mapping_id VARCHAR PRIMARY KEY,
    prediction_market_id VARCHAR NOT NULL,
    fixture_id VARCHAR NOT NULL,
    mapping_version VARCHAR NOT NULL,
    mapping_policy_sha256 VARCHAR NOT NULL,
    provider_market_type VARCHAR NOT NULL,
    contract_key VARCHAR,
    period VARCHAR,
    parameters JSON,
    mapping_status VARCHAR NOT NULL,
    rejection_reason VARCHAR,
    rules_sha256 VARCHAR NOT NULL,
    mapped_at TIMESTAMPTZ NOT NULL,
    UNIQUE (prediction_market_id, mapping_version),
    CHECK (mapping_status IN ('accepted', 'rejected')),
    CHECK (
        (mapping_status = 'accepted' AND contract_key IS NOT NULL
         AND period IS NOT NULL AND rejection_reason IS NULL)
        OR
        (mapping_status = 'rejected' AND contract_key IS NULL
         AND period IS NULL AND rejection_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS polymarket_contract_outcome_mapping (
    mapping_id VARCHAR NOT NULL,
    outcome_id VARCHAR NOT NULL,
    canonical_selection VARCHAR NOT NULL,
    polarity SMALLINT NOT NULL,
    PRIMARY KEY (mapping_id, outcome_id),
    CHECK (polarity IN (-1, 1))
);

CREATE INDEX IF NOT EXISTS polymarket_contract_mapping_fixture_idx
    ON polymarket_contract_mapping
       (fixture_id, mapping_version, mapping_status, contract_key);
CREATE INDEX IF NOT EXISTS polymarket_contract_outcome_mapping_selection_idx
    ON polymarket_contract_outcome_mapping
       (mapping_id, canonical_selection, polarity);
CREATE INDEX IF NOT EXISTS orderbook_snapshot_evidence_idx
    ON orderbook_snapshot
       (cadence_stage, capture_target_at, retrieved_at, source_token_id);
