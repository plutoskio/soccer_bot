# Railway Collector Operations

This document is the production runbook for the Soccer Bot collector. Railway
runs the collector; DuckDB remains the canonical warehouse. Supabase and other
databases are not part of this deployment.

## Production layout

- Railway service: `soccer_bot`
- Railway environment: `production`
- Persistent volume mount: `/app/data`
- Warehouse: `/app/data/warehouse/soccer.duckdb`
- Immutable provider responses: `/app/data/raw/`
- Historical staging files: `/app/data/staged/`
- Daily Markdown health report: `/app/data/reports/collector/`
- Start command: `python scripts/run_collector.py`
- Schedule: every five minutes (`*/5 * * * *`)
- Restart policy: never

The process is intentionally run-once. It plans due work, acquires the collector
lock, performs bounded requests, commits state, writes health information, and
exits. Railway starts a new process at the next scheduled time.

## Required configuration

The service must have one secret variable named `API_FOOTBALL_KEY`. Never print
its value, copy it into `railway.json`, add it to a command line, or commit it.
The repository's `.env` is only for local execution and is ignored by Git.

Only one service may write to this volume. Do not create a scheduled local
collector while Railway is production.

## Normal deployment

Changes pushed to the connected GitHub `main` branch are deployed by Railway.
Before pushing collector or migration changes, run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/run_collector.py --dry-run
git status --short
git diff --check
```

The dry run reads the local warehouse, not the Railway volume. A production
deployment must be observed in Railway logs after any collector, migration, or
configuration change.

## Routine health checks

Check the current service state:

```bash
railway service status --json
```

Review recent Railway logs in the dashboard. A successful collection run has:

- deployment status `SUCCESS`;
- a completed `collection_run` row;
- `blocking_reason: null`;
- exit code `0`;
- no traceback or secret-bearing output.

Health severity `warning` is not automatically a failed run. Current controlled
warnings include unresolved historical player aliases, missed pregame captures
for fixtures first observed after the deadline, and retryable provider sections.
Severity `blocking` or process exit code `2` requires investigation before the
next schema or collector deployment.

## Read-only warehouse inspection

Do not inspect the live DuckDB file while the collector is writing. Temporarily
disable or stop scheduled executions, deploy an inspection process whose start
command is `sleep infinity`, then connect with DuckDB read-only:

```python
import duckdb

connection = duckdb.connect(
    "/app/data/warehouse/soccer.duckdb",
    read_only=True,
)
```

Restore the tracked `railway.json` immediately afterward. An inspection must
not run migrations, `CHECKPOINT`, `VACUUM`, repair scripts, or the collector.

## Backups and recovery

Enable Railway volume backups in the project dashboard before treating the
service as unattended production. Use a daily backup if the plan permits it,
retain at least one known-good backup, and test restoration to a separate
volume or downloaded copy. Configure a cost warning near USD 7 and a hard
monthly limit of USD 10 if those controls are available for the account.

Before any migration or manual warehouse repair:

1. Disable the cron schedule and confirm the service is stopped.
2. Create a volume backup.
3. Copy or download `soccer.duckdb` and record its byte size and SHA-256 hash.
4. Test the migration or repair against a copy.
5. Apply it with transactional, expected-count guards.
6. Run integrity, eligibility, scope, and unrelated-table comparisons.
7. Restore the cron schedule only after validation.

Never overwrite a live warehouse with a local file unless the cron is disabled,
the remote database has a verified backup, and a rollback copy is available.
Raw artifacts are immutable and must remain paired with the warehouse's
provenance rows.

## Failure handling

- `already_running`: normal lock protection; the invocation exits successfully.
- Retryable provider errors or rate limits: leave checkpoints scheduled; the
  next cron run resumes them.
- Missing API key, migration failure, database-open failure, or traceback:
  disable cron, preserve logs, and diagnose before restarting.
- Blocking health report: disable cron if continued writes could compound an
  integrity problem; inspect the required invalid components read-only.
- Lost or corrupt volume: do not initialize an empty replacement as production.
  Stop the service, restore the latest verified warehouse and its raw/staged
  artifacts, validate read-only, and then resume collection.

## Removing migration quarantine directories

The directories `/app/data/bootstrap-warehouse-20260711` and
`/app/data/bootstrap-raw-20260711` contain the quarantined accidental empty
bootstrap state. Remove them only after automatic cron runs and a volume backup
have both been verified. Do not delete the active `warehouse`, `raw`, `staged`,
or `reports` directories.
