# Soccer Bot — Cleanup Audit and Approval Proposal

Date: 2026-07-06

Approval status: **A, B, and C executed with user approval. D not approved and
not performed.**

No scripts were moved or deleted.

## Execution record

- Deleted the two approved tracked execution logs.
- Added `reports/API_FOOTBALL_BACKFILL_FINAL.md` and deleted the four superseded
  incremental milestone reports.
- Deleted exactly the 12 approved Group A database backups.
- Recovered **5,743,984,640 bytes (5.350 GiB)** from Group A backups.
- Retained the live warehouse and all five recent correction-era backups.
- Reduced the local `data/` directory from approximately 13 GiB to 7.5 GiB.

## Summary

- The repository's source code is not materially bloated. `src/`, `scripts/`,
  and `tests/` together occupy about 1.1 MB.
- The main disk-space issue is `data/warehouse/`: 17 ignored backups occupy
  approximately 11.53 GiB in addition to the 1.24 GiB live warehouse.
- The clearest tracked-file cleanup candidates are two execution logs and four
  incremental backfill milestone reports.
- Repair scripts must not all be treated alike. Some are superseded by current
  loader logic; two still encode behavior needed to reproduce the current
  warehouse safely.
- The current tracked database coverage report is stale and should be
  regenerated, not deleted.
- Existing uncommitted files are active work and are excluded from proposed
  deletion.

## Decision labels

- **KEEP**: current operational, reproducibility, configuration, or evidence value.
- **KEEP / REORGANIZE**: retain, but move or document more clearly later.
- **ARCHIVE**: retain as historical evidence outside the routine command surface.
- **REGENERATE**: replace stale generated output from the authoritative source.
- **DELETE CANDIDATE**: appears safe to remove after explicit approval.
- **BLOCKED**: do not remove until a stated dependency is fixed.

## Scripts

| Script | Classification | Proposal | Reason |
|---|---|---|---|
| `scripts/run_collector.py` | Operational | **KEEP** | Primary incremental collection entry point. |
| `scripts/build_database.py` | Operational/rebuild | **KEEP** | Canonical raw-to-warehouse build and quality report entry point. |
| `scripts/probe_sources.py` | Diagnostic | **KEEP / REORGANIZE** | Useful for bounded provider validation and schema-change investigation. |
| `scripts/backfill_history.py` | Historical acquisition | **KEEP / REORGANIZE** | Reproduces Football-Data.co.uk and Understat raw acquisition and supports future extensions. |
| `scripts/audit_historical_coverage.py` | Historical audit | **KEEP / REORGANIZE** | Reproducibly determines which API-Football seasons are safe to backfill. |
| `scripts/build_backfill_manifest.py` | Historical build | **KEEP / REORGANIZE** | Produces the fixture-level manifest and batching plan from retained evidence. |
| `scripts/run_historical_backfill.py` | Historical execution | **KEEP / REORGANIZE** | Checkpointed, validated executor remains useful for new seasons and disaster recovery. |
| `scripts/reprocess_api_football.py` | Maintenance utility | **KEEP / REORGANIZE** | Replays retained API-Football artifacts after parser/linker changes. It should gain clearer safety documentation. |
| `scripts/repair_api_player_identities.py` | Completed repair | **DELETED WITH APPROVAL** | The durable identity rule now exists in `api_player_identity_key` and regression tests; the original implementation remains in Git history. |
| `scripts/repair_api_player_links.py` | Completed repair | **DELETED WITH APPROVAL** | Current loaders contain the conservative linking behavior, temporary repair tables are absent, and the implementation remains in Git history. |
| `scripts/repair_api_player_transliterations.py` | Completed repair | **DELETED WITH APPROVAL** | Current name comparison and player-linking code contains the durable behavior; reports and Git history retain the incident record. |
| `scripts/remove_out_of_scope_discovery_fixtures.py` | One-time cleanup with unresolved rebuild dependency | **BLOCKED / KEEP** | Full replay of unfiltered daily-discovery raw responses can reintroduce the removed fixtures. First enforce competition scope during normal rebuilds. |
| `scripts/repair_known_swapped_player_blocks.py` | Evidence-backed provider correction | **BLOCKED / KEEP** | Seven immutable provider payloads contain proven opposing-team assignments. The current warehouse correction depends on this fixture-specific overlay. |

### Script conclusion

