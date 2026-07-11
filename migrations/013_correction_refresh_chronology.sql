-- Migration 012 was first deployed after several fixtures had already passed
-- both correction windows. The initial planner correctly chose the latest
-- (+72h) recovery response, but a later run then scheduled the older +24h
-- label. Preserve every raw response and attempt while correcting only the
-- operational stage disposition for that reversed chronology.

UPDATE fixture_collection_component AS component
SET state = 'missed',
    reason_code = 'correction_window_missed_during_downtime',
    details = '{"recovered_by":"correction_refresh_72h","chronology_corrected_by":"migration_013"}',
    updated_at = now()
WHERE component.source_code = 'api_football'
  AND component.component_code = 'correction_refresh_24h'
  AND component.state = 'complete'
  AND EXISTS (
      SELECT 1
      FROM collection_checkpoint early
      JOIN collection_checkpoint later
        ON later.fixture_id = early.fixture_id
       AND later.job_type = 'correction_refresh_72h'
       AND later.status = 'succeeded'
      WHERE early.fixture_id = component.fixture_id
        AND early.job_type = 'correction_refresh_24h'
        AND early.status = 'succeeded'
        AND later.last_attempt_at < early.last_attempt_at
  );

UPDATE collection_checkpoint AS early
SET status = 'terminal',
    terminal_reason = 'correction_stage_superseded_by_72h',
    next_attempt_at = NULL,
    completed_at = coalesce(completed_at, last_attempt_at),
    updated_at = now()
WHERE early.job_type = 'correction_refresh_24h'
  AND early.status = 'succeeded'
  AND EXISTS (
      SELECT 1
      FROM collection_checkpoint later
      WHERE later.fixture_id = early.fixture_id
        AND later.job_type = 'correction_refresh_72h'
        AND later.status = 'succeeded'
        AND later.last_attempt_at < early.last_attempt_at
  );
