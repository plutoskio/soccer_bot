# Railway Collector Operations

> Market-data update (2026-07-21): all Polymarket collection, evidence
> publication, settlement, and evaluation jobs are disabled. Existing paths
> below are historical audit artifacts and must not be deleted until a stopped-
> scheduler backup/reference audit is complete. Active market collection is
> limited to API-Football Match Winner snapshots at frozen T−72h/T−24h cutoffs.

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
- Immutable Polymarket prediction/book evidence: `/app/data/predictions/polymarket_market_evidence_v1/evidence/`
- Count-only Polymarket coverage: `/app/data/predictions/polymarket_market_evidence_v1/coverage.json`
- Polymarket evidence receipts: `/app/data/predictions/polymarket_market_evidence_v1/receipts.jsonl`
- Current private v3 view: `/app/data/predictions/regulation_score_grid_v3_shadow/latest.json`
- Immutable v3 evidence: `/app/data/predictions/regulation_score_grid_v3_shadow/evidence/`
- V3 evidence receipts: `/app/data/predictions/regulation_score_grid_v3_shadow/receipts/`
- Prospective settlement ledger: `/app/data/predictions/regulation_score_grid_v3_settlement/ledger.jsonl`
- Count-only v3 evaluation readiness: `/app/data/predictions/regulation_score_grid_v3_evaluation/readiness.json`
- Write-once v3 evaluation decision: `/app/data/predictions/regulation_score_grid_v3_evaluation/decision.json`
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
identity, row counts, champion-shadow row parity, Polymarket evidence policy
identity/count/safety invariants, settlement completion, count-only evaluation
readiness, anti-peeking/config/ledger-count guards, receipt durability, and
mounted-volume capacity. A critical condition exits
with code `3` after
collection has been safely committed. An independent GitHub Actions monitor
runs every 15 minutes against the public snapshot and opens one deduplicated
issue if the Railway cron stops refreshing it. Full thresholds, alert codes,
transition semantics, and incident procedures are in
`OPERATIONAL_ALERTING.md`.

### Confirmed-lineup player shadow activation

`prediction_publication.confirmed_lineup_player_shadow` is packaged and enabled
in the pending local implementation, but is not yet deployed. Its compressed
artifact was built from the frozen local target snapshot with the exclusive
2026-07-15 fit cutoff and carries a logical hash checked by the collector. Do
not deploy this configuration without the normal stopped-cron/current-backup
procedure and an exact comparison of target, manifest, config, warehouse,
model-file, and logical-model provenance.

Before enablement, verify:

1. the readiness audit reports no leakage claim for historical lineups;
2. the logical model hash equals collector configuration;
3. `apply_to_public_champion` is false;
4. unconditional substitute props and first scorer are false;
5. the first enabled cycle with no eligible lineup returns
   `no_eligible_confirmed_lineups`, not a failure;
6. the first eligible cycle writes one immutable record strictly between the
   T−24 parent timestamp and kickoff;
7. repeated execution verifies rather than overwrites that record;
8. champion publication remains byte-identical and independently successful.

Player shadow output persists under
`/app/data/predictions/confirmed_lineup_player_v1`. Never copy a locally fitted
model into production without the stopped-cron backup procedure and exact
provenance review. Full scientific and mathematical details are in
`CONFIRMED_LINEUP_PLAYER_MODEL.md`.

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
10000MB`. After usage reached 84%, it was live-resized again from 10 GB to
20 GB on 2026-07-21 and reported `Ready`, with 8,448.5 MB stored. Railway bills
the bytes stored rather than the configured maximum. Native daily backups are
enabled with six-day retention. The
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
- Polymarket evidence failure: preserve all raw books and existing immutable
  evidence, inspect the sanitized publication receipt and policy hash, and fix
  the mapping/pairing producer. Never edit an evidence JSON to clear an alert.
- Confirmed-lineup player shadow failure: preserve the champion snapshot and all
  immutable player evidence, inspect the model/config hashes and lineup timing
  gates, and keep the shadow disabled if identity or provenance is uncertain.
  Never set champion authorization true to clear an alert.
- `polymarket_pre_cutoff_capture_gap`: warning only. A complete semantic
  moneyline mapping existed but one or more required timing-safe books were
  absent. Inspect the stage checkpoint, raw response, retry timing, kickoff
  version, and token coverage before the next cutoff.
- Operational exit code `3`: inspect both `prediction_publication` and
  `operational_watchdog`; preserve valid parent output, and never overwrite an
  immutable shadow evidence file or settlement record to clear the alert.
- Prospective settlement failure: preserve the existing ledger, inspect the
  sanitized receipt and frozen hashes, and correct the producer. Never delete,
  truncate, reorder, or hand-edit `ledger.jsonl`.
- Prospective evaluation-readiness failure: preserve the ledger and both
  evaluator artifacts, inspect frozen hashes and the sanitized readiness
  receipt, and correct the producer. Never hand-edit readiness to suppress an
  alert.
- `prospective_evaluation_ready`: this is a warning, not an incident. Confirm
  the frozen identities and backup state, then deliberately run
  `scripts/evaluate_score_grid_v3_prospective.py` once. Do not add it to cron,
  inspect raw performance first, or replace `decision.json` afterward.
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
