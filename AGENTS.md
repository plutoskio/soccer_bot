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

The data foundation, first leakage-safe modeling dataset, walk-forward score
baselines, calibration layer, rich-rate correction, and market audit are
implemented. The first regulation-moneyline champion has been refit on all
eligible local history, packaged with a reproducibility manifest, and connected
to read-only upcoming-fixture inference.

- Production canonical warehouse: Railway
  `/app/data/warehouse/soccer.duckdb`
- Local warehouse snapshot: `data/warehouse/soccer.duckdb` (not automatically
  synchronized after the Railway migration)
- Production immutable provider responses: Railway `/app/data/raw/`
- Local immutable provider responses: `data/raw/`
- Historical API-Football manifest: Railway and local
  `data/staged/api_football_backfill_manifest.jsonl`
- Collection scope: `config/collector.json`
- Schema/design reference: `DATA_ARCHITECTURE.md`
- Product vision and active roadmap: `PRODUCT_VISION_AND_BUILD_PLAN.md`
- 1,181/1,181 historical backfill batches succeeded
- 23,619/23,619 approved historical fixtures are present
- At the pre-Railway baseline, 23,726 API-Football fixtures existed, including
  107 additional fixtures from watched competitions (audits, qualifiers, and
  current/validation matches). Production counts now change as cron runs; query
  the live warehouse read-only instead of treating this baseline as current.
- 23,526 approved historical fixtures passed all three modeling eligibility
  checks at that baseline.
- The 2026-07-13 local snapshot produces 38,445 reviewed regulation-score
  targets and 73,258 point-in-time team-state rows: 38,445 at T-24h and 34,813
  at the clean T-72h horizon.
- The expanding-window evaluation produces 142,384 prediction rows across
  independent Poisson and Dixon-Coles. The final-test Dixon-Coles deltas are
  slightly favorable, but every paired calendar-month bootstrap interval
  crosses zero; it has not earned promotion over independent Poisson.
- A chronological Understat-xG/API-Football-shots correction passed an internal
  development validation gate. After its recipe was frozen, coefficients were
  refit on all development rows, temperature was fit only on calibration, and
  the final test was scored once. Versus calibrated independent Poisson, log
  loss improved by 0.00453 at T-24h and 0.00434 at clean T-72h; both paired
  month-block 95% intervals exclude zero. This is the current champion recipe.
- Strict timestamped Polymarket three-way coverage is currently zero complete
  eligible fixtures. Football-Data closing consensus covers 12,458 fixtures,
  but its missing quote timestamps make it a retrospective benchmark only,
  never a model feature.
- The 2026-07-15 all-history refit uses 38,445 T−24h and 34,813 clean T−72h
  rows. The upcoming inference path requires the current kickoff to have been
  known by the exact horizon cutoff and never creates fake scores for unplayed
  fixtures. See `REGULATION_CHAMPION_MODEL.md`.
- 180 tests pass.

The database also contains useful observations from Football-Data.co.uk,
Understat, StatsBomb Open Data, and Polymarket.

## Production Environment — Railway

Railway is the production collector host. The initial warehouse, 3,345 raw
files, and three staging files were uploaded and verified on 2026-07-11.
A supervised production collection run then completed successfully, with no
blocking health condition and with new raw evidence persisted. The tracked
production deployment subsequently reached Railway status `SUCCESS`.

- Railway service: `soccer_bot`
- Railway environment: `production`
- Deployment source: connected GitHub `main` branch
- Deployment definition: `railway.json`
- Build: Railpack followed by `python -m pip install .`
- Start command: `python scripts/run_collector.py`
- Schedule: every five minutes (`*/5 * * * *`)
- Restart policy: `NEVER` because this is a run-once cron process
- Persistent volume mount: `/app/data`
- Volume capacity: 10 GB; 3,996.7 MB was used immediately after the
  2026-07-15 online resize and the volume reported `Ready`
- Required collector variables: `API_FOOTBALL_KEY` plus the snapshot bucket
  references documented in `RAILWAY_APPLICATION_DEPLOYMENT.md`; never print
  their values
- Operations and recovery runbook: `RAILWAY_OPERATIONS.md`

Persistent production paths are:

- Warehouse: `/app/data/warehouse/soccer.duckdb`
- Collector lock: `/app/data/warehouse/collector.lock`
- Raw evidence: `/app/data/raw/`
- Staged manifests: `/app/data/staged/`
- Health reports: `/app/data/reports/collector/`
- Prediction publication receipts: `/app/data/reports/predictions/publication.jsonl`

Everything outside `/app/data` is disposable between Railway deployments.
In particular, a report written to `/app/reports` would be lost. The collector
configuration intentionally uses `data/reports/collector` so the resolved
Railway path is on the volume.

The local checkout is already linked through the Railway CLI. Useful read-only
status commands are:

```bash
railway service status --json
railway logs
```

