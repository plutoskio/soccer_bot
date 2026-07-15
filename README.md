# Soccer Bot

Local soccer forecasting and Polymarket market-research project.

The intended interactive product and current build sequence are described in
[PRODUCT_VISION_AND_BUILD_PLAN.md](PRODUCT_VISION_AND_BUILD_PLAN.md). The
detailed quantitative architecture, model research standards, and probability
roadmap are defined in
[FORECASTING_SYSTEM_DESIGN.md](FORECASTING_SYSTEM_DESIGN.md).
The reviewed market scope, inclusion decisions, settlement defaults, and engine
dependencies are recorded in
[PREDICTION_CONTRACT_CATALOG.md](PREDICTION_CONTRACT_CATALOG.md).
The selected regulation model, production-refit policy, inference gates, and
current parameters are documented in
[REGULATION_CHAMPION_MODEL.md](REGULATION_CHAMPION_MODEL.md).

## Current stage

The repository contains the data-source audit, data architecture, bounded
validation harness, versioned DuckDB schema, historical backfill downloader,
canonical loaders, entity reconciliation, quality reporting, the first
versioned regulation contract registry, deterministic score-grid settlement,
the regulation-score target builder, chronological feature construction,
walk-forward evaluation, calibration, and market benchmarks. The current
champion is a temperature-calibrated independent-Poisson score model corrected
by chronological Understat xG and API-Football shots signals. Its T-72h and
T-24h features cannot use the target match or a result that was unavailable at
the prediction cutoff. Source responses are retained unchanged and can be
reprocessed idempotently as mappings and schemas evolve.

The frozen champion has also been refit on all eligible local history and an
upcoming-fixture snapshot command is implemented. The generated artifacts stay
under ignored `data/models/` and `data/predictions/` directories.

The first custom application vertical slice is also implemented. A read-only
FastAPI service validates the champion snapshot and exposes only supported
regulation-moneyline prices. A custom Next.js interface provides fixture and
T−72/T−24 selection, fair odds, evidence coverage, calibration movement, model
identity, and warnings. Neither application service opens DuckDB.

The probability desk reports both horizon-wide training size and selected-team
history. It interprets these counts only against thresholds frozen in the model
recipe, so a 38,445-fixture global training set is not presented as a substitute
for sparse team-specific history.

The first controlled Railway rollout is live at
<https://soccer-bot-web-production.up.railway.app>. The web service calls the
private API over Railway networking, and the API reads the immutable prediction
snapshot from object storage. The existing collector and its writer volume were
not queried by the application services. Guarded post-collection publication is
implemented so the sole production collector can refresh the validated object
after closing DuckDB while retaining its inter-process lock. The first guarded
cycle published a fresh 13-fixture snapshot and passed browser QA.

Freeze the current local modeling dataset and its reproducibility manifest:

```bash
.venv/bin/python scripts/build_regulation_modeling_dataset.py
```

Run the independent-Poisson and Dixon-Coles expanding-window baselines:

```bash
.venv/bin/python scripts/evaluate_regulation_baselines.py
```

Research the frozen xG/shots correction inside development only, then run its
promotion-gated calibration and final evaluation:

```bash
.venv/bin/python scripts/research_rich_rate_features.py
.venv/bin/python scripts/evaluate_promoted_rich_rate_model.py
```

Audit the strict point-in-time Polymarket benchmark and the explicitly
retrospective bookmaker-closing yardstick:

```bash
.venv/bin/python scripts/evaluate_market_benchmarks.py
```

Refit the already-selected champion and generate an upcoming snapshot:

```bash
.venv/bin/python scripts/fit_regulation_champion.py
.venv/bin/python scripts/predict_upcoming_regulation.py
```

These commands read the warehouse without modifying it. Generated Parquet,
manifests, predictions, and metric reports are written under the ignored
`data/features/regulation_team_state_v1/` directory.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Run the custom application

Install the API and web dependencies:

```bash
.venv/bin/pip install -e '.[api]'
npm --prefix apps/web install
```

