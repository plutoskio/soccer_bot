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
- Prediction publication receipts: `/app/data/reports/predictions/publication.jsonl`
- Current prediction-operations status: `/app/data/reports/operations/current.json`
- Prediction alert transitions: `/app/data/reports/operations/events.jsonl`
- Start command: `python scripts/run_collector.py`
- Schedule: every five minutes (`*/5 * * * *`)
- Restart policy: never

The process is intentionally run-once. It plans due work, acquires the collector
lock, performs bounded requests, commits state, writes health information,
closes DuckDB, and then publishes a validated prediction snapshot while still
holding the same lock. Railway starts a new process at the next scheduled time.

## Required configuration

The service must have `API_FOOTBALL_KEY` plus the snapshot bucket variables
listed in `RAILWAY_APPLICATION_DEPLOYMENT.md`. Bucket credentials must be
Railway reference variables, not copied values. Never print secret values, copy
them into `railway.json`, add them to a command line, or commit them. The
repository's `.env` is only for local execution and is ignored by Git.

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
- no traceback or secret-bearing output;
- `prediction_publication.status: uploaded`, unless the run has blocking health.

Health severity `warning` is not automatically a failed run. Current controlled
warnings include unresolved historical player aliases, missed pregame captures
for fixtures first observed after the deadline, and retryable provider sections.
Severity `blocking` or process exit code `2` requires investigation before the
next schema or collector deployment.

Publication is deliberately failure-isolated: generation, validation, upload,
or receipt-I/O failure does not undo a successful collection run. A candidate
is uploaded only after model-version, logical-model-hash, cutoff, horizon,
fixture-time, uniqueness, and minimum-row guards pass. The uploaded object is
then read back, compared byte-for-byte, and revalidated. If any guard fails,
the previous object remains the application snapshot and the sanitized failure
appears in the collector summary and append-only receipt.

Prediction operations are monitored separately from general collection health.
The in-process watchdog validates publication freshness, champion and shadow
identity, row counts, champion-shadow row parity, receipt durability, and
mounted-volume capacity. A critical condition exits with code `3` after
collection has been safely committed. An independent GitHub Actions monitor
runs every 15 minutes against the public snapshot and opens one deduplicated
issue if the Railway cron stops refreshing it. Full thresholds, alert codes,
transition semantics, and incident procedures are in
`OPERATIONAL_ALERTING.md`.

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

Railway Pro was enabled on 2026-07-15. The production volume was resized online
from 5 GB to 10 GB and immediately reported `Ready`, with 3,996.7 MB used.
Railway created a 3.91 GB manual restore point named `Online resize to
10000MB`, and native daily backups are enabled with six-day retention. The
Backups UI exposes `Restore` and a delete-only actions menu for the manual
restore point; it does not expose a separate lock control. Do not delete it
until an isolated restoration test has succeeded. Volume-usage alerts are
enabled at 80%, 95%, and 100%. Configure an account cost warning near USD 7 and
a hard monthly limit of USD 10 if those controls are available.

The guarded publisher rollout retained a verified local compressed database
backup at `data/backups/production/soccer-20260715T200224Z.duckdb.gz`.
Decompressed size is 2,889,363,456 bytes and SHA-256 is
`36269c7b4fcb79aeef001fe626c5be9a337ba4df981035a022192c92fc1ea760`.
This is an independently verified database rollback copy. It complements the
scheduled native backup of every file on the production volume.

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
- Prediction publication failure: keep the prior snapshot, inspect the
  sanitized `prediction_publication` result and persistent receipt, and fix the
  producer or bucket without republishing an unreviewed file manually.
- Operational exit code `3`: inspect both `prediction_publication` and
  `operational_watchdog`; preserve valid parent output, and never overwrite an
  immutable shadow artifact to clear the alert.
- GitHub issue `[operations] Soccer Bot prediction watchdog`: treat it as an
  external stale-heartbeat incident and verify the Railway cron and exact
  source commit before attempting a restart.
- Lost or corrupt volume: do not initialize an empty replacement as production.
  Stop the service, restore the latest verified warehouse and its raw/staged
  artifacts, validate read-only, and then resume collection.

## Removing migration quarantine directories

The directories `/app/data/bootstrap-warehouse-20260711` and
`/app/data/bootstrap-raw-20260711` contain the quarantined accidental empty
bootstrap state. Remove them only after automatic cron runs and a volume backup
have both been verified. Do not delete the active `warehouse`, `raw`, `staged`,
or `reports` directories.
