-- API-Football does not provide a stable event ID. Older loader versions used
-- list position as part of the key, which created duplicates when the provider
-- reordered a corrected event timeline. Rebuild these rows from retained raw
-- artifacts using the stable natural key implemented by the current loader.
DELETE FROM match_event WHERE source_code = 'api_football';
