# API-Football 100-Batch Backfill Audit

Generated: 2026-07-04

## Execution result

- Run ID: `0efbee93-3feb-4c4e-a2c9-6076a7da169e`
- Run status: **completed**
- Successful batches: **100 / 100**
- API calls: **100**
- Requested fixtures: **1,944**
- Returned fixtures: **1,944**
- Validated fixtures: **1,944**
- Failed checkpoints: **0**
- Remaining manifest batches: **1,020**

The pre-run backup is
`data/warehouse/soccer.pre_100_batch_run.duckdb`. Its SHA-256 checksum matched
the live database immediately before execution.

## Integrity checks

Every fixture in this block passed the executor's raw-payload and relational
validation before its batch checkpoint was committed.

| Check | Failures |
|---|---:|
| Requested/returned fixture identity | 0 |
| Final score against raw response | 0 |
| Home/away team identity against raw response | 0 |
| Missing or duplicate result observations | 0 |
| Missing lineup teams | 0 |
| Lineups without exactly 11 starters per team | 0 |
| Missing or duplicate team-stat observations | 0 |
| Duplicate player identities within a fixture | 0 |
| Invalid player values | 0 |
| Players assigned to the wrong team | 0 |
| Critical player-value mismatches against raw data | 0 |
| Missing raw files | 0 |
| Raw SHA-256 checksum failures | 0 |
| Non-200 stored API responses | 0 |
| Open database-quality issues | 0 |

Participating-player counts ranged from **24** to **34**. Passing-data
coverage ranged from **86.67%** to **100%**, above the configured 80% gate.
Eleven participating player records were omitted from their provider lineup;
no fixture exceeded the permitted maximum of one omission.

All **161** successful checkpoints now represent **3,060** independently
validated fixtures. Across that full checkpoint set, passing coverage ranges
from **80.65%** to **100%**, and no fixture has more than one provider-lineup
omission.

## Database growth

| Table | Before | After | Added |
|---|---:|---:|---:|
| `fixture` | 24,141 | 24,555 | 414 |
| `fixture_result_observation` | 41,997 | 43,941 | 1,944 |
| `lineup_snapshot` | 4,216 | 8,104 | 3,888 |
| `lineup_player` | 90,908 | 173,196 | 82,288 |
| `team_match_stat_observation` | 83,646 | 87,534 | 3,888 |
| `player_match_stat_observation` | 82,496 | 164,776 | 82,280 |
| `appearance` | 82,496 | 164,776 | 82,280 |
| `match_event` | 37,981 | 69,534 | 31,553 |
| `player` | 60,964 | 62,217 | 1,253 |
| `team` | 1,411 | 1,414 | 3 |
| `raw_artifact` | 449 | 549 | 100 |

Only 414 canonical fixtures were new because 1,530 scheduled fixtures already
existed in the warehouse and were enriched with detailed match data. The
number of API-Football fixtures with player-match statistics increased from
**2,017** to **3,961**. Based on the prior strict gate result and this block's
1,944 passing fixtures, **3,960** now pass the strict completeness gate; the
previously documented Sparta Praha-Slovácko provider anomaly remains the sole
failure.

## Independent web spot checks

The sample was selected reproducibly with random seed `20260704` from the
1,867 newly imported fixtures in the five major domestic leagues.

1. **Leeds 0-4 Arsenal, 31 January 2026**
   - Database: Zubimendi 27', Madueke 38', Gyökeres 69', Gabriel Jesus 86'.
   - The official Premier League report confirms the score, scorers and
     minutes. It also confirms that the second goal was ultimately reassigned
     from a goalkeeper own goal to Madueke, matching the database.
   - Source: https://www.premierleague.com/en/news/4572658/leeds-united-0-arsenal-4-match-report-31-january-2026

2. **Manchester United 0-3 Tottenham, 29 September 2024**
   - Database: Brennan Johnson 3', Dejan Kulusevski 47', Dominic Solanke 77'.
   - The official Premier League report confirms the score and all three
     scorers, including Johnson's third-minute opener.
   - Source: https://www.premierleague.com/en/news/4130199

3. **Lazio 0-3 Inter, 9 May 2026**
   - Database: Lautaro Martínez 6', Petar Sučić 39', Henrikh Mkhitaryan 76'.
   - Inter's official match centre confirms the score, scorers and minutes.
     Both teams' formations and all 22 starters also match the database.
   - Source: https://www.inter.it/en/match_center/5349

No discrepancy was found in the random external sample.
