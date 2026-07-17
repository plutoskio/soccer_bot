# Prediction Operations Alerting

Status: implemented and fail-closed

Primary configuration: `config/collector.json` → `operations`

This document defines the operational alert boundary for the production
champion publisher and prospective v3 score-grid shadow. The objective is to
make each failure observable in the correct control plane, preserve an audit
trail, prevent alert spam, and ensure that a dead scheduler is monitored by a
process other than itself.

## 1. Detection architecture

There are three complementary layers.

### 1.1 In-process watchdog

After collection has closed DuckDB and prediction publication has finished,
`scripts/run_collector.py` calls
`soccer_bot.operational_alerts.run_operational_watchdog`. The watchdog never
opens the warehouse. It evaluates only:

- the sanitized publication result from the current run;
- frozen champion and shadow identities in `config/collector.json`;
- prior append-only publication receipts when the current publication fails;
- the mounted `data` filesystem's total, used, and free bytes.

Critical alerts make the collector exit with code `3`. This occurs after
successful collection commits and does not roll back provider data. It makes
an operationally failed publication or shadow cycle visible as a failed
Railway cron invocation. Blocking data health remains distinct at exit `2`.

### 1.2 Independent public-heartbeat monitor

A process cannot report that it was never started. Scheduler death therefore
cannot be inferred from an in-process heartbeat alone.

`.github/workflows/prediction-operations-watchdog.yml` runs every 15 minutes
on GitHub's scheduler, independently of Railway. It executes
`scripts/check_public_prediction_health.py` against the public web service and
verifies the server-rendered snapshot's:

- `as_of` freshness;
- production model version;
- frozen logical model SHA-256;
- positive prediction-row count;
- positive fixture count.

The freshness limit is 1,200 seconds. With a five-minute production cron, the
snapshot can miss three refresh opportunities before the fourth missed
interval breaches the limit. This tolerates one delayed invocation but still
detects a stopped schedule promptly.

On failure the workflow prints a sanitized diagnosis, opens one GitHub issue
titled `[operations] Soccer Bot prediction watchdog` if none is open, and
leaves the workflow red. It does not create duplicate issues. After recovery,
it closes the incident with a link to the successful run. The check uses only
public data and the scoped `GITHUB_TOKEN`; no Railway, provider, or storage
credential is stored in GitHub.

### 1.3 Railway-native capacity alerts

Railway volume alerts remain the authoritative provider-side signal at 80%,
95%, and 100% of the 10 GB volume. The in-process check duplicates the 80%
warning and 95% critical boundary so every collector receipt records capacity
evidence. Filesystem-reported capacity is corroborative; Railway's quota
calculation is authoritative if the two disagree.

## 2. Alert conditions

### 2.1 Champion publication failure

`champion_publication_failed` is critical whenever the current publisher
result is not `uploaded`, including generation, validation, object upload,
read-back, or blocking-health skips. The previous public snapshot remains
untouched.

### 2.2 Champion staleness

`champion_publication_stale` is critical when no successful champion
publication is at most 1,200 seconds old. On a current failure, the watchdog
scans the receipt for the latest prior `uploaded` record. Missing history is
stale by definition. The independent GitHub monitor applies the same threshold
to public output; this detects a Railway cron that stopped running.

### 2.3 Champion identity mismatch

`champion_model_identity_mismatch` is critical when the successful result does
not contain the configured model version and logical SHA-256. Publisher errors
containing `mismatch` receive the same explicit classification.

### 2.4 Champion rows below minimum

`champion_prediction_rows_below_minimum` is critical when a result is below
`minimum_prediction_rows` or the publisher reports its fail-closed
`below_minimum_prediction_rows` error. The independent monitor separately
rejects zero public predictions and zero public fixtures.

### 2.5 Publication receipt failure

`publication_receipt_write_failed` is critical. An uploaded snapshot without
its append-only audit receipt is not operationally healthy even if the public
object is valid.

### 2.6 Shadow generation failure

`shadow_score_grid_failed` is critical whenever enabled v3 does not report
`written_to_persistent_shadow_store`. Shadow failure does not undo the public
champion upload, but it fails the cron invocation so loss of prospective
evidence is visible immediately.

### 2.7 Shadow identity mismatch

`shadow_model_identity_mismatch` is critical when the shadow version or
logical SHA-256 differs from frozen configuration. Generation errors containing
`mismatch` receive the same classification.

### 2.8 Shadow rows below minimum or inconsistent with parent

`shadow_prediction_rows_below_minimum` and
`shadow_parent_row_count_mismatch` are critical. The shadow must meet its
configured minimum and contain one row for every champion
fixture/information-state row. Zero rows cannot be silently successful.

### 2.9 Persistent volume pressure

`persistent_volume_warning` opens at 80% and does not fail a run.
`persistent_volume_critical` opens at 95% and exits `3`. Railway's additional
100% native alert remains enabled.

### 2.10 Watchdog failure

`operational_watchdog_failed` is critical. If the watchdog cannot evaluate or
durably write state, the collector prints only the exception type, never its
text, and exits `3`. A broken alarm is treated as an alarm.

## 3. Durable state and transitions

Production paths are:

```text
/app/data/reports/operations/current.json
/app/data/reports/operations/events.jsonl
```

`current.json` is written through a temporary file, flushed, `fsync`ed, and
atomically renamed. It contains generated time, external stale deadline,
overall state, check measurements, active alerts, and run-failure state.

`events.jsonl` is append-only and records transitions rather than every cron:

- `opened` when an alert code appears;
- `updated` if its severity changes;
- `resolved` when the code disappears.

An unchanged alert does not append another event. The current collector
summary still shows it on every affected run. No environment value, secret,
provider body, command stderr, or credential path enters either artifact.

## 4. Exit-code contract

```text
0  collection completed and no critical operational alert is active
2  collector data-health severity is blocking
3  prediction watchdog is critical or could not run safely
```

## 5. Incident response

### Public snapshot stale or scheduler stopped

1. Open the GitHub incident and linked workflow.
2. Run `railway service status --json`; confirm the exact collector commit,
   cron `*/5 * * * *`, and restart policy `NEVER`.
3. Inspect recent logs without opening DuckDB.
4. If a writer may exist, do not start a manual collector.
5. Restore schedule or deployment only after confirming the lock and volume.
6. Confirm a new automatic publication and let the monitor close the issue.

### Champion or shadow failure

1. Inspect sanitized `prediction_publication` and `operational_watchdog` log
   sections.
2. Inspect persistent receipts only with the stopped-scheduler/read-only
   procedure from `RAILWAY_OPERATIONS.md`.
3. Never manually replace public output or mutate a timestamped shadow file.
4. Correct code, identity, configuration, or storage access and deploy normally.

### Volume pressure

1. Compare the in-process measurement with Railway's volume dashboard.
2. Preserve a current native backup before deleting anything.
3. Never casually delete active warehouse, raw evidence, staged manifests,
   prediction receipts, or prospective grids.
4. Follow `RAILWAY_OPERATIONS.md` for cleanup, resize, and recovery.

## 6. Verification

```bash
.venv/bin/python -m unittest tests.test_operational_alerts -v
.venv/bin/python -m unittest tests.test_public_prediction_health -v
.venv/bin/python scripts/check_public_prediction_health.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/run_collector.py --dry-run
git diff --check
```

The public check is safe to repeat. It performs one bounded HTTP GET and makes
no external changes.