Start the API from the repository root:

```bash
.venv/bin/uvicorn apps.api.main:app --reload --port 8000
```

In another terminal, start the web interface:

```bash
npm --prefix apps/web run dev
```

Open `http://localhost:3000`. The API reads the ignored local
`data/predictions/regulation_champion_v1/latest.json` by default. Railway uses
object storage and separate service definitions; see
[RAILWAY_APPLICATION_DEPLOYMENT.md](RAILWAY_APPLICATION_DEPLOYMENT.md).

## Run validation

The API-Football key belongs in `.env`:

```dotenv
API_FOOTBALL_KEY=...
```

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Incremental collector

The collector is a restart-safe `run-once` process. It verifies fixture discovery across a 14-day recovery window and seven-day planning window, automatically extending recovery to the latest monitored completed-fixture frontier so model history is not skipped after downtime. Today and tomorrow refresh every six hours. Confirmed-lineup stages run at T-50, T-35, T-20, and T-5, stopping after two valid starting elevens. Polymarket events are rediscovered every 15 minutes on match days and hourly within seven days, with order books captured at the configured pregame, post-lineup, and closure stages.

Preview currently due work without an API key, network requests, or database writes:

```bash
.venv/bin/python scripts/run_collector.py --dry-run
```

Explicitly extend recovery when needed:

```bash
.venv/bin/python scripts/run_collector.py --catch-up-days 30
```

Execute one collection cycle:

```bash
.venv/bin/python scripts/run_collector.py
```

Each writable run acquires `data/warehouse/collector.lock` before opening
DuckDB. A concurrent invocation exits successfully with an `already_running`
summary. Temporary provider failures and rate limits remain scheduled for
retry; ordinary per-job failures do not abort unrelated batches.

The collector writes a machine-readable daily health row to
`collection_health_report` and an ignored generated report under
`data/reports/collector/`. On Railway this directory is part of the persistent
`/app/data` volume. Exit code `0` means the run completed (including ordinary
retryable work or lock contention), `1` means a configuration/database/system
failure, and `2` means the run completed but health validation found a blocking
integrity problem.

### Production scheduling on Railway

Production collection runs as a Railway cron job using the tracked
`railway.json`. Railway mounts the persistent volume at `/app/data`, starts
`python scripts/run_collector.py` every five minutes, and expects the run-once
process to exit. `restartPolicyType` is deliberately `NEVER`; a later cron
invocation handles retryable work, while the warehouse checkpoint state and
collector lock make repeated or overlapping invocations safe.

The collector requires `API_FOOTBALL_KEY` plus private snapshot-bucket reference
variables, configured in Railway and never committed. The DuckDB warehouse,
immutable raw responses, staged manifest, lock, health reports, and publication
receipts all live below `/app/data`. Do not deploy a second service that writes
to the same volume.

Operational checks, deployment commands, maintenance steps, and recovery rules
are documented in [RAILWAY_OPERATIONS.md](RAILWAY_OPERATIONS.md).

The scheduler interval is five minutes. Waking every five minutes does not mean
calling an API every five minutes: DuckDB checkpoints ensure that the process
exits without network requests when nothing is due. The script is safe to
invoke repeatedly.

Collection scope and timing are configured in `config/collector.json`. The monitored scope includes the World Cup, Euro, Champions League, and configured domestic first divisions represented in the Champions League. Review that list when a new Champions League field is finalized.

The current API-Football Pro configuration supports the multi-fixture `ids`
parameter, so the collector groups up to 20 due fixtures per request. Each
response embeds lineups, events, team statistics, and player statistics. It
reserves 250 of the 7,500 daily calls and spaces requests by one second.
Polymarket order books are independently batched up to 500 outcome tokens per
public request.

Railway is the only production scheduler. Do not create a second scheduled
local collector: the local and cloud warehouses are separate copies and would
diverge.

## Historical API-Football coverage audit

Before a paid historical backfill, audit provider-declared and observed player-match coverage:

```bash
.venv/bin/python scripts/audit_historical_coverage.py
```

