# Database Coverage Report

Generated: 2026-07-04T11:28:51.215763+00:00

Database: `data/warehouse/soccer.duckdb`

## Canonical row counts

| Table | Rows |
|---|---:|
| `raw_artifact` | 449 |
| `competition` | 107 |
| `season` | 235 |
| `team` | 1,411 |
| `player` | 60,964 |
| `source_entity_map` | 74,858 |
| `fixture` | 24,141 |
| `fixture_result_observation` | 41,997 |
| `lineup_snapshot` | 4,216 |
| `lineup_player` | 90,908 |
| `appearance` | 82,496 |
| `match_event` | 37,981 |
| `team_match_stat_observation` | 83,646 |
| `player_match_stat_observation` | 82,496 |
| `player_season_stat` | 32,574 |
| `bookmaker_quote` | 192,016 |
| `prediction_market_event` | 252 |
| `prediction_market` | 4,731 |
| `prediction_market_outcome` | 9,462 |
| `orderbook_snapshot` | 1 |
| `orderbook_level` | 142 |
| `market_price_history` | 25 |
| `data_quality_issue` | 18 |

## Raw artifacts by source

| Source | Artifacts |
|---|---:|
| `api_football` | 309 |
| `football_data_uk` | 50 |
| `polymarket_clob` | 2 |
| `polymarket_gamma` | 24 |
| `statsbomb_open` | 4 |
| `understat` | 60 |

## Source fixture mappings

| Source | Fixtures |
|---|---:|
| `api_football` | 2,576 |
| `football_data_uk` | 17,937 |
| `statsbomb_open` | 64 |
| `understat` | 21,690 |

## Modeling-data readiness

| Usable data slice | Rows |
|---|---:|
| Fixtures with regulation scores | 23,872 |
| Team-match rows with corners | 40,158 |
| Team-match rows with xG | 45,166 |
| Player-season rows with minutes, xG, and xA | 32,574 |
| Detailed match events | 37,981 |
| Historical bookmaker quotes | 192,016 |
| Polymarket moneyline markets | 96 |
| Polymarket spread markets | 54 |
| Polymarket exact-score markets | 187 |

## Open quality issues

| Severity | Rule | Count |
|---|---|---:|
| — | — | 0 |

## Current interpretation

This database is the initial canonical backfill from validated sample and bulk sources. Counts measure successfully normalized records, not complete global soccer coverage. Additional leagues/seasons and continuous upcoming-match collection will be appended idempotently.
