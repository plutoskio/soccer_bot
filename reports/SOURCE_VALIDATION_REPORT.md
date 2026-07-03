# Source Validation Report

Generated: 2026-07-02T18:42:33.254179+00:00

## Retrieval summary

| Source | Observations | Resources |
|---|---:|---|
| `api_football` | 23 | fixture_by_id (4), fixture_events (4), fixture_injuries (2), fixture_lineups (4), fixture_players (2), fixture_statistics (2), fixtures_by_date (3), status (2) |
| `football_data_uk` | 2 | league_csv (2) |
| `polymarket_clob` | 2 | order_book (1), price_history (1) |
| `polymarket_gamma` | 23 | fixture_search (2), soccer_events (13), sports (4), sports_market_types (4) |
| `statsbomb_open` | 4 | competitions (1), events (1), lineups (1), matches (1) |
| `understat` | 1 | league_data (1) |

## HTTP and payload results

| Source | Resource | HTTP | Top-level records | Duplicate body |
|---|---|---:|---:|---|
| `api_football` | `fixture_by_id` | 200 | 1 | False |
| `api_football` | `fixture_events` | 200 | 0 | False |
| `api_football` | `fixture_injuries` | 200 | 0 | False |
| `api_football` | `fixture_lineups` | 200 | 2 | False |
| `api_football` | `fixture_players` | 200 | 2 | False |
| `api_football` | `fixture_statistics` | 200 | 2 | False |
| `api_football` | `fixtures_by_date` | 200 | 203 | False |
| `api_football` | `status` | 200 | 0 | False |
| `football_data_uk` | `league_csv` | 200 | 380 | False |
| `polymarket_clob` | `order_book` | 200 | 142 | False |
| `polymarket_clob` | `price_history` | 200 | 25 | False |
| `polymarket_gamma` | `fixture_search` | 200 | 7 | False |
| `polymarket_gamma` | `soccer_events` | 200 | 50 | True |
| `polymarket_gamma` | `sports` | 200 | 305 | True |
| `polymarket_gamma` | `sports_market_types` | 200 | 0 | True |
| `statsbomb_open` | `competitions` | 200 | 80 | False |
| `statsbomb_open` | `events` | 200 | 4407 | False |
| `statsbomb_open` | `lineups` | 200 | 2 | False |
| `statsbomb_open` | `matches` | 200 | 64 | False |
| `understat` | `league_data` | 200 | 537 | False |

## API-Football field coverage

### `fixtures_by_date`

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `fixture.id` | 485/485 | 485/485 |
| `fixture.date` | 485/485 | 485/485 |
| `fixture.status.short` | 485/485 | 485/485 |
| `league.id` | 485/485 | 485/485 |
| `league.name` | 485/485 | 485/485 |
| `teams.home.id` | 485/485 | 485/485 |
| `teams.away.id` | 485/485 | 485/485 |
| `goals.home` | 485/485 | 239/485 |
| `goals.away` | 485/485 | 239/485 |
| `score.fulltime.home` | 485/485 | 226/485 |
| `score.fulltime.away` | 485/485 | 226/485 |

### `fixture_lineups`

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `team.id` | 4/4 | 4/4 |
| `formation` | 4/4 | 4/4 |
| `startXI` | 4/4 | 4/4 |
| `substitutes` | 4/4 | 4/4 |

### `fixture_events`

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `time.elapsed` | 13/13 | 13/13 |
| `team.id` | 13/13 | 13/13 |
| `player.id` | 13/13 | 12/13 |
| `assist.id` | 13/13 | 8/13 |
| `type` | 13/13 | 13/13 |
| `detail` | 13/13 | 13/13 |

### `fixture_players`

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `player.id` | 50/50 | 50/50 |
| `statistics.0.games.minutes` | 50/50 | 30/50 |
| `statistics.0.games.position` | 50/50 | 50/50 |
| `statistics.0.games.substitute` | 50/50 | 50/50 |
| `statistics.0.shots.total` | 50/50 | 8/50 |
| `statistics.0.shots.on` | 50/50 | 4/50 |
| `statistics.0.goals.total` | 50/50 | 2/50 |
| `statistics.0.goals.assists` | 50/50 | 30/50 |
| `statistics.0.passes.key` | 50/50 | 10/50 |
| `statistics.0.cards.yellow` | 50/50 | 50/50 |
| `statistics.0.penalty.scored` | 50/50 | 50/50 |

### `fixture_statistics`

