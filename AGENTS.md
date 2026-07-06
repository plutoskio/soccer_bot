# Soccer Bot — Agent Context

## Purpose

Build a soccer forecasting system for researching Polymarket bets. The model
should use confirmed lineups and player-level histories to estimate:

- Regulation moneyline and spreads (highest priority)
- Player goals and assists (highest priority)
- Exact score, corners, and first team to score

This is an experimental project, not a claim of guaranteed betting edge. Data
correctness and leakage prevention take priority over quickly fitting a model.

## Current Stage

The data foundation is built; modeling has not started.

- Canonical warehouse: `data/warehouse/soccer.duckdb`
- Immutable provider responses: `data/raw/`
- Historical API-Football manifest: `data/staged/api_football_backfill_manifest.jsonl`
- Collection scope: `config/collector.json`
- Schema/design reference: `DATA_ARCHITECTURE.md`
- 1,181/1,181 historical backfill batches succeeded
- 23,619/23,619 approved historical fixtures are present
- 23,726 API-Football fixtures exist, including 107 additional fixtures from
  watched competitions (audits, qualifiers, and current/validation matches)
- 23,526 approved fixtures pass all three modeling eligibility checks
- 53 tests pass

The database also contains useful observations from Football-Data.co.uk,
Understat, StatsBomb Open Data, and Polymarket.

## Modeling Eligibility

Always start dataset construction from the `fixture_model_eligibility` view.
It exposes exactly three consumer-facing flags:

- `eligible_result_models`
- `eligible_team_models`
- `eligible_player_models`

`reason_codes` explains broad exclusions. Detailed provider anomalies remain
in `data_quality_issue`; those rule codes are diagnostics, not extra model
flags. Feature SQL must still require each feature column to be non-null.

Examples:

- Moneyline/spread/exact score: require `eligible_result_models`
- Corners/team-stat models: require `eligible_team_models`
- Player goals/assists/minute features: require `eligible_player_models`

Administrative results are excluded from sporting-performance training.

## Data Architecture and Invariants

- DuckDB is relational and uses canonical IDs for competitions, seasons,
  teams, players, and fixtures.
- Provider IDs map through `source_entity_map`; do not join providers by names.
- Raw artifacts are immutable evidence. Never edit raw JSON/CSV to repair a
  normalized observation.
- Missing values stay `NULL`; do not invent zeroes or assumed minutes.
- Corrections must be evidence-backed, narrowly scoped, transactional, and
  preceded by a verified database backup.
- Historical loaders and repairs are intended to be idempotent or explicitly
  guarded by fixture IDs, raw hashes, and before/after invariants.
- Do not train directly from every row in `fixture`; use eligibility and an
  explicit dataset manifest/cutoff policy.

## Important Completed Repairs

- Removed 477 shallow fixtures from unrelated competitions discovered through
  unfiltered daily API responses.
- Removed their unused dimensions: 79 competitions, 79 seasons, and 846 teams.
- Repaired seven provider responses whose player-stat blocks were assigned to
  the opposing team. This was a one-time, fixture-specific repair; no future
  automatic swap behavior was added.
- Corrected West Ham total shots from 8 to 18 for Newcastle–West Ham on
  2021-08-15, corroborated by API-Football. The original CSV remains intact.
- Added migration `006_fixture_model_eligibility.sql` and regression tests.

Do not rerun one-time repair scripts unless their guards and current database
state have been reviewed first.

## Known, Controlled Limitations

- The warehouse is clean and structurally consistent, but provider coverage is
  not perfect. Open warnings document administrative matches, unavailable
  provider sections, low passing coverage, duplicate provider lineup entries,
  and unresolved lineup aliases.
- One watched Czech audit fixture (`API-Football 1049556`) has complete result,
  lineups, team stats, goals, and substitutions but no player minutes. It is
  result/team eligible and player ineligible.
- The collector is a restart-safe run-once program; an operating-system
  scheduler has not been installed.
- A complete replay of all raw daily-discovery artifacts can reintroduce
  out-of-scope shallow fixtures because raw responses intentionally retain all
  competitions. Apply the configured competition boundary during any future
  rebuild before replacing the live database.
- The project covers configured World Cup, Euro, Champions League, and selected
  domestic first divisions. It is not global coverage; MLS is not currently in
  scope.

## Safe Working Practices

Before changing the warehouse:

1. Inspect provenance and all referencing tables.
2. Back up `soccer.duckdb` and verify the backup hash.
3. Test risky repairs against a copied database.
4. Use a transaction with strict expected-count guards.
5. Compare unrelated tables before and after.
6. Run the complete test suite.

Primary validation command:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Useful collector commands:

```bash
.venv/bin/python scripts/run_collector.py --dry-run
.venv/bin/python scripts/run_collector.py
```

Never expose `.env` or API keys in logs, reports, tests, or commits.

## Recommended Next Work

Design the first frozen, leakage-safe model dataset from
`fixture_model_eligibility`, beginning with regulation moneyline/spread targets
and pre-match team/player features. Define feature cutoff times and dataset
manifests before training any model. Keep result, team, and player datasets
separate where their eligibility requirements differ.
