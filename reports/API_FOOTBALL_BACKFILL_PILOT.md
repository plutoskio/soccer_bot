# API-Football Backfill Pilot

Generated: 2026-07-03

## Scope

- Batches executed: **10**
- API calls used: **10**
- Requested fixtures: **187**
- Returned fixtures: **187**
- Relationally validated fixtures: **187**
- Duplicate fixture IDs: **0**
- Failed batches: **0**

The pilot covered the remaining nine UEFA Champions League 2025 batches and
the first UEFA Champions League 2024 batch. One 2025 batch contained seven
fixtures; all other pilot batches contained twenty.

## Validation results

| Check | Result |
|---|---:|
| Raw artifact checksum failures | 0 |
| Missing fixture mappings | 0 |
| Score mismatches against raw JSON | 0 |
| Home/away identity mismatches | 0 |
| Player-value mismatches | 0 |
| Source player identity collisions | 0 |
| Within-fixture player collisions | 0 |
| Blocking quality issues | 0 |
| Quality warnings | 0 |

Participating-player counts ranged from **27** to **34** per fixture. Passing
coverage ranged from **90.32%** to **100%**, above the configured 80% minimum.

## Rows added

| Table | Added |
|---|---:|
| `raw_artifact` | 10 |
| `fixture` | 187 |
| `fixture_result_observation` | 187 |
| `lineup_snapshot` | 374 |
| `lineup_player` | 8,174 |
| `appearance` | 8,176 |
| `match_event` | 3,117 |
| `team_match_stat_observation` | 374 |
| `player_match_stat_observation` | 8,176 |
| `player` | Not isolated* |

Cross-table identity reconciliation found one provider omission: Dani Meso has
an internally consistent 11-minute Real Madrid player-stat record but is absent
from both lineup lists in that fixture. The record is retained and counted as
an unlisted participant. Validation permits at most one such omission per
fixture; a larger discrepancy fails the batch.

\*The canonical `player` count changed across the full retained API archive
during the source-ID repair, so its delta cannot be attributed only to these
ten pilot batches.

## Quota and recovery

The last response reported a 7,500-request daily limit and 7,263 requests
remaining. The pre-pilot database backup is stored locally at
`data/warehouse/soccer.pre_10_batch_pilot.duckdb`.
