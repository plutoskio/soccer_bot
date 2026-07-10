CREATE TABLE IF NOT EXISTS player_identity_state (
    player_id VARCHAR PRIMARY KEY,
    is_identity_placeholder BOOLEAN NOT NULL,
    reason VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS player_identity_placeholder_idx
    ON player_identity_state (is_identity_placeholder, player_id);

-- Existing unresolved lineup aliases were intentionally stored as local
-- player rows before this marker existed.  Mark only rows that have no
-- canonical API-Football player mapping; rows that also have canonical
-- provider evidence are retained as real players and are not rewritten.
INSERT INTO player_identity_state (player_id, is_identity_placeholder, reason)
SELECT p.player_id, true, 'unresolved_api_football_lineup_alias'
FROM player p
WHERE EXISTS (
    SELECT 1
    FROM source_entity_map lineup_map
    WHERE lineup_map.source_code = 'api_football_lineup'
      AND lineup_map.entity_type = 'player'
      AND lineup_map.review_status = 'pending'
      AND lineup_map.internal_entity_id = p.player_id
)
AND NOT EXISTS (
    SELECT 1
    FROM source_entity_map api_map
    WHERE api_map.source_code = 'api_football'
      AND api_map.entity_type = 'player'
      AND api_map.internal_entity_id = p.player_id
)
ON CONFLICT (player_id) DO UPDATE SET
    is_identity_placeholder = true,
    reason = excluded.reason,
    updated_at = now();
