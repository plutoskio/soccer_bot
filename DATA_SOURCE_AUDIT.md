# Soccer Polymarket Model — Data Source Audit

Status: initial research pass, 2 July 2026

## 1. Prediction scope and required data

The system should ultimately price:

1. Regulation-time moneyline (home/draw/away)
2. Player goals, assists, and goal-or-assist props
3. Goal spreads/handicaps
4. Exact score
5. First team to score
6. Corners

These are not six independent modeling problems. A coherent match simulation can produce moneyline, spread, exact-score, and first-to-score probabilities from the same simulated score/event distribution. Player props require a second layer that assigns attacking events to players based on the lineup, expected minutes, roles, and teammates. Corners require a related but separate count process.

### Minimum fields

| Data group | Required fields | Markets enabled |
|---|---|---|
| Fixture context | teams, competition, kickoff, venue, neutral-site flag | all |
| Confirmed lineup | starters, bench, formation, positions | player props; improves all others |
| Player history | minutes, starts, substitutions, goals, assists, shots, shots on target | player props |
| Advanced attacking history | xG, non-penalty xG, xA, key passes, shot location/type | player goals and assists |
| Team match history | score, home/away/neutral, opponent, date | moneyline, spread, exact score |
| Event timeline | goal minute, scorer, assister, substitutions, cards | first scorer/team; player models |
| Team match statistics | corners, shots, possession, cards | corners; contextual features |
| Availability | injuries, suspensions, squad selection | expected lineup/minutes |
| Market data | contract rules, outcomes, bids, asks, trades, timestamps, liquidity | value comparison and evaluation |

## 2. Important lineup distinction

The lineup displayed several hours or days before kickoff is usually a **predicted lineup**. The official starting XI is normally published close to kickoff. These need separate fields and separate confidence levels in the system.

- `predicted_lineup`: useful for early forecasts, but uncertain.
- `confirmed_lineup`: authoritative input for the final pre-match forecast.

API-Football states that covered competitions usually receive confirmed lineups 20–40 minutes before kickoff, with some competitions publishing later or only after the match. Google does not expose a documented public sports-card API suitable as a stable system dependency. Scraping the Google result card would therefore be a brittle fallback, not the preferred lineup source.

## 3. Source comparison

Ratings below describe suitability for this project, not the general quality of the provider.

### 3.1 API-Football — strongest free current-match candidate