Populated statistic types across **2** team records: `Ball Possession`, `Blocked Shots`, `Corner Kicks`, `Fouls`, `Goalkeeper Saves`, `Offsides`, `Passes %`, `Passes accurate`, `Red Cards`, `Shots insidebox`, `Shots off Goal`, `Shots on Goal`, `Shots outsidebox`, `Total Shots`, `Total passes`, `Yellow Cards`, `expected_goals`, `goals_prevented`.

## Historical bootstrap validation

### StatsBomb Open Data

The probe captured **64** FIFA World Cup 2022 matches, **50** lineup-player records, and **4407** events for the Argentina–France sample match.

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `id` | 4407/4407 | 4407/4407 |
| `period` | 4407/4407 | 4407/4407 |
| `timestamp` | 4407/4407 | 4407/4407 |
| `minute` | 4407/4407 | 4407/4407 |
| `second` | 4407/4407 | 4407/4407 |
| `type.name` | 4407/4407 | 4407/4407 |
| `team.id` | 4407/4407 | 4407/4407 |
| `player.id` | 4378/4407 | 4378/4407 |
| `location` | 4352/4407 | 4352/4407 |
| `shot.statsbomb_xg` | 38/4407 | 38/4407 |
| `shot.outcome.name` | 38/4407 | 38/4407 |
| `pass.goal_assist` | 2/4407 | 2/4407 |
| `pass.assisted_shot_id` | 19/4407 | 19/4407 |

### Football-Data.co.uk

The probe captured **760** Premier League rows from `/mmz4281/2526/E0.csv`, `/mmz4281/2425/E0.csv`.

| Column | Non-empty rows |
|---|---:|
| `Date` | 760/760 |
| `Time` | 760/760 |
| `HomeTeam` | 760/760 |
| `AwayTeam` | 760/760 |
| `FTHG` | 760/760 |
| `FTAG` | 760/760 |
| `FTR` | 760/760 |
| `HS` | 760/760 |
| `AS` | 760/760 |
| `HST` | 760/760 |
| `AST` | 760/760 |
| `HC` | 760/760 |
| `AC` | 760/760 |
| `B365H` | 760/760 |
| `B365D` | 760/760 |
| `B365A` | 760/760 |
| `AvgH` | 760/760 |
| `AvgD` | 760/760 |
| `AvgA` | 760/760 |
| `AHh` | 759/760 |
| `AvgAHH` | 760/760 |
| `AvgAHA` | 760/760 |

### Understat

The EPL 2025/26 endpoint returned **380** fixtures, **20** teams, and **537** player-season records.

| Field | Records containing field | Non-null records |
|---|---:|---:|
| `id` | 537/537 | 537/537 |
| `player_name` | 537/537 | 537/537 |
| `games` | 537/537 | 537/537 |
| `time` | 537/537 | 537/537 |
| `goals` | 537/537 | 537/537 |
| `xG` | 537/537 | 537/537 |
| `assists` | 537/537 | 537/537 |
| `xA` | 537/537 | 537/537 |
| `shots` | 537/537 | 537/537 |
| `key_passes` | 537/537 | 537/537 |
| `npg` | 537/537 | 537/537 |
| `npxG` | 537/537 | 537/537 |
| `xGChain` | 537/537 | 537/537 |
| `xGBuildup` | 537/537 | 537/537 |
| `position` | 537/537 | 537/537 |
| `team_title` | 537/537 | 537/537 |

## Polymarket sports discovery

The `/sports` endpoint returned **305** records.

Likely soccer configurations: `epl`, `lal`, `bun`, `fl1`, `sea`, `ucl`, `afc`, `ofc`, `fif`, `ere`, `arg`, `itc`, `mex`, `lcs`, `lib`, `sud`, `tur`, `con`, `cof`, `uef`, `caf`, `rus`, `efa`, `efl`, `uel`, `mls`, `cdr`, `col`, `cde`, `dfb`, `bra`, `jap`, `ja2`, `kor`, `spl`, `chi`, `aus`, `ind`, `nor`, `den`, `por`, `ssc`, `mar1`, `egy1`, `cze1`, `bol1`, `rou1`, `bra2`, `per1`, `uwcl`, `ccc`, `fifwc`, `bl2`, `elc`, `j2100`, `col1`, `ukr1`, `aut`, `j1100`, `es2`, `argpn`, `srb`, `hun`, `ire`, `afwq`, `aswq`, `atc`, `auc`, `brcm`, `brco`, `conl`, `copa`, `copaam`, `cwc`, `el1`, `el2`, `enl`, `euc`, `ewq`, `fifaw`, `fpd`, `chi1`, `fr2`, `itsb`, `swe`, `isr`, `slo`, `bul`, `grc`, `gtm`, `hr1`, `icwq`, `isp`, `lec`, `nawq`, `ncag`, `nlc`, `nwsl`, `owq`, `ptc`, `sawq`, `scoc`, `scop`, `skc`, `svk1`, `trsk`, `ueq`, `unl`, `weuc`, `wwcquefa`, `uru1`, `ecu1`, `fin1`, `isl1`, `irl1`, `nor2`, `est1`, `lva1`, `geo1`, `fro1`, `kor2`, `chi2`, `kaz1`, `bra3`, `blr1`, `col2`, `uzb1`, `ven1`, `swe2`, `ltu1`, `chl2`.