The three superseded identity/linking repair commands were deleted with user
approval after confirming that their durable rules exist in current loaders and
tests. Their incident reports and Git history remain. The two blocked scripts
remain easy to find until a clean rebuild reproduces their effects automatically
and tests prove it.

## Reports

| Report | Proposal | Reason |
|---|---|---|
| `reports/DATABASE_COVERAGE_REPORT.md` | **REGENERATE** | It predates the completed historical backfill and no longer describes the live database. |
| `reports/API_FOOTBALL_HISTORICAL_COVERAGE.json` | **KEEP** | Machine-readable input to `build_backfill_manifest.py`. |
| `reports/API_FOOTBALL_HISTORICAL_COVERAGE.md` | **KEEP** | Human-readable evidence for approved and rejected league-seasons. |
| `reports/API_FOOTBALL_BACKFILL_MANIFEST.md` | **KEEP** | Human summary of the immutable staged manifest. |
| `reports/SOURCE_VALIDATION_REPORT.md` | **ARCHIVE** | Useful initial source-validation evidence, but no longer a current-state report. |
| `reports/API_FOOTBALL_TRANSLITERATION_REPAIR.md` | **ARCHIVE** | Explains a completed identity correction retained in loader behavior. |
| `reports/API_FOOTBALL_COMPOUND_NAME_REPAIR.md` | **ARCHIVE** | Explains a completed identity correction retained in loader behavior. |
| `reports/API_PLAYER_LINK_REVIEW.md` | **REGENERATE / KEEP** | A generated manual-review queue. Preserve until regenerated from the current database and compared. |
| `reports/API_FOOTBALL_BACKFILL_PILOT.md` | **DELETE CANDIDATE** | Superseded by completed 1,181-batch state and Git history. |
| `reports/API_FOOTBALL_BACKFILL_50_BATCH.md` | **DELETE CANDIDATE** | Intermediate progress report, superseded by final completion. |
| `reports/API_FOOTBALL_BACKFILL_100_BATCH.md` | **DELETE CANDIDATE** | Intermediate progress report, superseded by final completion. |
| `reports/API_FOOTBALL_BACKFILL_250_BATCH.md` | **DELETE CANDIDATE** | Intermediate progress report, superseded by final completion. |
| `reports/API_FOOTBALL_BACKFILL_REMAINING.log` | **DELETE CANDIDATE** | Raw execution log; success is durably recorded in DuckDB checkpoints and reports. |
| `reports/API_PLAYER_LINK_REPAIR_RUN.log` | **DELETE CANDIDATE** | Raw execution log for a completed repair; durable state and review output exist elsewhere. |

### Required replacement before deleting milestone reports

- Create one concise final historical-backfill report containing:
  - manifest hash;
  - approved fixture count;
  - batch and fixture completion totals;
  - final validation counts;
  - controlled warnings and exclusions;
  - completion date;
  - relevant raw/staged paths.
- Verify that this report and DuckDB checkpoints preserve every fact still needed
  from the pilot/50/100/250 milestone reports.

## Warehouse backups

The ignored `data/warehouse/` directory contains:

- live database: approximately 1.24 GiB;
- 17 backups: approximately 11.53 GiB;
- no byte-identical backup files; every SHA-256 hash differs.

Different hashes do not imply that all backups remain useful. Most represent
temporary points in a completed backfill or repair sequence.

### Group A — superseded incremental backfill snapshots

Proposed **DELETE CANDIDATES after approval**:

- `soccer.pre_alias.duckdb`
- `soccer.pre_pro.duckdb`
- `soccer.pre_10_batch_pilot.duckdb`
- `soccer.pre_player_identity_fix.duckdb`
- `soccer.pre_50_batch_run.duckdb`
- `soccer.pre_100_batch_run.duckdb`
- `soccer.post_250_failure.duckdb`
- `soccer.pre_transliteration_repair.duckdb`
- `soccer.pre_compound_name_repair.duckdb`
- `soccer.pre_evidence_link_repair.duckdb`
- `soccer.pre_failed_batch_retry.duckdb`
- `soccer.pre_remaining_backfill.duckdb`

Rationale: the backfill is complete, all 1,181 checkpoints succeeded, retained
raw artifacts are the authoritative evidence, and these snapshots are not used
by application code. They are useful only for historical forensic comparison.

### Group B — recent correction and cleanup snapshots

Proposed **KEEP TEMPORARILY**:

- `soccer.pre_scope_cleanup_20260706_165231.duckdb`
- `soccer.pre_player_block_repair_20260706_181237.duckdb`
- `soccer.pre_west_ham_shots_fix_20260706_182223.duckdb`
- `soccer.pre_orphan_dimension_cleanup_20260706_183131.duckdb`
- `soccer.pre_eligibility_view_20260706_184210.duckdb`

These are the only immediate rollback/forensic snapshots around the most recent
data corrections. They should not all be kept indefinitely, but deletion should
wait until:

- a fresh verified post-cleanup milestone backup exists;
- scope filtering is part of the normal rebuild;
- the swapped-player correction is reproducible during rebuild;
- the West Ham correction is represented by a reproducible correction rule or
  overlay rather than only live warehouse state;
- current eligibility and quality totals are captured in a refreshed report.

After those conditions are satisfied, retain one documented milestone backup
and remove or externally archive the rest.

## Raw and staged data

| Path | Proposal | Reason |
|---|---|---|
| `data/raw/` | **KEEP** | Immutable source evidence; required for rebuild and audit. Only about 103 MB. |
| `data/staged/api_football_backfill_manifest.jsonl` | **KEEP** | Immutable approved fixture manifest. |
| `data/staged/api_football_backfill_batches.json` | **KEEP** | Exact checkpoint/batch definition used for historical execution. |
| `data/staged/api_football_backfill_summary.json` | **KEEP** | Compact machine-readable manifest summary. |

No raw or staged file is currently proposed for deletion.

## Documentation and migrations

- **KEEP** all migrations, including one-time migrations. Applied migration
  history must remain stable.
- **KEEP** `AGENTS.md`, `DAILY_COLLECTION_REWORK.md`, and `TODO.md`; they are
  active uncommitted project work.
- **KEEP** `DATA_ARCHITECTURE.md`, but reconcile its provisional language and
  stale counts with the implemented system later.
- **KEEP** `DATA_SOURCE_AUDIT.md` as source-selection rationale, while marking it
  historical where provider assumptions have since been validated.
- **KEEP** `README.md`, then update commands if scripts are reorganized.

## Ignore-policy findings

- Secrets, environments, raw data, staged data, warehouse files, models, and
  generated artifact directories are already ignored appropriately.
- The two tracked `.log` files bypass the intended generated-report convention;
  future execution logs should not be committed.
- Consider standardizing generated reports under `reports/generated/`, which is
  already ignored, while keeping concise reviewed summaries under `reports/`.

## Proposed approval batches

### Approval A — completed

Delete the two tracked execution logs:

- `reports/API_FOOTBALL_BACKFILL_REMAINING.log`
- `reports/API_PLAYER_LINK_REPAIR_RUN.log`

Expected repository reduction: approximately 118 KB. The benefit is clarity,
not disk space.

### Approval B — completed

Create a final backfill completion report, then delete:

- `reports/API_FOOTBALL_BACKFILL_PILOT.md`
- `reports/API_FOOTBALL_BACKFILL_50_BATCH.md`
- `reports/API_FOOTBALL_BACKFILL_100_BATCH.md`
- `reports/API_FOOTBALL_BACKFILL_250_BATCH.md`

### Approval C — completed

Delete the 12 Group A warehouse snapshots after optionally copying them to
external archival storage. This should recover several GiB while preserving the
five recent correction-era backups temporarily.

### Approval D — superseded by approved minimal cleanup

The three superseded repair scripts were deleted instead of moved. Their durable
behavior remains in loaders and tests, while reports and Git preserve history.
The two rebuild-critical repair scripts were retained.

## Recommended order

1. Approve or reject Approval A.
2. Create the final backfill report and perform Approval B if approved.
3. Decide whether Group A backups need an external archive; perform Approval C
   only after that decision.
4. Reorganize scripts under Approval D.
5. Fix normal rebuild scope and correction overlays.
6. Reduce Group B to one verified milestone backup.
7. Regenerate current database and player-link reports.

## Explicitly excluded from deletion review

The following are active uncommitted work and must not be treated as clutter:

- `AGENTS.md`
- `DAILY_COLLECTION_REWORK.md`
- `TODO.md`
- `migrations/006_fixture_model_eligibility.sql`
- `scripts/remove_out_of_scope_discovery_fixtures.py`
- `scripts/repair_known_swapped_player_blocks.py`
- `tests/test_model_eligibility.py`
- current modifications to `DATA_ARCHITECTURE.md`
