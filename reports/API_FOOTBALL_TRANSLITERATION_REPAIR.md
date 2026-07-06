# API-Football Player Transliteration Repair

Generated: 2026-07-05

## Trigger

Historical backfill batch `api-football-103-2025-0003` stopped on fixture
`1342230`, Kristiansund BK 0-1 Fredrikstad. API-Football represented the same
players differently between its embedded sections:

| Lineup/event identity | Player-stat identity |
|---|---|
| `A. Saether`, ID `544512` | `Adrian Sæther`, ID `313648` |
| `S. Sorlokk`, ID `544500` | `Sondre Sørløkk`, ID `39265` |

The numeric lineup/event IDs are not authoritative. The existing contextual
linker correctly prioritizes player-stat identities, but its comparison
normalizer did not transliterate standalone letters such as `æ` and `ø`.

## Prospective fix

API player-name comparison now transliterates common standalone Latin letters,
including `æ -> ae`, `œ -> oe`, `ø -> o`, `ł -> l`, `đ/ð -> d`, `þ -> th`,
`ı -> i`, `ŋ -> n`, `ŧ -> t`, `ħ -> h`, `ß -> ss`, and `ə -> e`.

This transformation is comparison-only. Canonical display names, normalized
database names, source identity keys, and stable IDs are unchanged. Matching
still requires a unique compatible player within the same fixture and team;
ambiguous candidates are not linked automatically.

## Retrospective repair

The dry-run audit found **11** uniquely recoverable historical links across
**11 fixtures** and **3 raw artifacts**, with **0 ambiguous candidates**. The
repair replayed only those retained raw batches on a disposable database copy.
The live database was replaced atomically after validation.

- Recoverable links before: **11**
- Recoverable links after: **0**
- Blocking quality issues: **0**
- API player-identity collisions: **0**
- Backup: `data/warehouse/soccer.pre_transliteration_repair.duckdb`

## Failed-batch recovery

The rejected 20-fixture batch was replayed from its retained raw response:

- API calls: **0**
- Cache hits: **1**
- Requested/returned/validated: **20 / 20 / 20**
- Fixture `1342230` participating players absent from lineup: **0**
- Starter counts: **11 / 11**
- Score, team, player-value, and identity mismatches: **0**

All **35** automated tests pass, including an end-to-end regression test that
links transliterated lineup and event aliases to the authoritative player-stat
identity without changing its stable key.
