ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS pass_accuracy_pct DOUBLE;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS rating DOUBLE;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS captain BOOLEAN;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS shirt_number INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS goals_conceded INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS goalkeeper_saves INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS tackles INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS tackle_blocks INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS interceptions INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS duels INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS duels_won INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS dribbles_attempted INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS dribbles_successful INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS dribbled_past INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS fouls_drawn INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS fouls_committed INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS yellow_red_cards INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS penalties_won INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS penalties_committed INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS penalties_missed INTEGER;
ALTER TABLE player_match_stat_observation ADD COLUMN IF NOT EXISTS penalties_saved INTEGER;

UPDATE player_match_stat_observation
SET
    pass_accuracy_pct = CASE
        WHEN passes > 0 AND accurate_passes IS NOT NULL
        THEN 100.0 * accurate_passes / passes
    END,
    rating = try_cast(json_extract_string(statistics, '$.games.rating') AS DOUBLE),
    captain = try_cast(json_extract_string(statistics, '$.games.captain') AS BOOLEAN),
    shirt_number = try_cast(json_extract_string(statistics, '$.games.number') AS INTEGER),
    goals_conceded = try_cast(json_extract_string(statistics, '$.goals.conceded') AS INTEGER),
    goalkeeper_saves = try_cast(json_extract_string(statistics, '$.goals.saves') AS INTEGER),
    tackles = try_cast(json_extract_string(statistics, '$.tackles.total') AS INTEGER),
    tackle_blocks = try_cast(json_extract_string(statistics, '$.tackles.blocks') AS INTEGER),
    interceptions = try_cast(json_extract_string(statistics, '$.tackles.interceptions') AS INTEGER),
    duels = try_cast(json_extract_string(statistics, '$.duels.total') AS INTEGER),
    duels_won = try_cast(json_extract_string(statistics, '$.duels.won') AS INTEGER),
    dribbles_attempted = try_cast(json_extract_string(statistics, '$.dribbles.attempts') AS INTEGER),
    dribbles_successful = try_cast(json_extract_string(statistics, '$.dribbles.success') AS INTEGER),
    dribbled_past = try_cast(json_extract_string(statistics, '$.dribbles.past') AS INTEGER),
    fouls_drawn = try_cast(json_extract_string(statistics, '$.fouls.drawn') AS INTEGER),
    fouls_committed = try_cast(json_extract_string(statistics, '$.fouls.committed') AS INTEGER),
    yellow_red_cards = try_cast(json_extract_string(statistics, '$.cards.yellowred') AS INTEGER),
    penalties_won = try_cast(json_extract_string(statistics, '$.penalty.won') AS INTEGER),
    penalties_committed = try_cast(json_extract_string(statistics, '$.penalty.commited') AS INTEGER),
    penalties_missed = try_cast(json_extract_string(statistics, '$.penalty.missed') AS INTEGER),
    penalties_saved = try_cast(json_extract_string(statistics, '$.penalty.saved') AS INTEGER)
WHERE source_code = 'api_football';
