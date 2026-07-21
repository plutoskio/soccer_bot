# Prediction Operations Alerting

Status: implemented and fail-closed

Primary configuration: `config/collector.json` → `operations`

This document defines the operational alert boundary for the production
champion publisher, outcome-blind Polymarket evidence, prospective v3
score-grid shadow, and prospective settlement ledger. The objective is to
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
- frozen champion, Polymarket-policy, and shadow identities in
  `config/collector.json`;
- prior append-only publication receipts when the current publication fails;
- the mounted `data` filesystem's total, used, and free bytes.

Critical alerts make the collector exit with code `3`. This occurs after
successful collection commits and does not roll back provider data. It makes
an operationally failed publication or shadow cycle visible as a failed
Railway cron invocation. Blocking data health remains distinct at exit `2`.

### 1.2 Independent public-snapshot monitor

A process cannot report that it was never started. Scheduler death therefore
cannot be inferred from an in-process heartbeat alone.

`.github/workflows/prediction-operations-watchdog.yml` runs every 15 minutes
on GitHub's scheduler, independently of Railway. It executes
`scripts/check_public_prediction_health.py` against the public web service and
verifies both the compact champion heartbeat and the specialized V2 platform
snapshot. The checks cover:

- `as_of` freshness;
- production model version;
- frozen logical model SHA-256;
- positive prediction-row count;
- positive fixture count.
- the frozen family-registry version and validated-only ranking policy;
- every exposed family model's registry membership and logical identity;
- positive platform state count and non-empty information-state coverage.

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
95%, and 100% of the 20 GB volume. The in-process check duplicates the 80%
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

### 2.9 Polymarket evidence failure and capture gaps

`polymarket_market_evidence_failed` is critical when the enabled read-only
pairing process does not report `updated` or `no_new_evidence`. It does not undo
the already-published champion, but it prevents silent loss of market evidence.

`polymarket_evidence_policy_identity_mismatch` is critical when the receipt's
policy SHA-256 differs from frozen collector configuration.
`polymarket_evidence_receipt_invalid` is critical for missing, negative, or
internally impossible counts. `polymarket_evidence_safety_violation` is
critical if the receipt claims that outcome/performance data was written or a
trading action was performed.

`polymarket_pre_cutoff_capture_gap` is a warning, not a run failure. It opens
only when at least one champion row has a complete semantic moneyline mapping
but lacks complete timing-safe books. Zero listings or an unmapped market do
not produce an operational incident.

### 2.10 Confirmed-lineup player shadow safety

When enabled, the player shadow has two healthy statuses: `written` and
`no_eligible_confirmed_lineups`. A genuine zero-lineup cycle is expected and is
not an alert.

`confirmed_lineup_player_shadow_failed` is critical for every other status.
`confirmed_lineup_player_model_identity_mismatch` is critical when version or
logical SHA-256 differs from frozen configuration.
`confirmed_lineup_player_receipt_invalid` is critical for missing, negative, or
internally inconsistent record counts.
`confirmed_lineup_player_unsafe_activation` is critical unless the publisher
explicitly reports `champion_replacement_authorized: false`. The player shadow
may fail without undoing a valid champion upload, but the cron exits `3` so a
loss of prospective lineup evidence or an unsafe activation cannot be silent.

### 2.11 Prospective settlement failure

`prospective_settlement_ledger_failed` is critical when the enabled outcome
join does not report `updated` or `no_new_settlements`. This includes frozen
artifact drift, corrupt evidence, a broken hash chain, ambiguous results,
read-only warehouse errors, or subprocess failure. It does not undo the
already-published champion, but the cron exits `3` so settlement gaps cannot be
silent.

`premature_prospective_evaluation_output` is critical if the settlement receipt
does not explicitly report both `performance_aggregates_written: false` and
`gate_decision_written: false`. Per-fixture scoring is allowed; aggregate
peeking before the frozen evidence minimum is not.

`prospective_settlement_receipt_invalid` is critical when supposedly successful
output has negative/missing counts, adds more rows than exist, omits a required
chain head, or reports a chain head for an empty ledger.

### 2.12 Prospective evaluation readiness

The routine evaluator path is count-only and has three valid states:
`locked_insufficient_evidence`, `ready_for_explicit_one_shot_evaluation`, and
`decision_already_exists`.

`prospective_evaluation_readiness_failed` is critical for subprocess failure,
missing output, or any unrecognized state.
`prospective_evaluation_readiness_unsafe` is critical if the receipt exposes
performance or permits automatic decision execution.
`prospective_evaluation_config_identity_mismatch` is critical when the receipt
does not match the collector-pinned frozen evaluation-config SHA-256.
`prospective_evaluation_ledger_count_mismatch` is critical when readiness and
the verified settlement receipt disagree on ledger length.

`prospective_evaluation_ready` is a warning. It means the first deterministic
cutoff has reached every frozen evidence minimum and the human-only one-shot
command may be run. It does not fail the cron and never runs the decision
automatically.

### 2.12 Persistent volume pressure

`persistent_volume_warning` opens at 80% and does not fail a run.
`persistent_volume_critical` opens at 95% and exits `3`. Railway's additional
100% native alert remains enabled.

### 2.13 Watchdog failure

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
2. Run `railway service status --service soccer_bot --json`; confirm the exact collector commit,
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
3. Never manually replace public output or mutate a per-pair evidence file or
   settled ledger row.
4. Correct code, identity, configuration, or storage access and deploy normally.

### Polymarket evidence failure or capture gap

1. Preserve raw artifacts, normalized books, and all existing immutable
   evidence; never hand-edit or delete an evidence record.
2. Compare configured and observed policy hashes in the sanitized receipt.
3. For a capture gap, inspect the fixture's schedule version, mapping decision,
   stage checkpoint, attempt timing, token count, and raw CLOB batch.
4. Confirm the request was inside \([C_h-16\text{m},C_h)\); do not relabel a
   late book to make coverage pass.
5. Correct the collector or mapper through a new deployment. A semantic-policy
   change requires a new version and hash, not an in-place historical rewrite.

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
