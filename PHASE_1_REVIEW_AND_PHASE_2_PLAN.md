# Collector Phase 1 Review and Phase 2 Plan

Status: Phase 1 implemented and verified; Phase 2 not started
Prepared: 10 July 2026
Scope: schedule-observation history and the next component-state phase

## 1. Purpose of this document

This document is a review aid for another model or human reviewer. It records:

- exactly what Phase 1 changed;
- what was deliberately not changed;
- how the migration and code were verified;
- the remaining limitations of the current collector;
- what Phase 2 will change and how it will be tested.

The collector implementation plan remains the broader roadmap:
`COLLECTOR_IMPLEMENTATION_PLAN.md`. This document covers only the completed
Phase 1 slice and the proposed next phase.

## 2. Phase 1 in one sentence

Phase 1 teaches the warehouse to preserve every API-Football schedule/status
observation instead of relying only on the latest mutable values in `fixture`.

This is the historical foundation needed before the collector can safely handle
reschedules, lineup timing, market timing, and recovery after downtime.

## 3. Problem before Phase 1

The existing loader updates these fields on the canonical `fixture` row:

```text
fixture.scheduled_kickoff
fixture.status
```

Those fields are useful as a current cache, but they do not preserve history.
If a match moves from Saturday to Sunday, the Saturday schedule can disappear.
The database then cannot answer:

- what schedule was known when a lineup was collected;
- what schedule was known when a market price was collected;
- when the provider first reported a postponement;
- which collection jobs were planned against the old schedule.

The collector plan requires schedule-specific jobs, so this history must exist
before the later planner work begins.

## 4. Files changed in Phase 1

### `migrations/007_collector_rework.sql`

Added the `fixture_schedule_observation` table and two indexes.

### `src/soccer_bot/loaders.py`

Added:

- `API_FOOTBALL_STATUS_MAP`;
- `canonical_api_football_status()`;
- schedule-observation insertion for every loaded API-Football fixture;
- an open warning for unknown provider status codes.

The insertion is performed by `_load_api_fixture()`, which is used by fixture
discovery and detail/backfill payloads. The raw artifact is already registered
before normalization, so the observation can retain exact raw provenance.

### `scripts/build_database.py`

Added `fixture_schedule_observation` to the database-build count list so future
raw replays include its count in the generated coverage report.

### `tests/test_loaders.py`

Added tests for status mapping, idempotent loading, rescheduling history, and
unknown-status warnings.

## 5. New database table

The new table is:

```text
fixture_schedule_observation
```

Columns:

| Column | Meaning |
|---|---|
| `schedule_observation_id` | Deterministic internal observation ID |
| `fixture_id` | Canonical project fixture ID |
| `source_code` | Provider, currently `api_football` |
| `fixture_source_id` | API-Football fixture ID |
| `provider_status` | Exact provider short code, such as `NS`, `PST`, or `FT` |
| `canonical_status` | Project status used by later collector logic |
| `scheduled_kickoff` | Kickoff reported in this response |
| `observed_at` | Provider observation time when reliably available; currently nullable |
| `retrieved_at` | Local retrieval time from the raw-artifact metadata |
| `raw_artifact_id` | Exact immutable response supporting this row |
| `created_at` | Database insertion timestamp |

The deterministic ID uses the provider, provider fixture ID, and raw artifact
ID. The unique key `(source_code, fixture_source_id, raw_artifact_id)` prevents
duplicate observations for the same provider response.

Indexes support lookup by fixture/retrieval time and by canonical status/kickoff.

## 6. Canonical status mapping

The loader preserves the provider code and also derives a canonical status:

| API-Football code | Canonical status |
|---|---|
| `NS`, `TBD` | `scheduled` |
| `1H`, `2H`, `ET`, `P`, `LIVE`, `HT` | `live` |
| `INT` | `delayed` |
| `SUSP` | `suspended` |
| `FT`, `AET`, `PEN` | `final` |
| `PST` | `postponed` |
| `CANC` | `cancelled` |
| `ABD` | `abandoned` |
| `AWD`, `WO` | `administrative_result` |
| explicit repository administrative-unplayed shape | `administrative_result` |
| unknown or missing code | `unknown` |

Unknown codes are not guessed. The loader stores `unknown` and creates an open
warning with rule code `api_unknown_fixture_status`. Later checkpoint and retry
logic will decide how to schedule such work; Phase 1 does not implement that
planner behavior yet.

