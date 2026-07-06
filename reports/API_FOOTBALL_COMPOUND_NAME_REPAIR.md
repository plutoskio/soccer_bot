# API-Football Compound-Name Repair

Generated: 2026-07-05

## Trigger

Backfill batch `api-football-103-2025-0005` stopped on fixture `1342273`,
Strømsgodset 0-2 Vålerenga. API-Football used different name lengths and IDs
between its embedded sections:

| Lineup/event identity | Player-stat identity | Shirt |
|---|---|---:|
| `M. Spiten-Nysaeter`, ID `319368` | `Mats Spiten`, ID `519924` | 39 |
| `S. Sjovold`, ID `544513` | `Stian Sjøvold Thorstensen`, ID `384572` | 22 |

Official and independent match records confirm that Mats Spiten entered for
Marcus Mehnert and Stian Sjøvold Thorstensen started before leaving after six
minutes.

## Matching rule

The contextual matcher now permits a shortened compound surname only when all
of the following hold:

- first initials agree;
- at least one side is abbreviated;
- the shorter surname-token set is contained in the longer set;
- every compared surname token has at least four characters;
- lineup and player-stat shirt numbers are equal;
- exactly one candidate exists for that fixture and team.

Two different full first names are never merged merely because their initials
and one surname token agree. Ambiguous candidates remain isolated.

## Retrospective repair

The database-wide dry run found **16** uniquely recoverable historical links
across **16 fixtures** and **11 raw artifacts**. Each had exact shirt-number
agreement. Ambiguous García-family candidates were explicitly excluded.

- Recoverable links before: **16**
- Recoverable links after: **0**
- Blocking quality issues: **0**
- API player-identity collisions: **0**
- Backup: `data/warehouse/soccer.pre_compound_name_repair.duckdb`

## Failed-batch recovery

The rejected 20-fixture batch replayed from retained raw data with **0 API
calls** and **1 cache hit**. Fixture `1342273` now has:

- requested/returned/validated batch fixtures: **20 / 20 / 20**;
- participating players absent from lineup: **0**;
- starter counts: **11 / 11**;
- score, team, player-value, and identity mismatches: **0**.

Mats Spiten and Stian Sjøvold Thorstensen now each connect to the correct
lineup record and event record. All **37** automated tests pass.
