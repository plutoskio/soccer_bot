# API-Football 50-Batch Backfill Audit

Generated: 2026-07-04

## Scope

- New successful batches: **50**
- Successful HTTP responses used: **50**
- Newly validated fixtures: **909**
- New canonical fixtures: **859**
- Existing canonical fixtures enriched: **50**
- Failed checkpoints remaining: **0**
- Remaining manifest batches: **1,120**

The block covered UEFA Champions League seasons, UEFA EURO 2024 and 2020,
and the 2022 FIFA World Cup.

## Retrospective validation

All **61** completed checkpoints, representing **1,116** fixtures, were
revalidated after the identity repair.

| Check | Failures |
|---|---:|
| Raw artifact checksums | 0 |
| Returned fixture identities | 0 |
| Scores against raw JSON | 0 |
| Home/away identities | 0 |
| Critical player values | 0 |
| API player-source identity collisions | 0 |
| Blocking database-quality issues | 0 |
| Failed checkpoints | 0 |

Participating-player counts ranged from **25** to **34**. Passing coverage
ranged from **80.65%** to **100%**, above the configured 80% minimum. Thirteen
player-stat appearances were absent from their provider lineup lists; no
fixture exceeded the permitted maximum of one such omission.

## Provider anomalies handled

- A transient TLS disconnect stopped the first execution safely. Checkpointing
  resumed without repeating completed batches.
- Older EURO payloads reused API player ID `26389` for Renat Dadaşov and Rüfət
  Dadaşov in the same match. API player-stat identities now use the pair
  `(provider ID, normalized provider name)`.
- Lineup and event IDs remain isolated from authoritative player-stat IDs and
  are linked only through unique fixture-and-team context.
- Reprocessing a lineup snapshot now replaces its membership rather than
  appending stale player rows.

## Current detailed coverage

- API-Football fixtures with player statistics: **2,017**
- Fixtures passing the strict completeness gate: **2,016**
- Total canonical fixtures in DuckDB: **24,141**

The local pre-run backup is
`data/warehouse/soccer.pre_50_batch_run.duckdb`.