The existing legacy `fixture.status` behavior was preserved. In particular,
final fixtures still use `completed`, and the existing
`administrative_result_unplayed` value remains available for the model
eligibility view. The new table is the canonical historical status record for
future collector phases.

## 7. Example: a rescheduled fixture

First response:

```text
provider_status: NS
canonical_status: scheduled
scheduled_kickoff: 2026-07-10 18:00
retrieved_at: 2026-07-09 12:00
```

Later response:

```text
provider_status: PST
canonical_status: postponed
scheduled_kickoff: 2026-07-10 18:00
retrieved_at: 2026-07-10 10:00
```

Later rescheduled response:

```text
provider_status: NS
canonical_status: scheduled
scheduled_kickoff: 2026-07-12 18:00
retrieved_at: 2026-07-11 12:00
```

All three observations remain available. The current `fixture` row may reflect
the newest known values, but the historical table preserves the timeline.

## 8. What Phase 1 deliberately did not do

Phase 1 did not implement:

- rolling discovery over the previous 14 days and next 7 days;
- four-stage lineup polling at T-50, T-35, T-20, and T-5;
- component-level completion state;
- retryable checkpoint redesign;
- HTTP retry/backoff handling;
- collector locking;
- status-aware post-match polling;
- correction refreshes at +24h and +72h;
- repeated Polymarket discovery or staged market snapshots;
- daily health reports;
- `launchd` installation or scheduling.

The current collector therefore still has its old operational limitations. The
new table is foundation work; it is not yet consumed by a new planner.

No historical schedule observations were fabricated or bulk-created. The live
table is empty until new API-Football artifacts are loaded. This is intentional:
the project must not infer historical capture timing from the current fixture
row.

## 9. Verification performed

### Backup

Before the live migration, the warehouse was copied to an external backup path
and hashed:

```text
Backup SHA-256:
f7ab0803a503cc544154fbd031c19fe3e40450b9f757198a494cf7365fb6639a
```

The source and backup hashes matched before any live change. The live hash is
different afterward because the migration record and new empty table were
added; the backup remains the pre-migration rollback copy.

### Temporary database tests

The migration was applied to a copy of the live database. The checks confirmed:

- all 29 pre-existing fact/operational tables retained their row counts;
- `fixture_model_eligibility` totals were unchanged;
- the new schedule table was created empty;
- migration `007_collector_rework` was recorded exactly once.

The migration was also tested on empty temporary databases through the normal
test setup.

### Live migration checks

The live migration preserved these checked counts:

| Table | Count after migration |
|---|---:|
| `fixture` | 38,523 |
| `fixture_result_observation` | 63,314 |
| `lineup_snapshot` | 47,430 |
| `player_match_stat_observation` | 964,831 |
| `team_match_stat_observation` | 126,840 |
| `data_quality_issue` | 707 |
| `source_entity_map` | 1,073,071 |

The eligibility distribution was unchanged, and the new schedule table had
zero rows after migration.

### Tests

The full suite passed:

```text
Ran 56 tests
OK
```

This includes the original 53 tests plus three Phase 1 schedule-observation
tests. `git diff --check` also passed.

## 10. Phase 1 review checklist

A reviewer should verify:

- the new migration is additive and does not edit migrations 001–006;
- schedule rows retain raw-artifact provenance;
- repeated loading of the same artifact is idempotent;
- different retrieval artifacts append distinct schedule observations;
- provider status codes are preserved exactly;
- unknown codes remain `unknown` and create warnings;
- existing `fixture.status` and eligibility semantics are not unintentionally
  changed;
- no historical schedule timing was invented;
- the existing full test suite still passes.

## 11. Phase 2: component-level completion and checkpoint redesign

### Plain-language goal

Phase 2 will make the collector determine completion from the facts actually in
the database, rather than from whether a request was attempted before.

Today, an `incomplete` checkpoint is treated as finished. Also, the current
post-match check only asks for any result, two team-stat rows, and any player
stat row. That can incorrectly stop collection even when data is partial or
invalid.

Phase 2 replaces that coarse behavior with independent, evidence-based state
for each fixture component.

### New component states

Each API-Football fixture will track components such as:

- `result`;
- `lineups`;
- `team_statistics`;
- `player_statistics`;
- `events`;
- `identity_linking`;
- `pregame_lineup_capture`;
- `pregame_market_capture`;
- later correction refreshes.

Each component can be:

| State | Meaning |
|---|---|
| `pending` | Not attempted yet and still expected |
| `retryable` | Missing, incomplete, or temporarily failed; try again |
| `complete` | Validator confirms the stored facts are sufficient |
| `unavailable` | Provider or documented exhaustion proves the component cannot be obtained |
| `missed` | A time-sensitive capture window passed without a capture |
| `invalid` | Rows exist but violate a blocking rule |
| `terminal` | No more work should be attempted for a documented reason |

Only `complete`, evidence-backed `unavailable`, `missed`, or `terminal` stop
that component. `invalid` does not silently stop retries.

### Validators

Phase 2 will add deterministic validators for:

#### Result

- final or explicitly terminal status;
- non-null regulation scores;
- nonnegative home and away scores;
- no accidental treatment of postponed or administrative results as played
  sporting results.

#### Lineups

- one coherent raw artifact;
- exactly the two fixture teams;
- exactly 11 distinct starters for each team;
- no player starting for both teams;
- no unrecoverable duplicate starter entry.

Formation and bench completeness will remain optional.

#### Team statistics

- exactly two home/away team blocks from one coherent artifact;
- nonnegative values;
- shots on target do not exceed shots;
- accurate passes do not exceed total passes;
- provider-declared absence is `unavailable`, not falsely `complete`.

#### Player statistics

- unique player rows;
- only the fixture’s two teams;
- at least 22 positive-minute participants when player data is expected;
- participant minutes between 1 and 130;
- no negative or logically inconsistent values;
- partial blocks remain retryable or invalid.

#### Events

- successful response processing is recorded;
- a valid empty event list is `complete` with event count zero;
- nonempty events have valid fixture/team assignments and stable keys.

This distinction prevents “no events were returned” from being confused with
“the event endpoint was never processed.”

### Checkpoint changes

The existing checkpoint table will gain fields for:

- canonical fixture ID;
- component code;
- next attempt time;
- maximum attempts;
- priority;
- terminal reason;
- last run ID.

Checkpoint states will be divided into:

Retryable:

```text
pending, incomplete, failed, rate_limited
```

Stopping:

```text
succeeded, terminal, skipped_with_reason
```

Legacy `skipped` will remain readable but will not be written by new code.

`completed_at` will only be set for stopping states. Retryable checkpoints will
retain a future `next_attempt_at` when appropriate.

### Attempt history

A new `collection_attempt` table will record every attempt, including:

- job and run IDs;
- attempt number;
- fixture and component;
- start and finish times;
- HTTP status;
- raw artifact ID when a response existed;
- sanitized error class/message;
- requested and returned counts.

This separates “the request was attempted” from “the facts are complete.”

### Planner behavior after Phase 2

For every fixture, the planner will:

1. Read the latest schedule observation.
2. Validate actual stored facts for every component.
3. Update component state and validator details.
4. Read matching checkpoints.
5. Reopen a successful checkpoint if its facts are missing or invalid.
6. Suppress obsolete retries when facts are already complete.
7. Create the next concrete job when facts are missing and work remains.

Examples:

- A successful detail checkpoint exists but player rows are partial: player
  statistics becomes `retryable` and another job is created.
- Result and team statistics are complete but events were never processed:
  only events remains pending/retryable.
- A valid empty events response was processed: events becomes complete with
  `event_count=0`.
- A fixture is postponed: the current schedule’s jobs become terminal, but
  later schedule observations can create a new lifecycle.

### What Phase 2 will not do

Phase 2 will not yet implement the full rolling discovery window, four lineup
polling stages, HTTP backoff, process locking, repeated Polymarket cadence, or
health reports. Those are later phases that depend on this state foundation.

### Phase 2 verification

Phase 2 must pass tests proving:

- validators reject incomplete or malformed facts;
- successful checkpoints are reopened when facts are missing;
- complete facts suppress obsolete work;
- partial player data remains retryable;
- empty processed events are valid;
- invalid identities and assignments remain blocking or warning according to
  the component policy;
- legacy checkpoint rows migrate without changing fact counts;
- repeated planning is deterministic and makes no unnecessary requests;
- model eligibility remains independent from collector component state.

## 12. Approval boundary

Phase 1 is complete. Phase 2 should begin only after the reviewer is satisfied
with the component contracts, retryable/stopping state definitions, and
checkpoint reconciliation rules above.

No Phase 2 code should be implemented merely because a checkpoint exists. The
next phase must continue to preserve immutable raw evidence, canonical IDs,
nullable missing data, separate model eligibility, and safe copied-database
migration practice.
