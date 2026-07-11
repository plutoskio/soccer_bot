# Soccer Bot — Detailed Project TODO

This roadmap orders the remaining work from repository cleanup through model
development. Data correctness, provenance, reproducibility, and leakage
prevention take priority over implementation speed.

## Working rules

- [ ] Make one focused change set per phase or independently reviewable task.
- [ ] Preserve `data/raw/` as immutable source evidence.
- [ ] Never delete an applied migration, even if it performed a one-time change.
- [ ] Do not rerun repair scripts until their guards and the current warehouse
      state have been reviewed.
- [ ] Before changing `soccer.duckdb`, create a verified backup and test risky
      operations against a copy.
- [ ] Run the full test suite before and after structural changes:

  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```

- [ ] Update documentation and generated reports when their underlying behavior
      or counts change.

---

## Phase 0 — Clean and standardize the repository

### 0.1 Create a complete inventory

- [x] Inventory every tracked and ignored top-level directory by purpose, size,
      owner, regeneration method, and retention requirement.
- [x] Classify every script as one of:
  - operational command;
  - reproducible build/backfill command;
  - diagnostic/audit tool;
  - guarded repair tool;
  - completed one-time migration helper;
  - obsolete or superseded code.
- [x] Classify every report as one of:
  - current generated report;
  - permanent audit evidence;
  - historical progress report;
  - disposable execution log;
  - stale or superseded output.
- [x] Record which files are generated and which are authoritative inputs.
- [x] Check README and documentation links before moving or removing anything.

### 0.2 Review scripts

The following is an initial classification hypothesis. Confirm it from code,
database state, documentation references, and Git history before acting.

#### Likely permanent operational commands

- [ ] Keep and document `scripts/run_collector.py`.
- [ ] Keep and document `scripts/build_database.py`.
- [ ] Decide whether `scripts/probe_sources.py` remains a supported diagnostic
      command or belongs in a development-tools directory.

#### Historical acquisition and reproducibility tools

- [ ] Review `scripts/audit_historical_coverage.py`.
- [ ] Review `scripts/build_backfill_manifest.py`.
- [ ] Review `scripts/run_historical_backfill.py`.
- [ ] Review `scripts/backfill_history.py`.
- [ ] Retain enough tooling to reproduce or extend the historical warehouse.
- [ ] Move completed-but-still-useful historical commands to a clearly named
      location such as `scripts/historical/` if they are no longer routine.

#### Repair and cleanup scripts requiring a decision

- [x] Review `scripts/remove_out_of_scope_discovery_fixtures.py`; retain until
      normal rebuilds enforce the configured scope.
- [x] Review `scripts/repair_known_swapped_player_blocks.py`; retain until the
      fixture-specific provider corrections are part of reproducible rebuilds.
- [x] Review and remove the superseded `repair_api_player_identities.py` command.
- [x] Review and remove the superseded `repair_api_player_links.py` command.
- [x] Review and remove the superseded `repair_api_player_transliterations.py`
      command.
- [x] Review `scripts/reprocess_api_football.py`; retain as a maintenance replay
      utility.
- [ ] For each script, document:
  - the incident it repaired;
  - whether the repair has already completed;
  - exact database and raw-artifact guards;
  - whether a full rebuild still needs the script;
  - whether the behavior now belongs in a loader, migration, or test;
  - whether the script should be kept, archived, or deleted.
- [ ] Prefer retaining small guarded repair scripts as historical evidence when
      they explain current normalized data.
- [ ] Delete a repair script only when its behavior is unnecessary for rebuilds,
      its evidence is documented elsewhere, and regression tests preserve the
      invariant it established.

### 0.3 Reorganize scripts without losing history

- [ ] Define a stable script layout, for example:

  ```text
  scripts/
  ├── operational/
  ├── historical/
  ├── diagnostics/
  └── repairs/
  ```

- [ ] Decide whether the extra hierarchy improves usability enough to justify
      changing existing command paths.
- [ ] Use Git-aware moves so file history remains understandable.
- [ ] Update imports, README commands, AGENTS instructions, and report references.
- [ ] Add clear module or command descriptions to retained scripts.
- [ ] Ensure dangerous repair commands default to dry-run and require an explicit
      apply flag.
- [ ] Add a prominent completed/guarded warning to one-time repair commands.

### 0.4 Clean reports and logs

- [ ] Keep the current database coverage report, but regenerate it from the live
      warehouse because the tracked version predates the completed backfill.
- [x] Decide which historical backfill milestone reports still provide useful
      permanent evidence.
- [x] Consolidate the 50-, 100-, 250-batch, pilot, and remaining-backfill reports
      into one final historical-backfill record if they are redundant.
- [x] Review `.log` files under `reports/`; logs should normally be ignored or
      moved outside tracked documentation.
- [ ] Keep repair reports when they contain evidence required to explain current
      warehouse corrections.
- [ ] Add a `reports/README.md` describing authoritative and generated reports.
- [ ] Add generation timestamps, source database identity, and relevant hashes
      to generated reports.

### 0.5 Clean generated data and backups

- [x] Inventory every DuckDB backup under `data/warehouse/` with size, creation
      reason, hash, and whether it is still required.
- [ ] Define a backup retention policy: protected milestone backups plus a small
      rolling set of recent backups.
- [x] Verify protected backups before deleting redundant copies.
- [ ] Keep the live warehouse filename stable.
- [ ] Confirm staged manifests are reproducible before deleting stale staged
      outputs.
- [ ] Confirm `.gitignore` covers databases, raw artifacts, temporary outputs,
      logs, secrets, model artifacts, and local environments appropriately.
- [ ] Never expose or inspect `.env` contents in cleanup reports or commits.

### 0.6 Remove dead code safely

- [ ] Search for imports, subprocess calls, documentation links, and generated
      references to every proposed deletion.
- [ ] Run the full test suite before deletion.
- [ ] Remove one logical group at a time.
- [ ] Run the full test suite after each group.
- [ ] Run collector dry-run and database read-only integrity checks.
- [ ] Confirm the historical manifest and warehouse remain readable.
- [ ] Record removed files and the reason for removal in the commit message.

### 0.7 Cleanup exit criteria

- [ ] Every retained script has a current purpose and documented invocation.
- [ ] One-time scripts are clearly separated and guarded.
- [ ] No documentation points to removed paths.
- [ ] Reports distinguish current state from historical evidence.
- [ ] Backup retention is documented and redundant backups are removed safely.
- [ ] The working tree contains no unexplained generated artifacts.
- [ ] All tests pass.

---

## Phase 1 — Define modeling and data contracts

### 1.1 Forecast products

- [ ] Define the supported prediction families:
  - regulation home/draw/away;
  - regulation goal difference and spreads;
  - regulation score distribution and exact score;
  - player goals;
  - player assists;
  - first team to score;
  - team and match corners.
- [ ] Define settlement semantics for each target.
- [ ] Explicitly separate regulation, extra time, and penalties.
- [ ] Define how voided, postponed, abandoned, and administrative matches behave.

### 1.2 Forecast times

- [ ] Define an early forecast cutoff, initially proposed as `T-24h`.
- [ ] Define a confirmed-lineup forecast cutoff, initially proposed as `T-20m`.
- [ ] Decide whether an additional `T-60m` forecast is useful.
- [ ] Specify which inputs are legal at every cutoff.
- [ ] Specify behavior when the required pregame snapshot was missed.

### 1.3 Dataset contracts

- [ ] Define separate result-, team-, player-, corner-, and market-dataset grains.
- [ ] Require the appropriate `fixture_model_eligibility` flag for each dataset.
- [ ] Require feature-specific non-null checks in addition to eligibility.
- [ ] Define chronological train, validation, calibration, and test windows.
- [ ] Define competition inclusion and minimum-coverage policies.
- [ ] Define dataset manifest contents, including SQL hash, code revision, cutoff
      policy, warehouse identity, source bounds, exclusions, and output hash.
- [ ] Review the contract before database or collector schema changes.

### 1.4 Contract exit criteria

- [ ] Every initial prediction has an unambiguous target and cutoff.
- [ ] Required collector snapshots follow directly from the contracts.
- [ ] Required database observations and metadata follow directly from the
      contracts.
- [ ] No historical feature can legally use information retrieved after cutoff.

---

## Phase 2 — Harden the database

### 2.1 Canonical identity and schema audit

- [ ] Audit duplicate or parallel competition identities across providers.
- [ ] Audit season naming and source-to-canonical mappings.
- [ ] Audit remaining fixture duplicates using canonical teams and kickoff time.
- [ ] Audit team aliases and cross-source team mappings.
- [ ] Keep player linking conservative; never merge globally by display name.
- [ ] Add explicit integrity checks for logical foreign-key relationships.
- [ ] Add uniqueness checks for source maps and observation natural keys.

### 2.2 Rebuild safety

- [ ] Make full raw replay enforce the configured competition boundary.
- [ ] Build the warehouse into a temporary path before replacing the live file.
- [ ] Compare table counts, key fingerprints, eligibility totals, and unrelated
      tables before replacement.
- [ ] Preserve reviewed mappings and evidence-backed corrections across rebuilds.
- [ ] Document whether each historical correction is implemented by loader logic,
      migration, guarded repair, or reviewed mapping.
- [ ] Add a reproducible rebuild command and runbook.

### 2.3 Collector-supporting schema

- [x] Add fixture schedule observations so reschedules do not overwrite history.
- [x] Add component-level collection state for result, lineup, team statistics,
      player statistics, events, identities, and correction refreshes.
- [x] Record whether a lineup was captured before the kickoff known at retrieval.
- [x] Record missed pregame snapshots explicitly.
- [x] Extend checkpoints with next attempt, maximum attempts, priority, terminal
      reason, and last run identity.

### 2.4 Reporting and validation

- [ ] Regenerate `reports/DATABASE_COVERAGE_REPORT.md` from the current warehouse.
- [ ] Report eligibility counts separately for the approved historical manifest
      and the entire multi-source warehouse.
- [ ] Add source-, competition-, season-, and field-level coverage summaries.
- [x] Distinguish blocking failures from controlled warnings.
- [x] Add regression tests for every new integrity rule.

### 2.5 Database exit criteria

- [ ] A full rebuild is bounded, reproducible, and verified before promotion.
- [ ] Canonical identity inconsistencies relevant to initial models are resolved.
- [x] Collector-required temporal metadata is represented without overwriting
      history.
- [ ] Generated coverage reports match the live warehouse.
- [ ] All tests pass with zero open blocking quality issues.

---

## Phase 3 — Make the collector reliable and unattended

Use `DAILY_COLLECTION_REWORK.md` as the detailed behavioral specification.

### 3.1 Discovery and downtime recovery

- [x] Discover fixtures over a configurable future planning window.
- [x] Recheck a configurable past recovery window on every run.
- [x] Support explicit catch-up beyond the normal window, including at least a
      three-week outage.
- [x] Recover final results, lineups, events, and statistics where providers still
      expose them.
- [x] Mark unrecoverable pregame lineup and market snapshots as missed.
- [x] Refresh schedules for rescheduled, postponed, and cancelled fixtures.

### 3.2 Pregame collection

- [x] Implement lineup attempts at approximately T-50, T-35, T-20, and T-5.
- [x] Stop lineup polling once two valid starting elevens are stored.
- [x] Resolve pregame player identities using conservative team context.
- [x] Reconcile unresolved pregame aliases after post-match statistics arrive.
- [x] Capture Polymarket discovery and books at contract-defined timestamps.

### 3.3 Post-match collection

- [x] Treat T+150 as a status check, not an assumption of completion.
- [x] Poll live or delayed matches with bounded retries.
- [x] Handle final, postponed, cancelled, abandoned, suspended, and
      administrative states explicitly.
- [x] Retry incomplete components independently.
- [x] Add correction refreshes around T+24h and T+72h.
- [x] Preserve legitimately unavailable provider sections as explicit states.

### 3.4 Operational resilience

- [x] Add bounded network retries and exponential backoff.
- [x] Honor `Retry-After` for HTTP 429 responses.
- [x] Continue unrelated work when one fixture batch fails.
- [x] Add a single-process collector lock with stale-lock recovery.
- [x] Keep quota reserve enforcement and 20-fixture batching.
- [x] Add structured run summaries without secrets.
- [x] Add a daily health report.
- [x] Supply and document a macOS `launchd` schedule.

### 3.5 Collector tests and exit criteria

- [x] Test recovery after a three-day and three-week outage.
- [x] Test that recovered lineups are not mislabeled as pregame observations.
- [x] Test reschedules, postponements, partial data, rate limits, and concurrency.
- [x] Test that repeated runs make no unnecessary requests.
- [x] Test that ambiguous player identities remain unresolved.
- [ ] Run the collector continuously in observation mode for a defined trial.
- [ ] Review health reports and resolve systemic gaps before model automation.

---

## Phase 4 — Build frozen, leakage-safe datasets

### 4.1 Dataset infrastructure

- [ ] Add versioned dataset definitions.
- [ ] Add dataset-build manifests and immutable output hashes.
- [ ] Write frozen outputs to Parquet.
- [ ] Record the maximum source retrieval time used by each build.
- [ ] Record inclusion and exclusion counts by reason.
- [ ] Make builds deterministic from a declared warehouse snapshot.

### 4.2 First result dataset

- [ ] Begin from `eligible_result_models`.
- [ ] Generate regulation home goals, away goals, result class, and goal difference.
- [ ] Build rolling team features using only earlier fixtures.
- [ ] Prevent same-match and future-match leakage in every rolling window.
- [ ] Add home advantage, rest, competition, and season context carefully.
- [ ] Define missing-history behavior for promoted or newly observed teams.
- [ ] Add automated leakage and target-consistency tests.

### 4.3 Team, player, corner, and market datasets

- [ ] Build team-stat features from `eligible_team_models` with feature-specific
      non-null filters.
- [ ] Build player opportunities from `eligible_player_models`.
- [ ] Model starting probability and expected minutes separately from scoring.
- [ ] Build player goal and assist histories using only prior fixtures.
- [ ] Build corner targets and histories separately from result targets.
- [ ] Build market snapshots only where observation time and executable prices
      are known.
- [ ] Keep provider-specific xG/xA definitions distinct.

### 4.4 Dataset exit criteria

- [ ] Repeated builds from the same snapshot have identical hashes.
- [ ] Every row has a documented feature cutoff.
- [ ] Leakage tests cover rolling features, lineups, season aggregates, and odds.
- [ ] Dataset manifests fully explain sources, exclusions, and quality policy.
- [ ] Sample rows are manually reconciled with source evidence.

---

## Phase 5 — Develop and evaluate models

### 5.1 Baselines first

- [ ] Implement naive home/draw/away frequency baselines.
- [ ] Implement bookmaker-implied probability baselines with margin removal.
- [ ] Implement a simple Poisson or Dixon-Coles score model.
- [ ] Evaluate chronologically, never with random fixture splits.
- [ ] Measure log loss, Brier score, calibration, and relevant scoring metrics.

### 5.2 Team and score-distribution models

- [ ] Compare interpretable statistical models with tree-based alternatives.
- [ ] Produce one coherent regulation score distribution.
- [ ] Derive moneyline, spreads, exact score, and related markets from it.
- [ ] Calibrate probabilities on a separate chronological window.
- [ ] Evaluate by competition, season, favorite strength, and data availability.

### 5.3 Player models

- [ ] Model squad inclusion and starting probability.
- [ ] Model expected minutes conditional on lineup status.
- [ ] Model team attacking-event totals.
- [ ] Allocate goal and assist probabilities across players using role, history,
      teammates, opponent, and expected minutes.
- [ ] Recompute after confirmed lineups.
- [ ] Evaluate calibration separately for starters, substitutes, and positions.

### 5.4 Corners and first-to-score

- [ ] Build a separate corner count model.
- [ ] Derive first-team-to-score probabilities from a defensible event-time or
      simulation process.
- [ ] Validate consistency between score distribution and derived markets.

### 5.5 Polymarket evaluation

- [ ] Parse and version exact market rules.
- [ ] Compare model probabilities with executable bid/ask prices, not displayed
      midpoint probabilities alone.
- [ ] Account for spread, fees, size, and liquidity.
- [ ] Prevent look-ahead by matching predictions only to order books observed at
      or before the prediction timestamp.
- [ ] Backtest decision rules with conservative execution assumptions.
- [ ] Keep automated trading outside scope until research evaluation is stable.

### 5.6 Model automation

- [ ] Add model and prediction registries.
- [ ] Version model artifacts, code, data builds, and hyperparameters.
- [ ] Automate dataset refresh only after deterministic manual builds are stable.
- [ ] Automate retraining only after drift and failure policies are defined.
- [ ] Add prediction monitoring, calibration monitoring, and input-quality gates.
- [ ] Fail closed when required snapshots or features are unavailable.

### 5.7 ML exit criteria

- [ ] Baselines and candidate models are reproducible from frozen datasets.
- [ ] Evaluation is chronological and leakage-tested.
- [ ] Probabilities are calibrated and segmented diagnostics are reviewed.
- [ ] Predictions retain full lineage to dataset, model, cutoff, lineup, and
      market snapshot.
- [ ] Market research uses executable-price assumptions and reports uncertainty.

---

## Immediate next actions

- [ ] Complete the Phase 0 inventory.
- [ ] Produce a proposed keep/archive/delete table for every script and report.
- [ ] Review that proposal before moving or deleting files.
- [ ] Perform cleanup in small commits with tests after each group.
- [ ] Regenerate the database coverage report after cleanup.
- [ ] Begin the modeling/data-contract document only after repository paths and
      authoritative artifacts are stable.
