# Database Coverage Report

Generated: 2026-07-02T19:22:34.429211+00:00

Database: `data/warehouse/soccer.duckdb`

## Canonical row counts

| Table | Rows |
|---|---:|
| `raw_artifact` | 162 |
| `competition` | 86 |
| `season` | 141 |
| `team` | 1,123 |
| `player` | 9,484 |
| `source_entity_map` | 51,608 |
| `fixture` | 22,297 |
| `fixture_result_observation` | 39,815 |
| `lineup_snapshot` | 6 |
| `lineup_player` | 152 |
| `appearance` | 50 |
| `match_event` | 4,420 |
| `team_match_stat_observation` | 79,256 |
| `player_match_stat_observation` | 50 |
| `player_season_stat` | 32,574 |
| `bookmaker_quote` | 190,839 |
| `prediction_market_event` | 231 |
| `prediction_market` | 3,885 |
| `prediction_market_outcome` | 7,770 |
| `orderbook_snapshot` | 1 |
| `orderbook_level` | 142 |
| `market_price_history` | 25 |
| `data_quality_issue` | 0 |

## Raw artifacts by source

| Source | Artifacts |
|---|---:|
| `api_football` | 23 |
| `football_data_uk` | 50 |
| `polymarket_clob` | 2 |
| `polymarket_gamma` | 23 |
| `statsbomb_open` | 4 |
| `understat` | 60 |

## Source fixture mappings

| Source | Fixtures |
|---|---:|
| `api_football` | 485 |
| `football_data_uk` | 17,937 |
| `statsbomb_open` | 64 |
| `understat` | 21,690 |

## Modeling-data readiness

| Usable data slice | Rows |
|---|---:|
| Fixtures with regulation scores | 21,937 |
| Team-match rows with corners | 35,872 |
| Team-match rows with xG | 43,178 |
| Player-season rows with minutes, xG, and xA | 32,574 |
| Detailed match events | 4,420 |
| Historical bookmaker quotes | 190,839 |
| Polymarket moneyline markets | 90 |
| Polymarket spread markets | 32 |
| Polymarket exact-score markets | 153 |

## Open quality issues

| Severity | Rule | Count |
|---|---|---:|
| — | — | 0 |

## Current interpretation

This database is the initial canonical backfill from validated sample and bulk sources. Counts measure successfully normalized records, not complete global soccer coverage. Additional leagues/seasons and continuous upcoming-match collection will be appended idempotently.