### Observed soccer market types

| Market type | Markets observed |
|---|---:|
| `unclassified` | 3360 |
| `totals` | 56 |
| `moneyline` | 40 |
| `spreads` | 40 |
| `soccer_exact_score` | 34 |
| `both_teams_to_score` | 14 |
| `soccer_halftime_result` | 6 |

Example event titles: `World Cup Winner `; `Ballon d'Or Winner 2026`; `World Cup Group C Winner`; `Which continent will win the World Cup?`; `Claudio Tapia out as AFA President by July 19, 2026?`; `Will Cristiano Ronaldo announce his retirement in 2026?`; `2025-2026 PFA Players' Player of the Year Winner`; `CD Concepción vs. O'Higgins FC - More Markets`; `Which club will Cristiano Ronaldo play for next?`; `MLS: 2026 Defender of the Year`.

### Targeted fixture search

| Query | Events returned | Direct fixture event | Market types |
|---|---:|---|---|
| `Spain Austria` | 13 | Spain vs. Austria | `moneyline` |
| `USA Bosnia Herzegovina` | 7 | not found | — |

### CLOB read validation

| Bids | Asks | Tick size | Minimum order | Price-history points |
|---:|---:|---:|---:|---:|
| 82 | 60 | 0.0025 | 5 | 25 |

## Confirmed findings from this probe

- API-Football returned fixtures, confirmed lineups, formations, starters, substitutes, event timelines, per-player minutes/goals/assists/shots, team corners, and team expected goals for a covered World Cup fixture.
- The upcoming Spain fixture returned a complete lineup shortly before kickoff; events and injuries were empty at retrieval time.
- Rapid unpaced API-Football requests produced HTTP 429 responses, so the collector now enforces a minimum interval and stops on rate limiting.
- Polymarket exposes public soccer metadata and classified market types through its Gamma API.
- Targeted search found the Spain–Austria regulation moneyline event, and the public CLOB returned a populated order book and price history without authentication.
- StatsBomb Open Data supplied a complete World Cup match list plus rich lineups and 4,407 events for the 2022 final sample.
- Football-Data.co.uk supplied 760 immediately usable team-match rows with scores, shots, corners, moneyline odds, and handicap fields across two Premier League seasons.
- Understat supplied 537 EPL player-season records with minutes, goals, assists, shots, xG, xA, key passes, non-penalty xG, xGChain, and xGBuildup.

## Architecture implications

1. The database does not need to begin empty: Football-Data.co.uk can bootstrap team-result, spread, odds, and corner tables immediately.
2. StatsBomb can bootstrap rich event and lineup tables for selected competitions, while API-Football supplies current operational observations.
3. Understat can bootstrap club player-form features immediately, so the player model also does not need to wait for future collection.
4. Provider-specific xG must remain identifiable: API-Football returned team expected goals, StatsBomb supplies shot-level StatsBomb xG, and Understat supplies its own player xG/xA values.
5. Empty API fields are meaningful. Player minutes are null for unused substitutes, while zero and null goal/shot values must be normalized carefully rather than treated identically without field-specific rules.
6. The confirmed source payloads fit the canonical fixture, lineup, appearance, event, player-statistic, team-statistic, bookmaker-quote, prediction-market, and order-book tables proposed in `DATA_ARCHITECTURE.md`.

## Interpretation boundary

This report records endpoint behavior and field presence. It does not yet prove historical depth, competition-wide completeness, or long-term scraper stability. Those require the expanded fixture and bulk-source probes described in `DATA_ARCHITECTURE.md`.