The audit caches every successful response in the raw archive. It uses one fixture-list request and one deterministic ten-match detail batch per eligible league-season. Rerunning it makes no network calls unless the configuration changes or a response is missing.

Targets and season depth are configured in `config/api_football_coverage_audit.json`. Results are written to `reports/API_FOOTBALL_HISTORICAL_COVERAGE.md` and its machine-readable JSON companion. Only seasons graded `PASS` should enter an automatic backfill. Champions League qualifying and preliminary rounds are excluded because empirical player-stat coverage is inconsistent; the main tournament is assessed separately.

Build the fixture-level backfill manifest from the approved seasons and cached
fixture lists without making API requests:

```bash
.venv/bin/python scripts/build_backfill_manifest.py
```

The review report is written to `reports/API_FOOTBALL_BACKFILL_MANIFEST.md`.
Execution batches of at most 20 fixture IDs are written under `data/staged/`.
Generating the manifest does not execute those batches.

Preview the next pending historical batch without network or database writes:

```bash
.venv/bin/python scripts/run_historical_backfill.py --max-batches 1
```

Execute exactly one validated batch:

```bash
.venv/bin/python scripts/run_historical_backfill.py --execute --max-batches 1
```

The executor validates manifest membership, returned fixture IDs, competition,
season, teams, kickoff, final score, lineup structure, team statistics, player
participation, and critical player values. Passing coverage is measured but is
not a blocking ingestion condition: coverage below the configured 80% threshold
creates an open `low_player_passing_coverage` warning, and missing values remain
`NULL`. Relational writes are transactional. A batch is checkpointed only after
its stored raw response and DuckDB rows both pass validation. Completed batches
are skipped on restart; failed batches require the explicit `--retry-failed`
flag.

API-Football player-stat identities use `(provider player ID, normalized
provider name)` because historical payloads can reuse one numeric ID for
different people. Lineup and event IDs are isolated and linked to player-stat
identities only through unique fixture-and-team context. Display names are
never used for global automatic merging; cross-source player linkage requires
a separate reviewed identity decision. Contextual comparison transliterates
standalone Latin letters such as `æ`, `ø`, `œ`, `ł`, `ð`, `þ`, and `ß`, but
canonical names and stable identity keys remain unchanged. Historical links
missed by this distinction—or by provider sections shortening different parts
of a compound surname are handled by the current contextual player linker.
Compound-surname matching additionally requires an equal shirt number and one
unique candidate in the same fixture and team. The completed repair procedures
that introduced these rules remain documented in the corresponding reports and
Git history; they are no longer routine commands.

Run individual probes:

```bash
python3 scripts/probe_sources.py api-football
python3 scripts/probe_sources.py polymarket
python3 scripts/probe_sources.py bootstrap
python3 scripts/probe_sources.py understat
```

Run both and rebuild the report:

```bash
python3 scripts/probe_sources.py all
```

Build or refresh the canonical DuckDB warehouse from all retained raw artifacts:

```bash
.venv/bin/python scripts/build_database.py
```

The build is idempotent. It writes the ignored local database to `data/warehouse/soccer.duckdb` and the tracked summary to `reports/DATABASE_COVERAGE_REPORT.md`.

Current canonical coverage is summarized in [reports/DATABASE_COVERAGE_REPORT.md](reports/DATABASE_COVERAGE_REPORT.md). Provider-specific IDs are retained in `source_entity_map`; configured cross-source name aliases live in `config/entity_aliases.json`.

Download the curated top-five-league historical backfill, then refresh DuckDB:

```bash
.venv/bin/python scripts/backfill_history.py all
.venv/bin/python scripts/build_database.py
```

The downloader skips successful requests already present in `data/raw/`. Its bounded league/season scope and pacing are configured in `config/backfill.json`.

Raw payloads are written under `data/raw/` and ignored by Git. The generated summary is `reports/SOURCE_VALIDATION_REPORT.md`.

The probe limits are controlled by `config/probe_cases.json`. Keep them low until coverage and provider quotas are verified.
