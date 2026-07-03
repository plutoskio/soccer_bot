CREATE TABLE IF NOT EXISTS schema_migration (
    version VARCHAR PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS source (
    source_code VARCHAR PRIMARY KEY,
    source_name VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    base_url VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS raw_artifact (
    raw_artifact_id VARCHAR PRIMARY KEY,
    source_code VARCHAR NOT NULL,
    resource_name VARCHAR NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    request_url VARCHAR,
    request_parameters JSON,
    http_status INTEGER,
    response_headers JSON,
    content_sha256 VARCHAR NOT NULL,
    uncompressed_bytes BIGINT,
    data_path VARCHAR NOT NULL,
    metadata_path VARCHAR NOT NULL,
    duplicate_content BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS competition (
    competition_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    country_code VARCHAR,
    competition_type VARCHAR,
    gender VARCHAR DEFAULT 'male',
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS season (
    season_id VARCHAR PRIMARY KEY,
    competition_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS team (
    team_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    team_type VARCHAR NOT NULL,
    country_code VARCHAR,
    gender VARCHAR DEFAULT 'male',
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS player (
    player_id VARCHAR PRIMARY KEY,
    full_name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    birth_date DATE,
    nationality_code VARCHAR,
    primary_position VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS source_entity_map (
    source_code VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    source_entity_id VARCHAR NOT NULL,
    internal_entity_id VARCHAR NOT NULL,
    source_name VARCHAR,
    match_method VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    review_status VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (source_code, entity_type, source_entity_id)
);

CREATE TABLE IF NOT EXISTS fixture (
    fixture_id VARCHAR PRIMARY KEY,
    competition_id VARCHAR,
    season_id VARCHAR,
    home_team_id VARCHAR NOT NULL,
    away_team_id VARCHAR NOT NULL,
    scheduled_kickoff TIMESTAMPTZ,
    venue_name VARCHAR,
    neutral_venue BOOLEAN,
    stage VARCHAR,
    round_name VARCHAR,
    status VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS fixture_result_observation (
    observation_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    observed_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    home_score_regulation INTEGER,
    away_score_regulation INTEGER,
    halftime_home_score INTEGER,
    halftime_away_score INTEGER,
    home_score_extra_time INTEGER,
    away_score_extra_time INTEGER,
    home_score_penalties INTEGER,
    away_score_penalties INTEGER,
    result_status VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_snapshot (
    lineup_snapshot_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    team_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    lineup_type VARCHAR NOT NULL,
    formation VARCHAR,
    observed_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    is_complete BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup_player (
    lineup_snapshot_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    selection_role VARCHAR NOT NULL,
    position_code VARCHAR,
    formation_grid VARCHAR,
    shirt_number INTEGER,
    captain BOOLEAN,
    goalkeeper BOOLEAN,
    PRIMARY KEY (lineup_snapshot_id, player_id)
);

CREATE TABLE IF NOT EXISTS appearance (
    appearance_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    team_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    started BOOLEAN,
    minutes_played INTEGER,
    position_code VARCHAR,
    shirt_number INTEGER,
    rating DOUBLE,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS match_event (
    match_event_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    team_id VARCHAR,
    player_id VARCHAR,
    secondary_player_id VARCHAR,
    source_code VARCHAR NOT NULL,
    source_event_id VARCHAR,
    raw_artifact_id VARCHAR,
    event_type VARCHAR NOT NULL,
    event_detail VARCHAR,
    period INTEGER,
    minute INTEGER,
    added_minute INTEGER,
    second INTEGER,
    x DOUBLE,
    y DOUBLE,
    end_x DOUBLE,
    end_y DOUBLE,
    xg_value DOUBLE,
    event_data JSON,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS team_match_stat_observation (
    observation_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    team_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    period VARCHAR NOT NULL DEFAULT 'regulation',
    shots INTEGER,
    shots_on_target INTEGER,
    xg DOUBLE,
    possession_pct DOUBLE,
    corners INTEGER,
    fouls INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    passes INTEGER,
    accurate_passes INTEGER,
    statistics JSON,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS player_match_stat_observation (
    observation_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    team_id VARCHAR NOT NULL,
    player_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    minutes_played INTEGER,
    started BOOLEAN,
    position_code VARCHAR,
    goals INTEGER,
    assists INTEGER,
    shots INTEGER,
    shots_on_target INTEGER,
    key_passes INTEGER,
    passes INTEGER,
    accurate_passes INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    penalties_scored INTEGER,
    statistics JSON,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS player_season_stat (
    observation_id VARCHAR PRIMARY KEY,
    player_id VARCHAR NOT NULL,
    team_id VARCHAR,
    competition_id VARCHAR NOT NULL,
    season_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    games INTEGER,
    minutes INTEGER,
    goals INTEGER,
    assists INTEGER,
    shots INTEGER,
    key_passes INTEGER,
    xg DOUBLE,
    xa DOUBLE,
    npg INTEGER,
    npxg DOUBLE,
    xg_chain DOUBLE,
    xg_buildup DOUBLE,
    position VARCHAR,
    statistics JSON,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS bookmaker_quote (
    quote_id VARCHAR PRIMARY KEY,
    fixture_id VARCHAR NOT NULL,
    source_code VARCHAR NOT NULL,
    raw_artifact_id VARCHAR,
    bookmaker_name VARCHAR NOT NULL,
    market_type VARCHAR NOT NULL,
    selection VARCHAR NOT NULL,
    line_value DOUBLE,
    decimal_odds DOUBLE,
    quote_type VARCHAR,
    quoted_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_market_event (
    prediction_market_event_id VARCHAR PRIMARY KEY,
    source_event_id VARCHAR NOT NULL,
    title VARCHAR,
    slug VARCHAR,
    description VARCHAR,
    fixture_id VARCHAR,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    resolution_source VARCHAR,
    active BOOLEAN,
    closed BOOLEAN,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_market (
    prediction_market_id VARCHAR PRIMARY KEY,
    prediction_market_event_id VARCHAR,
    source_market_id VARCHAR NOT NULL,
    question VARCHAR,
    slug VARCHAR,
    market_type VARCHAR,
    line_value DOUBLE,
    rules_text VARCHAR,
    active BOOLEAN,
    closed BOOLEAN,
    volume DOUBLE,
    liquidity DOUBLE,
    retrieved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_market_outcome (
    outcome_id VARCHAR PRIMARY KEY,
    prediction_market_id VARCHAR NOT NULL,
    source_token_id VARCHAR,
    outcome_name VARCHAR NOT NULL,
    displayed_price DOUBLE
);

CREATE TABLE IF NOT EXISTS orderbook_snapshot (
    orderbook_snapshot_id VARCHAR PRIMARY KEY,
    outcome_id VARCHAR,
    source_token_id VARCHAR NOT NULL,
    market_condition_id VARCHAR,
    observed_at TIMESTAMPTZ NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    best_bid DOUBLE,
    best_ask DOUBLE,
    tick_size DOUBLE,
    minimum_order_size DOUBLE,
    raw_artifact_id VARCHAR
);

CREATE TABLE IF NOT EXISTS orderbook_level (
    orderbook_snapshot_id VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    level_index INTEGER NOT NULL,
    price DOUBLE NOT NULL,
    size DOUBLE NOT NULL,
    PRIMARY KEY (orderbook_snapshot_id, side, level_index)
);

CREATE TABLE IF NOT EXISTS market_price_history (
    source_token_id VARCHAR NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    price DOUBLE NOT NULL,
    raw_artifact_id VARCHAR,
    PRIMARY KEY (source_token_id, timestamp)
);

CREATE TABLE IF NOT EXISTS data_quality_issue (
    issue_id VARCHAR PRIMARY KEY,
    rule_code VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    entity_type VARCHAR,
    internal_entity_id VARCHAR,
    source_code VARCHAR,
    raw_artifact_id VARCHAR,
    details JSON,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    status VARCHAR NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS database_build (
    build_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,
    counts JSON,
    notes VARCHAR
);
