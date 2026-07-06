CREATE OR REPLACE VIEW fixture_model_eligibility AS
WITH result_quality AS (
    SELECT
        f.fixture_id,
        coalesce(f.status, '') = 'administrative_result_unplayed'
            AS administrative_unplayed,
        coalesce(
            bool_or(
                r.result_status = 'final'
                AND r.home_score_regulation IS NOT NULL
                AND r.away_score_regulation IS NOT NULL
                AND r.home_score_regulation >= 0
                AND r.away_score_regulation >= 0
            ),
            false
        )
        AND coalesce(f.status, '') <> 'administrative_result_unplayed'
            AS result_ok
    FROM fixture f
    LEFT JOIN fixture_result_observation r USING (fixture_id)
    GROUP BY f.fixture_id, f.status
),
team_artifact AS (
    SELECT
        tm.fixture_id,
        tm.source_code,
        coalesce(tm.raw_artifact_id, '') AS artifact_key,
        count(DISTINCT tm.team_id) FILTER (
            WHERE tm.team_id IN (f.home_team_id, f.away_team_id)
        ) AS represented_teams,
        count(DISTINCT tm.team_id) FILTER (
            WHERE tm.team_id IN (f.home_team_id, f.away_team_id)
              AND tm.shots IS NOT NULL
              AND tm.shots_on_target IS NOT NULL
              AND tm.corners IS NOT NULL
              AND tm.shots >= 0
              AND tm.shots_on_target >= 0
              AND tm.shots_on_target <= tm.shots
              AND tm.corners >= 0
              AND (tm.possession_pct IS NULL
                   OR tm.possession_pct BETWEEN 0 AND 100)
              AND (tm.passes IS NULL OR tm.passes >= 0)
              AND (tm.accurate_passes IS NULL OR tm.accurate_passes >= 0)
              AND (tm.passes IS NULL OR tm.accurate_passes IS NULL
                   OR tm.accurate_passes <= tm.passes)
        ) AS valid_core_teams,
        count(*) FILTER (
            WHERE tm.team_id NOT IN (f.home_team_id, f.away_team_id)
        ) AS wrong_team_rows
    FROM team_match_stat_observation tm
    JOIN fixture f USING (fixture_id)
    WHERE tm.period = 'regulation'
    GROUP BY tm.fixture_id, tm.source_code,
             coalesce(tm.raw_artifact_id, '')
),
team_quality AS (
    SELECT fixture_id,
           bool_or(
               represented_teams = 2
               AND valid_core_teams = 2
               AND wrong_team_rows = 0
           ) AS team_ok
    FROM team_artifact
    GROUP BY fixture_id
),
lineup_team AS (
    SELECT
        ls.fixture_id,
        ls.source_code,
        coalesce(ls.raw_artifact_id, '') AS artifact_key,
        ls.team_id,
        bool_or(ls.is_complete) AS complete,
        count(*) FILTER (WHERE lp.selection_role = 'starter') AS starters
    FROM lineup_snapshot ls
    LEFT JOIN lineup_player lp USING (lineup_snapshot_id)
    WHERE ls.lineup_type = 'confirmed'
    GROUP BY ls.fixture_id, ls.source_code,
             coalesce(ls.raw_artifact_id, ''), ls.team_id
),
lineup_artifact AS (
    SELECT
        fixture_id,
        source_code,
        artifact_key,
        count(*) AS represented_teams,
        bool_and(complete AND starters = 11) AS complete_lineups
    FROM lineup_team
    GROUP BY fixture_id, source_code, artifact_key
),
player_artifact AS (
    SELECT
        pm.fixture_id,
        pm.source_code,
        coalesce(pm.raw_artifact_id, '') AS artifact_key,
        count(*) FILTER (WHERE pm.minutes_played > 0) AS participants,
        count(DISTINCT pm.team_id) FILTER (
            WHERE pm.minutes_played > 0
        ) AS participant_teams,
        count(*) FILTER (
            WHERE pm.team_id NOT IN (f.home_team_id, f.away_team_id)
        ) AS wrong_team_rows,
        count(*) FILTER (
            WHERE pm.minutes_played < 0 OR pm.minutes_played > 130
               OR pm.goals < 0 OR pm.assists < 0
               OR pm.shots < 0 OR pm.shots_on_target < 0
               OR pm.shots_on_target > pm.shots
               OR pm.passes < 0 OR pm.accurate_passes < 0
               OR pm.accurate_passes > pm.passes
               OR pm.pass_accuracy_pct < 0 OR pm.pass_accuracy_pct > 100
               OR pm.rating < 0 OR pm.rating > 10
               OR pm.duels < 0 OR pm.duels_won < 0
               OR pm.duels_won > pm.duels
               OR pm.dribbles_attempted < 0
               OR pm.dribbles_successful < 0
               OR pm.dribbles_successful > pm.dribbles_attempted
        ) AS invalid_rows,
        count(*) FILTER (
            WHERE pm.minutes_played > 0
              AND EXISTS (
                  SELECT 1
                  FROM lineup_snapshot ls
                  JOIN lineup_player lp USING (lineup_snapshot_id)
                  WHERE ls.fixture_id = pm.fixture_id
                    AND ls.team_id = pm.team_id
                    AND ls.source_code = pm.source_code
                    AND coalesce(ls.raw_artifact_id, '') =
                        coalesce(pm.raw_artifact_id, '')
                    AND lp.player_id = pm.player_id
              )
        ) AS linked_participants
    FROM player_match_stat_observation pm
    JOIN fixture f USING (fixture_id)
    GROUP BY pm.fixture_id, pm.source_code,
             coalesce(pm.raw_artifact_id, '')
),
player_quality AS (
    SELECT pa.fixture_id,
           bool_or(
               la.represented_teams = 2
               AND la.complete_lineups
               AND pa.participants >= 22
               AND pa.participant_teams = 2
               AND pa.linked_participants >= 22
               AND pa.wrong_team_rows = 0
               AND pa.invalid_rows = 0
           ) AS player_ok
    FROM player_artifact pa
    JOIN lineup_artifact la
      ON la.fixture_id = pa.fixture_id
     AND la.source_code = pa.source_code
     AND la.artifact_key = pa.artifact_key
    GROUP BY pa.fixture_id
),
flags AS (
    SELECT
        f.fixture_id,
        rq.administrative_unplayed,
        rq.result_ok AS eligible_result_models,
        rq.result_ok AND coalesce(tq.team_ok, false)
            AS eligible_team_models,
        rq.result_ok AND coalesce(pq.player_ok, false)
            AS eligible_player_models
    FROM fixture f
    JOIN result_quality rq USING (fixture_id)
    LEFT JOIN team_quality tq USING (fixture_id)
    LEFT JOIN player_quality pq USING (fixture_id)
)
SELECT
    fixture_id,
    eligible_result_models,
    eligible_team_models,
    eligible_player_models,
    list_concat(
        CASE WHEN administrative_unplayed
             THEN ['administrative_unplayed'] ELSE [] END,
        CASE WHEN NOT administrative_unplayed
                   AND NOT eligible_result_models
             THEN ['missing_final_result'] ELSE [] END,
        CASE WHEN eligible_result_models
                   AND NOT eligible_team_models
             THEN ['team_data_incomplete'] ELSE [] END,
        CASE WHEN eligible_result_models
                   AND NOT eligible_player_models
             THEN ['player_data_incomplete'] ELSE [] END
    ) AS reason_codes
FROM flags;
