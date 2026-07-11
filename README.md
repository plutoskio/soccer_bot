# Soccer Bot

Local soccer forecasting and Polymarket market-research project.

The intended interactive product and the current build sequence are described
in [PRODUCT_VISION_AND_BUILD_PLAN.md](PRODUCT_VISION_AND_BUILD_PLAN.md).

## Current stage

The repository contains the data-source audit, data architecture, bounded validation harness, versioned DuckDB schema, historical backfill downloader, canonical loaders, entity reconciliation, and quality reporting. Source responses are retained unchanged and can be reprocessed idempotently as mappings and schemas evolve.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

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
`reports/collector/`. Exit code `0` means the run completed (including ordinary
retryable work or lock contention), `1` means a configuration/database/system
failure, and `2` means the run completed but health validation found a blocking
integrity problem.

### Optional macOS scheduling

The tracked example
`ops/launchd/com.soccer-bot.collector.plist.example` runs the restart-safe
collector every five minutes. It contains no API key; the collector continues
to read the local ignored `.env` file. Do not load it until manual observation
cycles and health reports are stable.

To install it manually after that observation period:

```bash
mkdir -p logs ~/Library/LaunchAgents
cp ops/launchd/com.soccer-bot.collector.plist.example \
  ~/Library/LaunchAgents/com.soccer-bot.collector.plist
plutil -lint ~/Library/LaunchAgents/com.soccer-bot.collector.plist
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/com.soccer-bot.collector.plist
```

To stop it before maintenance or warehouse migration:

```bash
launchctl bootout gui/$(id -u) \
  ~/Library/LaunchAgents/com.soccer-bot.collector.plist
```

Rotate `logs/collector.out.log` and `logs/collector.err.log` with a local log
rotation tool. Mac sleep can still miss pregame lineups and contemporaneous
prices; rolling discovery and correction jobs recover post-match facts later.

The intended scheduler interval is five minutes. Waking every five minutes does not mean calling an API every five minutes: DuckDB checkpoints ensure that the process exits without network requests when nothing is due. The script is safe to invoke repeatedly.

Collection scope and timing are configured in `config/collector.json`. The monitored scope includes the World Cup, Euro, Champions League, and configured domestic first divisions represented in the Champions League. Review that list when a new Champions League field is finalized.

The current API-Football Pro configuration supports the multi-fixture `ids`
parameter, so the collector groups up to 20 due fixtures per request. Each
response embeds lineups, events, team statistics, and player statistics. It
reserves 250 of the 7,500 daily calls and spaces requests by one second.
Polymarket order books are independently batched up to 500 outcome tokens per
public request.

The repository does not install an operating-system schedule automatically. On macOS, invoke the run-once command every five minutes with `launchd`; the machine must be awake and online or time-sensitive snapshots will be missed.

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
