# API-Football 250-Batch Backfill Audit

Generated: 2026-07-05

## Result

- Successful batches: **250 / 250**
- Requested fixtures: **4,901**
- Returned fixtures: **4,901**
- Validated fixtures: **4,901**
- Failed checkpoints remaining: **0**
- Total successful checkpoints: **411**
- Remaining manifest batches: **770**

## Interruption and recovery

The original run stopped after 216 successful batches when fixture `871431`
(VfL Bochum 1-1 Borussia Dortmund, 28 April 2023) had passing data for 25 of
32 participating players, or 78.125%, below the former blocking 80% gate.

The preceding 216 batches were already committed independently. The failing
20-fixture batch was not partially loaded, and the final 33 planned batches
had not been attempted.

Passing coverage is now a non-blocking quality measurement. Fixtures below
the configured 80% threshold are retained with honest `NULL` values and an
open `low_player_passing_coverage` warning. Structural, identity, score, lineup,
team-stat and critical player-value checks remain blocking.

The failed batch was replayed from its immutable raw artifact with **zero API
calls**, then the 33 unattempted batches completed normally.

## Integrity audit

| Check | Failures |
|---|---:|
| Requested/returned/validated count reconciliation | 0 |
| Score against raw response | 0 |
| Home/away team identity | 0 |
| Result observation cardinality | 0 |
| Two complete 11-player starting lineups | 0 |
| Two team-stat observations | 0 |
| Duplicate player identities within a fixture | 0 |
| Invalid player values | 0 |
| Players assigned to the wrong team | 0 |
| Critical player-value mismatch against raw data | 0 |
| Missing raw artifacts | 0 |
| Raw SHA-256 checksum failures | 0 |
| Non-200 stored responses | 0 |

Participating-player counts ranged from **25** to **34**. Passing coverage
ranged from **78.125%** to **100%**; only fixture `871431` was below 80%.
Seventeen participating player records were absent from their provider lineup
lists, and no fixture exceeded the permitted maximum of one such omission.

The warehouse now contains **25,700** canonical fixtures and **8,862** distinct
API-Football fixtures with player-match statistics. All **32** automated tests
pass.