- Type: authenticated REST API
- Free tier: 100 requests/day; all competitions and endpoints are advertised, but the free plan is limited in available seasons.
- Advertised data: fixtures, events, lineups, player match statistics, injuries, team statistics, pre-match odds, and match statistics.
- Relevant player fields: minutes, position, shots, goals, assists, key passes, cards, substitutions, and other match-level statistics.
- Relevant team fields: score, shots, possession, corners, cards, formations, and event timeline.
- Lineups: usually 20–40 minutes before kickoff when supported; coverage flags are available by league/season.
- Strength: one identifier system connects fixtures, lineups, players, events, and match statistics.
- Weakness: 100 calls/day is too small for indiscriminate historical collection. Coverage and historical-season access must be tested for the exact competitions.
- Preliminary role: **primary operational feed** for upcoming fixtures, confirmed lineups, events, and recent statistics.
- Source: [API-Football pricing](https://www.api-football.com/pricing), [API-Football documentation](https://www.api-football.com/documentation.)

Verdict: **Proceed to a live API test.** This is currently the best candidate for automating the data that appears in a Google match card.

### 3.2 StatsBomb Open Data — strongest open event-data training source

- Type: downloadable JSON on GitHub
- Cost: free for research/genuine interest, with attribution requirements.
- Data: selected competitions/seasons, matches, detailed events, lineups, and StatsBomb 360 data for selected matches.
- Strength: high-quality event structure, including locations and event relationships, suitable for learning event-based modeling and validating a simulation design.
- Weakness: only selected competitions and seasons; it is not a live or comprehensive production feed.
- Preliminary role: **offline event-model research and training**, not current fixture ingestion.
- Source: [StatsBomb Open Data](https://github.com/statsbomb/open-data)

Verdict: **Use**, but do not assume a model trained only on its selected competitions transfers cleanly to every national team or club competition.

### 3.3 Understat — useful club-level player attacking history

- Type: website without an official public API; community scrapers exist.
- Cost: free website access.
- Coverage: principally the top five European leagues plus the Russian Premier League, with history commonly available from 2014/15.
- Relevant fields: minutes, goals, assists, shots, xG, xA, key passes, non-penalty xG, xGChain, and xGBuildup; shot-level data is also exposed by the site.
- Strength: directly useful for scorer and assist propensity, especially for national-team players whose recent form comes from club football.
- Weakness: limited league coverage, no dependable official API contract, scraping/terms risk, and no broad national-team coverage.
- Preliminary role: **player-form enrichment** for players active in supported club leagues.
- Source: [understatr project and documented fields](https://github.com/ewenme/understatr), [soccerdata Understat adapter](https://soccerdata.readthedocs.io/en/stable/datasources/index.html)

Verdict: **Use experimentally with caching and provenance**, never as the sole player source.

### 3.4 Sofascore and FotMob through `soccerdata` — broad but unofficial

- Type: scraping/internal JSON interfaces wrapped by an open-source Python package.
- `soccerdata` supports sources including ESPN, FBref, FotMob, Sofascore, Understat, WhoScored, Club Elo, and Football-Data.co.uk.
- Potential data: schedules, historical results, lineups, player/team statistics, and advanced fields depending on the underlying site and competition.
- Strength: broad practical coverage and a common DataFrame interface; potentially the best free gap filler for current and historical lineups/player stats.
- Weakness: not official provider APIs. Site changes can break the adapters; rate limiting and terms must be respected. The project explicitly warns that scrapers can stop working.
- Preliminary role: **secondary research source and fallback**, after endpoint/coverage tests.
- Source: [`soccerdata` GitHub repository](https://github.com/probberechts/soccerdata), [`soccerdata` source overview](https://soccerdata.readthedocs.io/en/stable/datasources/index.html)

Verdict: **Test Sofascore and FotMob first.** Do not make them the only production dependency until stability and permitted-use questions are understood.

### 3.5 Football-Data.co.uk — excellent free team/odds baseline data

- Type: downloadable CSV/Excel files
- Cost: free
- History: 31 seasons of results, 26 seasons of betting odds, and 26 seasons of match statistics are advertised.
- Relevant fields: regulation scores, shots, corners, fouls, cards, referees, and historical opening/closing bookmaker odds. Major European league match statistics extend back to approximately 2000/01, with broader statistics coverage in newer seasons.
- Strength: ideal for team-level baselines, moneyline/spread evaluation, corner-count training, and comparison against sportsbook odds.
- Weakness: mostly domestic league data; no player-level event history or lineups; limited use for national-team matches.
- Preliminary role: **team model, corner model, and historical market benchmark**.
- Source: [Football-Data.co.uk data page](https://www.football-data.co.uk/data.php)

Verdict: **Use.** It is one of the highest-value free sources for the moneyline/spread/corners side of the project.

### 3.6 football-data.org — reliable basics, insufficient free depth

- Type: authenticated REST API
- Free tier: 12 competitions, delayed scores, fixtures, schedules, tables, and 10 calls/minute.
- Lineups, substitutions, scorers, cards, and squads begin on a paid plan; detailed statistics such as corners and shots require another paid add-on.
- Strength: clean official API for basic schedules/results.
- Weakness: free tier does not contain the player and lineup depth central to this project.
- Preliminary role: optional fixture/result fallback only.
- Source: [football-data.org pricing](https://www.football-data.org/pricing)

Verdict: **Do not use as the primary source.**

### 3.7 TheSportsDB — inexpensive metadata/fallback, uncertain completeness

- Type: crowd-sourced REST API
- Free tier: basic event/player/team search, limited queries, and 30 requests/minute.
- Documentation exposes lineup and event-statistics endpoints, but completeness varies because the database is crowd-sourced and premium access is needed for broader JSON/live functionality.
- Strength: team/player metadata, artwork, schedules, and potentially some lineups.
- Weakness: model training needs consistent completeness, not occasional availability.
- Preliminary role: metadata or emergency fallback.
- Source: [TheSportsDB documentation](https://www.thesportsdb.com/documentation), [TheSportsDB pricing](https://www.thesportsdb.com/docs_pricing)

Verdict: **Low priority; test only after stronger sources.**

### 3.8 OpenFootball / football.json — open score archive only

- Type: public-domain GitHub datasets
- Data: fixtures and results across many leagues/tournaments.
- Strength: no key, open format, useful for filling old score histories.
- Weakness: no lineups, player statistics, xG, event timeline, or corners.
- Source: [openfootball/football.json](https://github.com/openfootball/football.json)

Verdict: **Optional score-history fallback.**

### 3.9 Sportmonks — capable, but not a free foundation

- Type: commercial REST API
- Current entry pricing: paid plan after a 14-day trial; player/team data, lineups, statistics, and broad competition coverage are advertised. xG is an additional bundle.
- Strength: coherent professional feed and strong coverage.
- Weakness: there is no permanent free tier suitable for this project's stated constraint.
- Source: [Sportmonks plans](https://www.sportmonks.com/football-api/plans-pricing/)

Verdict: **Keep as the first paid upgrade path**, not part of the free initial stack.

### 3.10 Polymarket official APIs — market and price source

- Gamma API: market/event discovery, metadata, sports, teams, and market types; public reads.
- CLOB API: order books, bids/asks, midpoint, spread, last trade, and price history; public market-data endpoints, authenticated order endpoints.
- Data API: positions, trades, activity, holders, and open interest.
- Historical price endpoint: accepts token/market ID, time interval, timestamps, and fidelity.
- Strength: official source for tradable market definitions and executable prices.
- Weakness: contract wording and regulation-time resolution must be read per market; a displayed probability is not necessarily an executable price at meaningful size.
- Preliminary role: **authoritative market discovery, rule parsing, prices, liquidity, and eventual execution**.
- Source: [Polymarket API introduction](https://docs.polymarket.com/api-reference/introduction), [Polymarket price history](https://docs.polymarket.com/api-reference/markets/get-prices-history)

Verdict: **Use.**

## 4. Coverage by target market

Legend: P = primary candidate, S = secondary/enrichment, B = baseline, — = not useful.

| Source | Moneyline | Player goals/assists | Spreads | Exact score | First team | Corners | Upcoming lineup |
|---|---:|---:|---:|---:|---:|---:|---:|
| API-Football | P | P | P | P | P | P | P |
| StatsBomb Open | S | S | S | S | S | S | historical only |
| Understat | S | P/S | S | S | — | — | — |
| Sofascore/FotMob scraping | S | P/S | S | S | S | S | P/S |
| Football-Data.co.uk | P/B | — | P/B | P/B | — | P/B | — |
| football-data.org free | S | — | S | S | — | — | — |
| TheSportsDB | S | weak | S | S | weak | weak | uncertain |
| OpenFootball | B | — | B | B | — | — | — |
| Polymarket APIs | market price | market price | market price | market price | market price | market price | — |

## 5. Recommended free-source architecture

No single source should be treated as canonical for everything.

### Layer A — upcoming match and confirmed lineup

1. API-Football as the first-choice current feed.
2. Sofascore or FotMob adapter as a monitored fallback.
3. Store the source, retrieval timestamp, and confirmation status for every lineup.

### Layer B — historical team model

1. Football-Data.co.uk for domestic league results, match statistics, corners, and bookmaker baselines.
2. API-Football historical fixtures where the free plan exposes the required seasons.
3. OpenFootball only for missing score histories.

### Layer C — historical player model

1. API-Football recent per-match player statistics.
2. Understat for xG/xA/shots/minutes in supported club leagues.
3. StatsBomb Open Data for richer event relationships and research.
4. Sofascore/FotMob as a gap-filling experiment.

### Layer D — market data

1. Gamma API to find soccer events and exact contract rules.
2. CLOB API to capture bids, asks, depth, spreads, trades, and price history.
3. Persist local snapshots because source history may not preserve every order-book state needed later.

## 6. Main gaps and risks

1. **National-team player samples are sparse.** Club performance must inform national-team player estimates, which creates cross-competition and role-transfer problems.
2. **Predicted lineups are not confirmed lineups.** Early forecasts require an uncertainty model over starters and minutes.
3. **Free xG/xA coverage is incomplete.** Understat is valuable but narrow; StatsBomb Open Data is detailed but selective.
4. **Entity matching is a real data-engineering problem.** The same player/team has different IDs and spellings across sources. We need internal IDs plus source-specific mappings.
5. **API-Football's free historical access is not yet verified.** Marketing documentation says seasons are limited but does not establish which exact seasons are accessible for every target competition.
6. **Scrapers are operationally fragile.** Every scraped record needs caching, schema checks, throttling, and a replaceable adapter.
7. **Regulation-time semantics must be explicit.** Training targets and Polymarket resolution rules must both exclude extra time and penalties where the contract says regulation time.
8. **Spreads depend on settlement rules.** Asian handicap, European handicap, and binary “win by N+” markets are different transformations of the goal-difference distribution.
9. **A model probability is not a trade price.** Edge calculations must use the actual ask for buying and bid for selling, with available size and fees.

## 7. Concrete validation tests before model selection

The next research step is empirical rather than another comparison article.

### Test A — API-Football coverage probe

Using a free key, inspect several representative fixtures:

- one major club match;
- one international match;
- one less prominent competition;
- one completed fixture from each of the last several seasons.

For each, record whether the API returns fixtures, confirmed lineups, events, player minutes/shots/goals/assists, team corners, injuries, and historical seasons. Also measure calls needed per match.

### Test B — scraper reliability probe

Run `soccerdata` against Sofascore, FotMob, Understat, and ESPN for the same small fixture set. Record available fields, historical reach, response stability, throttling, and identifier quality. Do not bulk scrape until this probe succeeds and source-use constraints are reviewed.

### Test C — Polymarket soccer inventory

Use Gamma sports/events endpoints to enumerate active and recently closed soccer markets. Classify actual market availability into moneyline, spread, scorer, assist, exact score, first team to score, and corners. Save each market's wording and resolution source; do not assume every desired prop is regularly listed.

### Test D — joinability test

Attempt to join one fixture across API-Football, one historical/player source, and Polymarket using team names, kickoff timestamps, competition, and player identities. This will determine the internal entity schema before a large download.

## 8. Initial conclusion

The full idea is feasible as a fun/research project, but **not from one uniformly complete free dataset**. The strongest initial combination is:

- API-Football for upcoming fixtures, official lineups, events, recent player statistics, and corners;
- Football-Data.co.uk for long team/corner/odds histories;
- Understat plus StatsBomb Open Data for player and event-model enrichment;
- carefully tested Sofascore/FotMob scraping as a coverage fallback;
- official Polymarket APIs for markets and executable prices.

The immediate decision is not yet which machine-learning algorithm to use. First, the four validation tests above must establish the actual field coverage and joinability. The model architecture can then be designed against observed data rather than provider feature lists.
