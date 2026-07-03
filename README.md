# Soccer Bot

Local soccer forecasting and Polymarket market-research project.

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
python3 -m unittest discover -s tests -v
```

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