Do not assume the local DuckDB contains observations collected after the cloud
migration. For current production facts, inspect the Railway volume with the
cron disabled/stopped and DuckDB opened using `read_only=True`. Never run a
second writable collector locally while treating Railway as production. The
repository intentionally has no alternative local scheduler.

Before a live inspection or migration:

1. Disable or remove the cron schedule and confirm no collector is running.
2. Preserve a verified Railway volume/database backup.
3. Use a temporary inspection deployment such as `sleep infinity`.
4. Open `/app/data/warehouse/soccer.duckdb` explicitly read-only.
5. Restore the committed `railway.json` immediately after inspection.

Do not use `railway up` casually: it creates a deployment and can execute the
configured start command against the live volume. Do not upload a warehouse,
raw directory, or staged directory over the active production paths without a
stopped scheduler, a verified backup, exact path review, and a rollback plan.

The guarded publisher rollout retained a verified compressed DuckDB backup at
`data/backups/production/soccer-20260715T200224Z.duckdb.gz`; its decompressed
SHA-256 is recorded in `RAILWAY_APPLICATION_DEPLOYMENT.md`. Railway Pro was
enabled on 2026-07-15, the production volume was resized online from 5 GB to
10 GB, and Railway created the 3.91 GB manual restore point `Online resize to
10000MB`. Native daily backups are enabled with Railway's six-day retention.
The Backups UI exposes `Restore` and a delete-only actions menu for this manual
restore point; it does not expose a separate lock toggle. Volume-usage alerts
are enabled at 80%, 95%, and 100%; the account cost warning/limit described in
`RAILWAY_OPERATIONS.md` remains an account-level follow-up. Quarantined
bootstrap directories named `/app/data/bootstrap-warehouse-20260711` and
`/app/data/bootstrap-raw-20260711` must not be deleted until a restoration test
has also been completed.

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

One-time repair scripts are archived under `scripts/maintenance/one_time/`.
Do not rerun them unless their guards and current database state have been
reviewed first.

## Known, Controlled Limitations

- The warehouse is clean and structurally consistent, but provider coverage is
  not perfect. Open warnings document administrative matches, unavailable
  provider sections, low passing coverage, duplicate provider lineup entries,
  and unresolved lineup aliases.
- One watched Czech audit fixture (`API-Football 1049556`) has complete result,
  lineups, team stats, goals, and substitutions but no player minutes. It is
  result/team eligible and player ineligible.
- The collector is a restart-safe, locked run-once program with rolling
  recovery, staged lineup/post-match/Polymarket jobs, bounded HTTP retries, and
  daily health reporting. Railway is the production host: `/app/data` is the
  persistent volume and the tracked `railway.json` schedules the collector
  every five minutes. No second production scheduler is supported.
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

For Railway changes, also validate:

```bash
git diff --check
.venv/bin/python scripts/run_collector.py --dry-run
railway service status --json
```

The user prefers to run commands that take several minutes (large uploads,
full builds/deployments, or prolonged monitoring) themselves. Explain the
command, expected output, and safe stopping condition before asking them to run
it. Short read-only checks can be run directly.

## Recommended Next Work

Follow `PRODUCT_VISION_AND_BUILD_PLAN.md`, `FORECASTING_SYSTEM_DESIGN.md`, and
the reviewed scope in `PREDICTION_CONTRACT_CATALOG.md`. The `CORE` regulation
contract registry, score-grid settlement layer, target task, target builder,
and first chronological team-state feature builder are implemented. The
builder and frozen manifest create clean T-72h/T-24h snapshots, delay result
availability, batch simultaneous kickoffs, and expose dynamic team state and
coverage features. Calibration, market audit, champion refit, immutable
manifest, upcoming-fixture inference, and the first Railway fixture-selection
deployment are complete. The public web service is
`https://soccer-bot-web-production.up.railway.app`; its API is private and reads
the immutable snapshot from Railway object storage. Guarded automatic
publication was first activated in collector deployment
`c314a7c9-53c7-4541-9b90-1c1e136ff268`; current verified deployment
`6251e139-5b6f-4910-9dba-472a634d71bd` runs exact source commit
`e2c756cb802835e882216521d2f2f6f6f8b4cea8`. The first cycle published 14 rows across
13 fixtures as-of `2026-07-15T20:27:40.917313Z` and passed browser QA. The live
UI now separates horizon-wide training size
(38,445 T−24; 34,813 clean T−72) from selected-team result and rich-signal
history, with labels derived from the frozen 1,000/5/20 evidence thresholds.
Do not tune further against the current final-test report. The collector volume
resize, native daily backup schedule, guarded publication, and source commit are
complete. Next add publication-failure/staleness alerting, test a restore into
an isolated volume, continue collecting complete timestamped Polymarket books,
and begin confirmed-lineup/player research under a new forward or nested
evaluation window. Treat T−24h as a comparable
pre-lineup anchor, not a separate model for every hour. Keep result, team, and
player datasets separate where their eligibility requirements differ.
