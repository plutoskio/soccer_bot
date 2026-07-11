# Collector implementation plan

Status: planning only; no collector code or warehouse data was changed  
Prepared: 2026-07-10, Europe/Luxembourg  
Implementation audience: a coding agent that has not previously seen this repository

## Non-negotiable constraints

- Treat `data/warehouse/soccer.duckdb` as the canonical warehouse and `data/raw/` as immutable evidence.
- Never edit or delete a raw response to repair normalized data.
- Never join providers by display name. Use `source_entity_map` and canonical IDs.
- Missing data remains `NULL`; do not infer zero minutes, empty events, or unavailable statistics without provider evidence.
- Checkpoints describe work scheduling and attempts. They never prove that a fixture component is complete.
- Determine modeling eligibility only through `fixture_model_eligibility`, plus feature-specific non-null requirements. Collector completion is a different concern.
- Keep API-Football fixture batches at 20 IDs or fewer.
- Do not rerun completed repair scripts as part of this work.
- Do not inspect, print, persist, or test with `.env` contents or real API keys.
- Apply schema changes to a copied database first. Before any live migration, make and hash a verified backup.

## 1. Executive summary

### Current collector maturity

The collector is a useful run-once prototype, not yet an unattended service. It has a clear entry point, monitored-competition filtering, raw-response preservation, canonical loading, API quota reserve logic, fixture-ID batching, basic lineup and post-match scheduling, Polymarket linking, and restart checkpoints. The existing suite passes 53 tests.

The live warehouse was inspected read-only on 2026-07-10. Relevant facts were:

- 38,523 canonical fixtures and 23,726 API-Football fixture mappings.
- 23,619/23,619 fixtures in the approved historical manifest are present.
- Within that approved manifest, 23,526 fixtures pass all three model-eligibility flags. Across the whole warehouse, 23,559 fixtures pass all three flags; do not confuse these populations.
- Entire-warehouse eligibility totals are 38,391 result-eligible, 34,570 team-eligible, and 23,562 player-eligible.
- There are 674 open quality issues, all warnings: 501 unresolved lineup links, 130 unavailable player-stat sections, 30 administrative unplayed results, eight low-passing-coverage warnings, three duplicate lineup-entry warnings, and two unavailable team-stat sections.
- There are only five collector runs: four completed and one failed. The latest run was 2026-07-03.
- There are nine collector checkpoints: one fixture discovery, one Polymarket discovery, and seven post-match jobs. One post-match checkpoint is `incomplete`.
- The only successful discovery checkpoint is for 2026-07-03. Raw discovery artifacts cover requested dates 2026-07-01 through 2026-07-03, but collector state does not represent the first two.
- API-Football fixtures end at 2026-07-03. On 2026-07-10 this is direct evidence that the current collector does not recover missed dates or plan future dates.
- Polymarket has 252 events, 4,731 markets, 9,462 outcomes, but only one order-book snapshot. Thirteen events are linked to five fixtures.
- The passing baseline is 53/53 tests with `.venv/bin/python -m unittest discover -s tests -v`.

### Main risks

1. `incomplete` checkpoints are treated as finished. Partial data can therefore become permanently stranded.
2. Discovery only covers the current local date and becomes permanently complete after one request. Downtime is not recovered and future fixtures are not planned.
3. `fixture.scheduled_kickoff` and `fixture.status` are overwritten with the latest provider values; prior schedules and statuses are not preserved.
4. Lineups have only two polling stages, and the completion query trusts `lineup_snapshot.is_complete` rather than independently requiring 11 distinct starters for each of two teams.
5. The post-match completion query requires only any result, two team-stat teams, and any player-stat row. It does not require final status, score validity, participant quality, events processing, identity quality, or correction refreshes.
6. A legitimately empty event response is indistinguishable from an event endpoint that was never processed.
7. Request failures generally abort the whole run. There is no bounded retry classification, `Retry-After` handling, per-job attempt ledger, or continuation of unrelated work.
8. There is no collector lock. Two scheduler invocations can race before DuckDB itself rejects or serializes writers.
9. Pregame lineup aliases normally lack same-response player statistics, so the current post-match linker creates unresolved fixture-local identities.
10. Repeated identical lineup payloads reuse a snapshot ID based on content hash and `INSERT OR REPLACE` the later retrieval time. That can erase proof of the earliest pre-kickoff observation.
11. Polymarket discovery is once per day, only queries active/open events, and cannot perform a final closed-market refresh. Current token selection also excludes closed markets, preventing the requested after-closure book snapshot.
12. `--dry-run` avoids collector rows and network calls inside `Collector.run`, but the script still opens DuckDB writable, runs migrations, and upserts sources before planning. It is not a strictly read-only preview.

### Final target behavior

The run-once process should be safe to launch every five minutes. Each invocation should acquire a single-process lock, reconcile discovery coverage from a configurable recovery window through a future planning window, refresh schedule observations, validate actual fixture components, create only due jobs, execute independent batches with bounded failure handling, and update checkpoints only after revalidating stored facts.

The final fixture lifecycle is:

```text
discovered -> scheduled -> pregame_monitoring -> lineup_captured_or_missed
           -> kickoff_passed -> waiting_for_final -> final_components_collected
           -> validated -> correction_24h -> correction_72h -> terminal
```

Postponed, cancelled, abandoned, administrative, unavailable, and missed-pregame outcomes must be explicit. A missed pregame snapshot is terminal only for that time-sensitive component; it must not prevent recoverable post-match data from being collected later.

## 2. Current implementation

### Exact current workflow

`scripts/run_collector.py` performs these steps:

1. Parse only `--dry-run`.
2. Load `config/collector.json` and `.env`.
3. Open the live DuckDB writable, apply pending migrations, and upsert source rows.
4. Construct `Collector`; this requires a nonempty API key even for a dry run.
5. Call `Collector.run()` and print its JSON summary.

`Collector.run()` then:

1. Converts `now` to UTC and creates a run UUID.
2. Inserts a `collection_run` row unless dry-running.
3. Computes the current date in `Europe/Luxembourg`.
4. Calls `_discover_fixtures()` for that date only.
5. Selects current-date fixtures for Polymarket discovery.
6. Selects current-date fixtures plus two lookback days for detail planning.
7. Calls `_discover_polymarket()` once for the current match day, only when current-date monitored fixtures exist.
8. Plans lineup and post-match jobs with `_plan_detail_jobs()`.
9. Executes due API-Football details with `_execute_detail_jobs()`.
10. Links unmatched Polymarket events to selected fixtures by normalized team names and a six-hour time tolerance.
11. Plans and executes order-book jobs.
12. Updates the run row to `completed`; any uncaught exception updates it to `failed` and is re-raised.

Fixture discovery stores the full response as an immutable raw artifact, filters its in-memory `response` list to monitored competitions, and passes only the filtered payload to the loader. This filtering must be preserved because replaying unfiltered discovery responses can reintroduce out-of-scope shallow fixtures.

The detail endpoint is `/fixtures` with either `ids=ID-ID-...` or `id=ID`. One embedded response is loaded in this order: fixture/result, player statistics, lineups, events, and team statistics. Loading players before lineups is why post-match same-artifact identity linking usually works.

### Current scheduling thresholds

| Work | Current rule | Current retry behavior |
|---|---|---|
| Fixture discovery | Current local date, once ever per date checkpoint | None |
| Lineup primary | At or after T-50, while before kickoff | One later stage |
| Lineup retry | At or after T-35, while before kickoff | No T-20 or T-5 stage |
| Post-match primary | At or after T+150 | One late stage |
| Post-match retry | At or after T+1,590 minutes (26h30) | No live-status polling or correction stages |
| Fixture selection | Today plus two past local dates | No future window; no recovery beyond two days |
| Scheduler | Intended every five minutes | Not installed by the repository |

`lineup_stage()` returns no work at or after kickoff, so a missed pregame window is silently lost. `postmatch_stage()` ignores provider status and treats elapsed time as the only scheduling signal.

### Current API batching and quota behavior

- Configuration advertises multi-fixture `ids` support and a batch size of 20.
- All due job types are grouped by provider fixture ID; one embedded response can satisfy more than one due job for a fixture.
- Fixture IDs are sorted and split into groups of at most 20.
- The minimum interval between API-Football calls within one process is one second.
- The configured daily allowance is 7,500 with 250 reserved, so the collector allows calls while its computed usage is below 7,250.
- Usage is computed as the count of API-Football `raw_artifact` rows on `now.date()` in UTC, excluding resource `status`. This includes historical/backfill responses, which is conservative, but has no explicit provider reset timezone and cannot count network attempts with no response.
- Discovery also uses `_api_get()` and therefore checks the same reserve. Detail execution additionally stops before a batch when reserve is reached.
- There are no HTTP retries. HTTP 429, all non-200 statuses, invalid JSON, or an API-Football `errors` payload raise. HTTP error bodies are stored before validation; network exceptions without a response have no raw artifact.
- A single detail failure aborts the run instead of allowing unrelated fixture batches or Polymarket work to continue.
- The live run history contains one failure from a provider-plan error for `ids`; later runs succeeded after the configuration/provider capability changed. Capability errors must remain permanent failures, not retry loops.

### Current Polymarket behavior

- Gamma event discovery uses the soccer tag and current local day bounds.
- It requests only active, not-closed events, follows at most five keyset pages of 100 events, and checkpoints the whole day after one successful pass.
- Discovery is skipped when no monitored current-day fixture is already known.
- Event linking mutates `prediction_market_event.fixture_id` when both team names occur in the title and the event time is within six hours. It does not record a reviewed link decision or confidence.
- Order books are batched up to 500 outcome tokens in one public `/books` POST.
- One snapshot stage occurs after any complete lineup and before kickoff. A second occurs at T-5 and remains eligible through kickoff plus five minutes.
- Active/open market tokens only are selected. Closed-market snapshots are impossible under the current query.
- A job is marked successful when all its fixture tokens occur in the combined response set. Its metadata currently reports the global received-token count, not the count received for that fixture.

### Current checkpoint semantics

`collection_checkpoint.job_key` is the primary key. Fixture detail and market keys include provider fixture ID and the kickoff epoch; a changed kickoff creates a new key, but there is no explicit schedule version.

`_checkpoint_done()` treats `succeeded`, `incomplete`, and legacy `skipped` as done. This directly contradicts the rework specification: `incomplete` is not retried. `_record_checkpoint()` also sets `completed_at` for `incomplete` and `skipped`.

Checkpoint rows are written only after a request returns and loading completes. An exception before that point leaves no job-attempt record. There is no `next_attempt_at`, maximum attempt count, priority, terminal reason, run linkage, canonical fixture ID, or attempt history.

### Current completion checks

`_lineup_complete(fixture_id)` checks that two distinct API-Football teams have a confirmed snapshot with `is_complete=true`. The loader sets `is_complete` from `len(startXI) == 11` before lineup-entry deduplication. The collector therefore does not independently prove 11 distinct starter identities per team.

`_postmatch_complete(fixture_id)` returns true when all of these hold:

- at least one API-Football result observation exists;
- at least two distinct teams have API-Football team-stat rows;
- at least one API-Football player-stat row exists.

It does not check final fixture status, non-null/nonnegative regulation scores, home/away team identity, player participant count, player minutes, lineup linkage, invalid values, events processing, declared provider unavailability, or correction refreshes. It is intentionally much weaker than `fixture_model_eligibility` and must be replaced with component validators, not with a direct eligibility check.

### Relevant functions and files

- `scripts/run_collector.py`: CLI, config/environment loading, warehouse opening, exit behavior.
- `src/soccer_bot/collector.py`: orchestration, planning, batching, checkpoints, quota, linking, and request wrappers.
- `src/soccer_bot/loaders.py`: API-Football and Polymarket normalization; lineup identity behavior; current fixture overwrite behavior.
- `src/soccer_bot/database.py`: migrations, transactions, source maps, fixture upserts, canonical identity helpers.
- `src/soccer_bot/player_linking.py` and `player_names.py`: conservative post-match contextual identity scoring.
- `src/soccer_bot/raw_store.py`: immutable compressed bodies, content hashes, per-retrieval metadata, safe response-header allowlist.
- `src/soccer_bot/http.py`: low-level GET/POST and HTTP-error response capture.
- `config/collector.json`: current thresholds, quota, batch sizes, and monitored competitions.
- `migrations/001_initial.sql`, `002_collector.sql`, and `006_fixture_model_eligibility.sql`: fact schema, coarse collector state, and modeling eligibility.
- `tests/test_collector.py`: current scheduling, batching, filtering, idempotency, loading, and Polymarket batch tests.
- `tests/test_validation_harness.py`: raw-store and core identity invariants.
- `tests/test_model_eligibility.py`: model flags; these flags must not be repurposed as operational checkpoints.

## 3. Required changes

The phases below are dependency ordered. Keep each phase as a separate, reviewable change set. Do not start network scheduling or install `launchd` until the state schema, validators, and restart tests are accepted.

### Phase 1 — schedule-observation history

Add the schema in section 5 before changing fixture planning. Every API-Football fixture payload, including discovery and detail refreshes, must append an idempotent `fixture_schedule_observation` tied to the exact raw artifact. Store both the provider status code and a canonical status. Continue updating `fixture.scheduled_kickoff` and `fixture.status` as the current cache, but never rely on that cache to reconstruct what was known earlier.

Add one central status mapping. At minimum map API-Football `NS`/`TBD` to `scheduled`, live period codes to `live`, `HT`/break states to `live`, `INT` to `delayed`, `SUSP` to `suspended`, `FT`/`AET`/`PEN` to `final`, `PST` to `postponed`, `CANC` to `cancelled`, `ABD` to `abandoned`, `AWD`/`WO` and the repository's explicit administrative shape to `administrative_result`. Unknown codes must remain visible as `unknown`, create a warning, and stay retryable rather than being guessed.

Do not backfill historical schedule observations from the current fixture row. That would fabricate observation time and provenance. Existing rows may have no schedule history until a new provider artifact is processed.

Agent suitability: additive migration and pure status-mapping tests are suitable for a lower-cost agent. A human or stronger reviewer must approve the status map and any change to existing fixture values.

### Phase 2 — component-level completion and checkpoint redesign

Add `fixture_collection_component`, the checkpoint columns, and the attempt ledger in section 5. Implement pure validators for result, lineups, team statistics, player statistics, events processing, identity linking, and correction refreshes.

The planner must run validators before consulting checkpoints. A successful checkpoint with missing or invalid facts must be reopened as `incomplete`. A complete factual component may suppress an obsolete retry even if its checkpoint is missing. Explicit `unavailable`, `missed`, and terminal states require evidence and reason codes.

Do not use `fixture_model_eligibility` as the collector validator. Its purpose is model dataset gating across providers. Collector validators should be API-Football-component-specific, share domain rules where appropriate, and allow legitimate differences such as an empty processed event list.

Agent suitability: table creation, indexes, deterministic SQL validators, and unit tests are suitable if implemented on temporary databases. State-transition policy and migration of existing checkpoint semantics require review.

### Phase 3 — rolling fixture discovery and downtime recovery

On every invocation, calculate a local-date window from `today - recovery_days` through `today + planning_days`. Defaults are 14 and 7. Automatically expand the past bound to the latest monitored completed-fixture frontier so downtime and off-season gaps do not omit model history. `--catch-up-days N` may expand it further but must not reduce either automatic bound.

Discovery rules:

- For every past date in the window, ensure at least one successful discovery request exists. Query dates with no successful evidence.
- For future dates through seven days, refresh at least daily.
- Refresh today and tomorrow every six hours.
- Schedule a fixture-specific refresh near kickoff, and refresh again after postponed/cancelled signals.
- Keep concrete discovery jobs immutable by cadence slot, for example `api_football:fixture_discovery:2026-07-12:six_hour:2026-07-10T12`.
- After discovery, filter to monitored competitions before canonical loading.
- For discovered past fixtures, validate every post-match component and batch only missing/retryable details.
- If kickoff has passed without a valid pre-kickoff lineup or market snapshot, mark only those pregame capture components `missed`. A later recovered lineup remains valid historical lineup data but never becomes a pregame observation.

The three-day and three-week outage tests must pass before enabling the future scheduler.

Agent suitability: date-window generation, freshness rules, CLI parsing, and fake-HTTP tests are safe for a lower-cost agent after the checkpoint contract exists. Review actual provider call counts before live use.

### Phase 4 — adaptive confirmed-lineup collection

Replace the two hard-coded stages with configured offsets `[50, 35, 20, 5]`. Each stage is a concrete job tied to the fixture's schedule version (the schedule-observation ID or exact kickoff known when planned). Batch all due fixture IDs, load the raw response, and independently validate two teams with exactly 11 distinct starters each.

Stop creating later lineup jobs as soon as factual completeness passes. Do not require formation or bench completeness. Persist `kickoff_known_at_retrieval`, `schedule_observation_id`, and `captured_before_kickoff` on each new lineup snapshot. Generate distinct lineup snapshots per raw retrieval artifact so a later identical payload cannot overwrite the earliest observation.

When a schedule changes, terminalize obsolete pregame jobs with reason `schedule_superseded` and create a fresh set for the new schedule. Never rewrite the old observations.

Agent suitability: pure stage generation and starter-count tests are straightforward. Snapshot-key changes and temporal provenance require careful loader review.

### Phase 5 — pregame player identity handling

Extend the existing conservative linker rather than adding a name-only shortcut. Candidate resolution order is:

1. Exact API-Football identity key `(provider player ID, normalized provider name)`.
2. Same numeric provider ID plus a compatible name; provider ID alone is insufficient because API-Football reuses numeric IDs.
3. One unique compatible canonical player with a recent appearance for the same team.
4. One unique candidate with compatible name, matching shirt number, and same-team context.
5. Otherwise create or retain the fixture-local unresolved lineup alias.

Use existing transliteration and name-compatibility helpers only for comparison. Do not change canonical stable identity keys. Require a unique best candidate and minimum margin as the existing linker does. Record method, evidence, confidence, and review status in `source_entity_map`.

After post-match player statistics arrive, rerun reconciliation for unresolved aliases from all lineup snapshots for that fixture. Update only reviewed mapping/link relationships; do not merge global players by display name. Ambiguity remains unresolved and produces a warning, not a guess.

Agent suitability: test fixtures and query helpers are delegable. Scoring thresholds, mapping rewrites, and any player merge are not safe for an unsupervised lower-cost agent.

### Phase 6 — status-aware post-match collection

Treat T+150 as a first status/detail check. Then:

- If `live` or `delayed`, poll every 30 minutes until kickoff plus six hours.
- If `suspended`, retain retryable state and follow configured status refreshes; do not invent a final result.
- If `final`, validate components independently. Retry incomplete final data at +8 hours.
- Run correction refreshes near +24 hours and +72 hours even if earlier facts were complete.
- `postponed` terminalizes the old schedule's jobs but remains discoverable; a new kickoff observation starts a new lifecycle.
- `cancelled` and `abandoned` are fixture-terminal unless a later provider observation reschedules them.
- `administrative_result` is terminal for sporting-data collection and remains model-ineligible under existing policy.
- Mark `data_unavailable` only after explicit provider evidence or exhaustion of correction attempts under a documented rule. Absence in one early response is `retryable`, not `unavailable`.

One details response may satisfy result, lineups, events, team stats, player stats, and identity reconciliation. Store the raw artifact once, load it in one database transaction, then validate every requested component separately.

Agent suitability: the timing table and pure state transitions are delegable. Status-terminal decisions and component-unavailability policy require review.

### Phase 7 — retries, rate limits, and failure handling

Create one request executor used by API-Football and Polymarket. It must classify:

- retryable network failures and timeouts;
- HTTP 429, honoring `Retry-After` as seconds or HTTP date;
- selected 5xx responses as retryable;
- permanent 4xx responses, including capability/auth/config errors, as non-retryable;
- invalid JSON and provider schema errors as retryable only under a bounded schema-failure policy;
- API-Football HTTP-200 `errors` payloads by error code/message without logging secrets.

Use short bounded inline retries only when the required delay is below `maximum_inline_retry_seconds`. Otherwise set `next_attempt_at` and finish the run. Apply exponential backoff with bounded jitter for transient failures. Every attempt gets a ledger row; every response body, including error bodies, remains in the raw store. A network failure with no response has an attempt row and no fake raw artifact.

Do not mark all fixtures in a batch failed when only one fixture is omitted from an otherwise valid response. Revalidate each fixture. Continue other batches and the other provider unless a configuration, database, or system-wide failure makes safe operation impossible.

Compute daily quota in the provider reset timezone and retain the 250-call reserve. Continue using raw artifacts as the inclusive cross-workflow evidence of calls, but also report request attempts and provider-reported remaining quota. Enforce the one-second API-Football interval across inline retries.

Agent suitability: response classification and fake-clock tests are delegable. Live error-message classification and quota changes require review against current provider documentation before deployment.

### Phase 8 — collector locking

Acquire a filesystem lock before opening DuckDB writable or running migrations. Use an atomic lock file under `data/warehouse/collector.lock`, containing only PID, hostname, process start marker, acquisition time, and heartbeat time. Never put secrets or command-line environment values in it.

On contention, verify whether the owning process is alive on the same host. Exit cleanly with code 0 and a structured `already_running` summary when active. Reclaim only when the process is demonstrably absent or the heartbeat exceeds the configured stale timeout. Release in `finally`; crash recovery must be tested.

An OS advisory lock (`fcntl.flock`) may be used on macOS, but keep the metadata/heartbeat so stale-state diagnostics are understandable. Do not use a DuckDB row as the only lock because the lock must be acquired before a second writable DuckDB connection.

Agent suitability: a self-contained lock module and subprocess tests are suitable for a lower-cost agent. Installation paths and stale-lock deletion should be reviewed.

### Phase 9 — Polymarket discovery and order-book cadence

Replace once-per-day completion with concrete cadence-slot jobs:

- Discover events every 60 minutes for fixtures within seven days.
- Discover every 15 minutes on match day.
- Refresh known event/market IDs once after closure, including closed data.
- Capture books at T-24h, T-6h, T-90m, immediately after a confirmed lineup, T-15m, T-5m, and after closure.

Link after every discovery refresh. Preserve current conservative matching, but record link method/confidence and do not silently relink an already linked event without an explicit conflict path. Add append-only event and market metadata observations so active/closed/rules state at retrieval time is queryable; keep current tables as latest-state caches.

For time-sensitive stages, require the book's local retrieval time to be within the stage window and compare it with the kickoff known at that retrieval. Never backfill a missed T-24h or pregame price label using a later response. The closure job may query tokens from closed markets; do not reuse the active/open-only token query.

Fix per-job metadata to report the intersection of requested and received tokens for that fixture. Missing tokens leave that job retryable. Preserve the 500-token batch maximum.

Agent suitability: cadence generation, per-fixture response accounting, and tests are delegable. Event-link conflict handling and metadata-history schema require review.

### Phase 10 — daily health reporting

Generate/update one machine-readable daily health row and one generated Markdown report after each run. Include:

- discovery dates expected, fresh, missing, failed, and last successful retrieval;
- monitored fixture counts by lifecycle status;
- valid pregame lineups and missed pregame captures;
- final results and every missing/invalid/unavailable post-match component;
- unresolved player identities;
- pending, retryable, rate-limited, failed, and terminal jobs;
- API-Football calls used, configured reserve, inferred remaining calls, and provider-reported remaining calls when present;
- linked Polymarket fixtures and snapshots by cadence stage;
- lock contention and run failures.

Use exit code 0 for success, no work, retryable per-job failures, or an active lock. Use exit code 1 for configuration/database/system failures. Use exit code 2 only when the run completed but health validation found a defined blocking integrity problem. Do not make an ordinary missed snapshot or temporary 429 a process-wide failure.

Agent suitability: SQL aggregation and Markdown rendering are delegable after metric definitions are frozen. Severity policy needs review.

### Phase 11 — operating-system scheduling

Add a tracked example `ops/launchd/com.soccer-bot.collector.plist.example` and setup instructions. It should invoke the repository's `.venv/bin/python` and `scripts/run_collector.py` every 300 seconds with the repository as working directory. Log to a local ignored directory with rotation guidance. Do not embed API keys, `.env` values, or wallet data in the plist.

Do not automatically install or load the plist. Installation changes external OS state and must be a separate user-approved step after a manual observation period. Document that sleep can miss pregame information even though post-match facts recover later.

Agent suitability: template and documentation are safe. Loading/unloading the service is not delegated and is not part of repository implementation.

## 4. File-by-file implementation plan

| File | Change and reason | Dependencies and invariants | Type |
|---|---|---|---|
| `migrations/007_collector_rework.sql` (new) | Add schedule observations, component state, attempt ledger, health table, checkpoint columns, lineup temporal columns, Polymarket metadata observations, and indexes from section 5. | Additive and idempotent; no raw/fact deletion; no guessed historical backfill. Test on copies first. | Migration |
| `src/soccer_bot/database.py` | Support explicit read-only opening for true dry runs; add small transaction-safe helpers only if shared by state persistence. | Existing migrations remain ordered; never run `migrate()` on a read-only connection. Preserve stable IDs/source maps. | Code |
| `src/soccer_bot/collector.py` | Reduce to run orchestration: startup reconciliation, planner calls, batch execution, provider isolation, run summary. Replace current stage functions, completion checks, and checkpoint semantics. | Depends on state, retry, and lock modules. Preserve monitored filtering, 20-ID batching, quota reserve, and immutable raw capture. | Code |
| `src/soccer_bot/collection_state.py` (new) | Define fixture/component/checkpoint constants, validators, state transitions, freshness rules, and job-key construction. | Pure logic where possible; planner always validates facts before checkpoints. | Code |
| `src/soccer_bot/collection_planner.py` (new) | Build discovery, lineup, status, correction, and market jobs from time, schedule observations, component facts, and retry state. | Use injected clock; no HTTP or database writes in planning functions. | Code |
| `src/soccer_bot/request_executor.py` (new) | Centralize retries, backoff, `Retry-After`, classification, per-attempt persistence, and safe error text. | Must store every response before validation and never log request headers/secrets. | Code |
| `src/soccer_bot/locking.py` (new) | Atomic collector lock, heartbeat, owner validation, stale recovery, and context manager. | Acquire before writable DuckDB open; release in `finally`. | Code |
| `src/soccer_bot/health.py` (new) | Query daily metrics, persist `collection_health_report`, and render generated Markdown. | Read canonical facts/component state; do not alter eligibility. | Code |
| `scripts/run_collector.py` | Add `--catch-up-days`, optional `--now` only for tests/development if safely guarded, true read-only dry run, lock-before-database ordering, explicit exit codes, and structured summary. | Do not print API key. Refuse negative catch-up. Dry run must make no filesystem/database/network changes. | Code |
| `src/soccer_bot/loaders.py` | Append schedule observations for every fixture artifact; centralize status mapping; create per-retrieval lineup snapshots with temporal fields; write market metadata observations; invoke pregame/post-match identity reconciliation. | Raw artifact is registered before normalization. Normalize each response transactionally. Preserve historical loader behavior and provider-specific IDs. | Code |
| `src/soccer_bot/player_linking.py` | Add candidate construction/scoring support for recent same-team evidence and explicit evidence ordering. | Keep unique-best/margin policy. Provider ID alone and display name alone never authorize a merge. | Code |
| `src/soccer_bot/player_names.py` | Only add comparison helpers if required; do not change canonical normalization or identity-key construction. | Existing transliteration tests must stay unchanged. | Code, possibly none |
| `src/soccer_bot/raw_store.py` | Usually no schema change. Add only a helper for sanitized retry metadata if needed; continue storing HTTP error bodies and safe headers including `Retry-After`. | Never overwrite bodies; keep content-hash physical dedup and per-retrieval metadata. | Code, likely minimal |
| `src/soccer_bot/http.py` | Return or raise typed network failures so the executor can distinguish timeout/network from HTTP responses. Keep HTTP error bodies as `HttpResponse`. | No automatic long sleeps here; no secret-bearing diagnostics. | Code |
| `config/collector.json` | Add windows, stage arrays, status polling, correction offsets, retry/backoff, quota reset timezone, lock, health, and Polymarket cadence keys. Remove old scalar timing keys only after compatibility code/tests are removed. | Validate types/ranges at startup. Preserve league IDs and competition keys exactly. | Configuration |
| `tests/test_collector.py` | Replace two-stage assertions; extend integration tests for fact-first planning, per-batch continuation, reschedules, correction refresh, and true idempotency. | Use temp DB, fake clock, fake HTTP; no live network. | Test |
| `tests/test_collection_state.py` (new) | Exhaustive component/state-transition and terminal/retryable tests. | Table-driven, deterministic. | Test |
| `tests/test_collector_recovery.py` (new) | Three-day/three-week downtime, missing dates, future freshness, recovered-lineup timing, and restart tests. | Assert exact requested dates and batch sizes. | Test |
| `tests/test_collector_migration.py` (new) | Apply all migrations to empty DB and 006-era fixture DB; reapply migration helpers safely; verify legacy checkpoints. | Run only on copies/temp DB. | Test |
| `tests/test_http_retry.py` (new) | 429, `Retry-After`, 5xx, timeout, permanent 4xx, malformed JSON, provider errors, jitter bounds, and attempt ledger. | Inject sleep/random/clock. | Test |
| `tests/test_collector_lock.py` (new) | Two-process contention, clean release, crash/stale recovery, and lock-before-DB behavior. | Use subprocesses and temp paths. | Test |
| `tests/test_polymarket_collector.py` (new) | Discovery cadence, closed refresh, all seven book stages, missed-stage behavior, token batching, and per-fixture accounting. | No public API calls. | Test |
| `tests/test_health_report.py` (new) | Metric counts, severity, safe rendering, and exit-code decisions. | Use known temp fixtures and compare stable fields. | Test |
| `tests/test_validation_harness.py` | Add true read-only dry-run/no-file-change and raw error-header/body preservation tests. | Continue secret-redaction assertions. | Test |
| `tests/test_model_eligibility.py` | Add regression proving collector component states do not change model eligibility and eligibility does not falsely complete collector correction jobs. | Keep exactly three consumer-facing flags. | Test |
| `tests/test_player_linking.py` | Add pregame exact-ID/name, compatible same-ID, recent-team unique match, shirt/team match, ambiguity, and post-match reconciliation cases. | Ambiguity must remain unresolved. | Test |
| `ops/launchd/com.soccer-bot.collector.plist.example` (new) | Five-minute run-once example without secrets or auto-installation. | Depends on successful observation trial and documented absolute-path substitution. | Operations template |
| `README.md` | Document new CLI, recovery, health output, locking, exit codes, and manual `launchd` setup. Remove obsolete two-stage claims. | Update only when behavior exists. | Documentation |
| `DAILY_COLLECTION_REWORK.md` | Mark completed items or replace status with an implementation reference after acceptance; do not silently rewrite the original requirements during coding. | Plan remains the source of acceptance behavior until completion. | Documentation |
| `DATA_ARCHITECTURE.md` | Document implemented temporal/state tables and latest-state versus append-only observations. | Keep collector completeness distinct from model eligibility. | Documentation |
| `TODO.md` | Check off only verified collector tasks and link tests/report evidence. | No premature completion claims. | Documentation |
| `AGENTS.md` | Update collector limitations and validation counts only after live verification. | Preserve safety and eligibility instructions. | Documentation |

## 5. Database design

Use the exact additive design below unless review identifies a DuckDB limitation. IDs are deterministic UUID strings produced by `stable_id`; timestamps are `TIMESTAMPTZ`; JSON fields contain only sanitized structured data.

### 5.1 `fixture_schedule_observation` (new)

| Column | Type / rule |
|---|---|
| `schedule_observation_id` | `VARCHAR PRIMARY KEY`; stable from source, provider fixture ID, and `raw_artifact_id` |
| `fixture_id` | `VARCHAR NOT NULL` canonical fixture |
| `source_code` | `VARCHAR NOT NULL` |
| `fixture_source_id` | `VARCHAR NOT NULL` |
| `provider_status` | `VARCHAR` exact short provider code |
| `canonical_status` | `VARCHAR NOT NULL`; values below |
| `scheduled_kickoff` | `TIMESTAMPTZ` as reported in this artifact |
| `observed_at` | `TIMESTAMPTZ`; provider timestamp if reliable, else `NULL` |
| `retrieved_at` | `TIMESTAMPTZ NOT NULL` |
| `raw_artifact_id` | `VARCHAR NOT NULL` |
| `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT current_timestamp` |

Canonical status values: `scheduled`, `live`, `delayed`, `suspended`, `final`, `postponed`, `cancelled`, `abandoned`, `administrative_result`, `unknown`.

Indexes:

- `(fixture_id, retrieved_at)`
- `(canonical_status, scheduled_kickoff)`
- unique `(source_code, fixture_source_id, raw_artifact_id)`

### 5.2 `lineup_snapshot` additions

Add nullable columns:

- `schedule_observation_id VARCHAR`
- `kickoff_known_at_retrieval TIMESTAMPTZ`
- `captured_before_kickoff BOOLEAN`
- `identity_state VARCHAR` with new values `resolved`, `partially_resolved`, `unresolved`

New snapshot IDs must use `raw_artifact_id`, not only content hash. Leave existing values `NULL`; do not infer historical pregame status from rows whose earliest retrieval may have been overwritten.

### 5.3 `fixture_collection_component` (new)

| Column | Type / rule |
|---|---|
| `fixture_id` | `VARCHAR NOT NULL` |
| `source_code` | `VARCHAR NOT NULL` |
| `component_code` | `VARCHAR NOT NULL` |
| `state` | `VARCHAR NOT NULL` |
| `required_for_fixture_terminal` | `BOOLEAN NOT NULL` |
| `reason_code` | `VARCHAR` |
| `details` | `JSON` validator counts/reasons |
| `first_attempt_at` | `TIMESTAMPTZ` |
| `last_attempt_at` | `TIMESTAMPTZ` |
| `validated_at` | `TIMESTAMPTZ` |
| `last_raw_artifact_id` | `VARCHAR` |
| `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT current_timestamp` |

Primary key: `(fixture_id, source_code, component_code)`.

Component codes:

`result`, `lineups`, `team_statistics`, `player_statistics`, `events`, `identity_linking`, `pregame_lineup_capture`, `pregame_market_capture`, `correction_refresh_24h`, `correction_refresh_72h`.

State values:

`pending`, `retryable`, `complete`, `unavailable`, `missed`, `invalid`, `terminal`.

Only `complete`, evidence-backed `unavailable`, `missed`, or `terminal` stop that component. `invalid` is blocking and must not silently stop retries until maximum attempts and terminal policy are evaluated. Identity linking is measured but normally `required_for_fixture_terminal=false`; unresolved identities remain warnings after the final reconciliation.

Use these exact validator contracts:

| Component | `complete` rule |
|---|---|
| `result` | Latest canonical schedule status is `final` (legacy `fixture.status='completed'` is accepted during transition) and an API-Football final result has non-null, nonnegative home and away regulation scores. Postponed/cancelled/administrative states use explicit terminal policy instead. |
| `lineups` | One coherent raw artifact supplies exactly the fixture's two teams, with exactly 11 distinct `starter` player IDs for each team, no player starting for both teams, and no unrecoverable duplicate starter entry. Bench and formation may be missing. |
| `team_statistics` | One coherent raw artifact supplies exactly two blocks for the fixture's home/away teams; present values satisfy nonnegative and logical constraints such as shots on target not exceeding shots and accurate passes not exceeding passes. A provider-declared missing section becomes `unavailable`, not `complete`. |
| `player_statistics` | One coherent raw artifact has unique player rows for exactly the home/away teams, at least 22 positive-minute participants, minutes in 1–130 for participants, and no invalid negative/logically inconsistent values. Explicit provider absence becomes `unavailable`; partial blocks are `retryable` or `invalid`. |
| `events` | A details response was successfully processed and its raw artifact is recorded in component state. Zero events is valid and recorded as `details.event_count=0`; nonempty events must have valid fixture/team assignments and stable natural keys. |
| `identity_linking` | Record linked and unresolved counts after reconciliation. It is `complete` only when all relevant aliases link safely; unresolved aliases remain a nonblocking warning/terminal disposition after final reconciliation. |
| `correction_refresh_24h` / `correction_refresh_72h` | The scheduled refresh request succeeded, its raw artifact was processed, and all factual components were revalidated. Earlier factual completeness does not complete a correction job. |
| `pregame_lineup_capture` | The lineup validator passed for a snapshot whose local retrieval time is earlier than the kickoff in its linked schedule observation. |
| `pregame_market_capture` | The required configured pregame market stages have valid locally retrieved books within their windows. Track individual stage jobs in checkpoints and summarize the component; expired absent stages are `missed`. |

Index: `(state, updated_at)` and `(fixture_id, state)`.

### 5.4 `collection_checkpoint` additions

Add:

- `fixture_id VARCHAR` canonical ID, nullable for discovery/global jobs
- `component_code VARCHAR`
- `next_attempt_at TIMESTAMPTZ`
- `maximum_attempts INTEGER NOT NULL DEFAULT 1`
- `priority INTEGER NOT NULL DEFAULT 2` where 0 is highest
- `terminal_reason VARCHAR`
- `last_run_id VARCHAR`

New checkpoint status values:

- Retryable: `pending`, `incomplete`, `failed`, `rate_limited`
- Stopping: `succeeded`, `terminal`, `skipped_with_reason`
- Compatibility: read legacy `skipped` as stopping, but never write it in new code

`completed_at` is non-null only for stopping states. A retryable row must have `next_attempt_at` unless it is immediately due. Replace the current index with or add `(status, next_attempt_at, priority)` and `(fixture_id, component_code, status)`.

Concrete job keys must identify the schedule/cadence version. Do not reuse one permanently successful daily job for recurring discovery.

### 5.5 `collection_attempt` (new)

| Column | Type / rule |
|---|---|
| `collection_attempt_id` | `VARCHAR PRIMARY KEY` |
| `job_key` | `VARCHAR NOT NULL` |
| `collection_run_id` | `VARCHAR NOT NULL` |
| `attempt_number` | `INTEGER NOT NULL` |
| `source_code` | `VARCHAR NOT NULL` |
| `job_type` | `VARCHAR NOT NULL` |
| `fixture_id` | `VARCHAR` |
| `started_at` | `TIMESTAMPTZ NOT NULL` |
| `finished_at` | `TIMESTAMPTZ` |
| `status` | `VARCHAR NOT NULL` |
| `http_status` | `INTEGER` |
| `retry_after_seconds` | `INTEGER` |
| `quota_cost` | `INTEGER NOT NULL DEFAULT 1` |
| `raw_artifact_id` | `VARCHAR` |
| `error_class` | `VARCHAR` sanitized category |
| `error_message` | `VARCHAR` sanitized text |
| `metadata` | `JSON` requested/returned counts and batch fingerprint |

Attempt statuses: `running`, `succeeded`, `incomplete`, `retryable_error`, `rate_limited`, `permanent_error`, `cancelled`.

Unique `(job_key, collection_run_id, attempt_number)`. Index `(collection_run_id, source_code)` and `(job_key, started_at)`.

One HTTP batch may create one attempt row per affected job, all pointing to the same raw artifact and batch fingerprint. This keeps job history simple while preserving request sharing.

### 5.6 Polymarket metadata observations (new)

`prediction_market_event_observation`:

- `event_observation_id VARCHAR PRIMARY KEY`
- `prediction_market_event_id VARCHAR NOT NULL`
- `raw_artifact_id VARCHAR NOT NULL`
- `active BOOLEAN`, `closed BOOLEAN`
- `start_time TIMESTAMPTZ`, `end_time TIMESTAMPTZ`
- `title VARCHAR`, `description VARCHAR`, `resolution_source VARCHAR`
- `observed_at TIMESTAMPTZ`, `retrieved_at TIMESTAMPTZ NOT NULL`
- unique `(prediction_market_event_id, raw_artifact_id)`
- index `(prediction_market_event_id, retrieved_at)`

`prediction_market_observation`:

- `market_observation_id VARCHAR PRIMARY KEY`
- `prediction_market_id VARCHAR NOT NULL`
- `raw_artifact_id VARCHAR NOT NULL`
- `active BOOLEAN`, `closed BOOLEAN`
- `rules_text VARCHAR`, `volume DOUBLE`, `liquidity DOUBLE`
- `observed_at TIMESTAMPTZ`, `retrieved_at TIMESTAMPTZ NOT NULL`
- unique `(prediction_market_id, raw_artifact_id)`
- index `(prediction_market_id, retrieved_at)`

Existing event/market tables remain latest-state caches. Existing order-book tables are already append-oriented and should not be replaced.

### 5.7 `collection_health_report` (new)

- `report_date DATE PRIMARY KEY` in configured local timezone
- `generated_at TIMESTAMPTZ NOT NULL`
- `timezone VARCHAR NOT NULL`
- `status VARCHAR NOT NULL`: `healthy`, `warning`, `blocking`
- `metrics JSON NOT NULL`
- `issues JSON NOT NULL`
- `last_run_id VARCHAR`

This is derived operational state and may be upserted for the current date. The Markdown report is also generated and can be regenerated from this row plus warehouse facts.

### 5.8 Migration safety and idempotency

1. Use a new ordered migration; never edit applied migrations 001–006.
2. Use `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and `CREATE INDEX IF NOT EXISTS`.
3. Do not delete, rekey, or rewrite fact rows in the schema migration.
4. Do not manufacture schedule observations or pregame flags for historical rows.
5. Leave legacy `incomplete` checkpoints as-is; new planner semantics make them retryable. Do not bulk clear `completed_at` unless a reviewed repair with expected-count guards is approved.
6. Test migration against an empty database and a byte-for-byte copy of the live database. Capture table counts and key fingerprints before and after.
7. Because the normal migration runner is transactional per file, any data backfill that cannot safely be one transaction belongs in a separate guarded command, not this migration.
8. Rollback is database-file restoration, not a down migration. Stop scheduling, preserve new raw artifacts, restore the verified backup, and replay new artifacts only through a reviewed process.

## 6. Collector state machine

### Fixture lifecycle

| Lifecycle state | Entry condition | Due work | Exit condition |
|---|---|---|---|
| `discovered` | Monitored provider fixture loaded | Store schedule observation | Current schedule/status known |
| `scheduled` | Future kickoff and scheduled status | Future refreshes and pregame jobs | Enters lineup window or status changes |
| `pregame_monitoring` | T-50 through kickoff | T-50/T-35/T-20/T-5 lineup; market stages | Valid pregame lineup or kickoff passes |
| `lineup_captured` | Two valid starting elevens retrieved before known kickoff | Lineup-triggered book snapshot | Kickoff passes |
| `pregame_missed` | Kickoff passed without qualifying capture | Mark pregame component missed; optionally recover historical lineup later | Continue post-match lifecycle |
| `waiting_for_final` | Kickoff passed, not final | T+150 then 30-minute status/detail checks | Final or exceptional status |
| `collecting_final` | Final status known | Validate/retry each factual component | Required components complete/unavailable/terminal |
| `validated` | Required components satisfy policy | Schedule corrections | +24h refresh due |
| `correction_24h` | First correction refresh completed | Wait/schedule +72h | +72h refresh due |
| `terminal` | Final correction done or explicit exceptional terminal state | Health reporting only | New provider schedule may create a new schedule lifecycle |

### Component state rules

- `pending`: never attempted and still potentially available.
- `retryable`: attempted or newly reopened; facts absent/incomplete and policy allows another attempt.
- `complete`: the component validator currently passes against stored facts.
- `unavailable`: provider explicitly declares absence, or final policy exhausts attempts with evidence. Missing once is not enough.
- `missed`: a time-sensitive pregame observation can no longer be captured. Later historical data does not change this state.
- `invalid`: rows exist but fail a blocking domain rule. Preserve raw evidence, retry/reconcile, and surface health blocking.
- `terminal`: component cannot or should not receive more work for a documented reason.

### Retryable versus terminal fixture statuses

- Retryable: `scheduled`, `live`, `delayed`, `suspended`, `unknown`, and `final` with missing/incomplete components.
- Terminal for the current schedule: `postponed`, `cancelled`, `abandoned`, `administrative_result`.
- A later schedule observation can reactivate postponed/cancelled/abandoned fixtures only through an explicit transition and new schedule-version jobs.
- `data_unavailable` is a component terminal reason, not a provider match status.

### Checkpoints versus actual completeness

For every planned fixture:

1. Read the latest schedule observation and current canonical fixture.
2. Recompute each factual component from normalized rows plus explicit processing evidence.
3. Upsert component state and validator details.
4. Read matching checkpoints.
5. If facts are complete, suppress obsolete retries and terminalize them with `facts_already_complete` where useful.
6. If a checkpoint says `succeeded` but facts are not complete, change it to `incomplete`, set `next_attempt_at`, and record `checkpoint_fact_mismatch` in metadata.
7. If no checkpoint exists and facts are missing, create the next due concrete job.
8. Never delete a checkpoint to force a retry; preserve its attempt history.

## 7. Detailed algorithm and pseudocode

### Startup and lock acquisition

```text
parse CLI and validate config without opening DuckDB
acquire collector lock atomically
if active owner exists:
    print {status: "already_running"}
    exit 0
try:
    if dry_run:
        open warehouse read-only
        verify required migration version exists
    else:
        open warehouse writable
        apply migrations and register sources
        insert collection_run(status="running")
    now = injected time or current UTC
    reconcile discovery coverage and plan all due jobs
    if not dry_run:
        execute jobs by priority and provider isolation
        generate health report
        finish collection_run with completed/partial/blocking summary
    print sanitized summary
finally:
    close warehouse
    release lock
```

### Discovery recovery

```text
today = now in configured timezone
frontier_days = days since latest monitored completed fixture, if any
past_days = max(config.recovery_days, frontier_days, CLI catch_up_days if supplied)
target_dates = [today - past_days, ..., today + planning_days]

for target_date in target_dates:
    required_slot = discovery_slot(target_date, today, now)
    if no successful concrete checkpoint for required_slot:
        enqueue discovery(target_date, priority based on past/today/future)

for discovery job in priority order:
    GET /fixtures?date=target_date&timezone=config.timezone
    store/register raw response before JSON/domain validation
    filter response to configured monitored competitions
    transaction:
        load filtered fixtures
        append schedule observations
    validate expected returned fixture identities
    checkpoint succeeded or schedule retry

for every monitored discovered fixture whose kickoff is in the past:
    validate actual components
    mark expired pregame capture components missed when appropriate
    enqueue missing post-match/status work, batching at execution time
```

### Fixture planning

```text
for fixture in monitored fixtures across recovery and planning windows:
    schedule = latest schedule observation, else current fixture schedule
    components = validate_all_components(fixture)
    reconcile component rows and stale checkpoints

    if terminal exceptional status for this schedule:
        terminalize schedule-specific jobs
        continue, except retain future status refresh if policy requires

    if kickoff is future:
        enqueue due schedule refresh, lineup stages, and market stages
    else if status is not final:
        enqueue T+150 or next 30-minute status check
    else:
        enqueue missing component retry, +8h retry, and due corrections

sort by priority, next_attempt_at, kickoff, provider fixture ID
```

### Lineup polling

```text
for offset in [50, 35, 20, 5]:
    stage_time = kickoff_known - offset minutes
    if now >= stage_time and now < kickoff_known:
        if pregame lineup validator already passes:
            stop creating lineup jobs
        else if concrete stage job is due/retryable:
            enqueue fixture ID

request due IDs in batches <= 20
for each response:
    store raw artifact
    transactionally load fixture/player/lineup/event/stat sections present
    validate each requested fixture independently
    if two teams each have 11 distinct starters:
        set lineups complete
        if retrieval < kickoff from linked schedule observation:
            set pregame_lineup_capture complete
            enqueue immediate-after-lineup market snapshot
    else:
        set lineups retryable and schedule next stage

if now >= kickoff and pregame capture is not complete:
    set pregame_lineup_capture missed
    cancel remaining pregame stages
```

### Post-match polling

```text
if now < kickoff + 150 minutes:
    no post-match job
else request details/status in batches <= 20

after load, read latest canonical status:
    live or delayed and now < kickoff + 6h:
        next_attempt_at = now + 30m
    suspended or unknown:
        next_attempt_at = status policy time
    postponed/cancelled/abandoned/administrative_result:
        terminalize current schedule with reason
    final:
        validate result, lineups, team stats, player stats, events, identities
        retry missing/invalid components at configured bounded backoff
        guarantee an incomplete-final retry around +8h
        enqueue correction jobs at +24h and +72h

after each correction response:
    transactionally load corrected observations
    revalidate every component and identities
    complete the correction component only after request+validation succeeds
```

### Retry and backoff handling

```text
for request batch:
    for inline_attempt from 1 to max_inline_attempts:
        write attempt(status=running)
        try request
        if response exists:
            store raw body and register artifact
        classify outcome
        update attempt with status/http/raw/error

        if success:
            load transactionally, validate per job, update each checkpoint
            break
        if permanent:
            terminalize only affected jobs; continue unrelated work
            break
        delay = Retry-After or exponential_backoff_with_bounded_jitter()
        if delay <= maximum_inline_retry_seconds and attempts remain:
            sleep using injected sleeper
            continue
        set checkpoint rate_limited/failed with next_attempt_at=now+delay
        break
```

### Checkpoint updates

```text
before execution:
    upsert checkpoint pending with concrete job key and last_run_id
on retryable result:
    status = incomplete | failed | rate_limited
    completed_at = NULL
    next_attempt_at = computed time
    attempts += 1
on factual success:
    rerun component validator
    only if validator passes:
        status = succeeded
        completed_at = now
        next_attempt_at = NULL
on exceptional terminal:
    status = terminal or skipped_with_reason
    terminal_reason must be non-null
```

### Polymarket snapshots

```text
generate concrete discovery slots from fixture proximity and match day
after each Gamma response:
    store raw; append metadata observations; update latest caches
    link new events conservatively; record conflicts

for each linked fixture and stage [24h, 6h, 90m, lineup, 15m, 5m, closure]:
    if stage window has passed without snapshot and stage is pregame:
        mark relevant capture missed; do not backfill label
    else if due:
        obtain stage-eligible tokens, including closed tokens for closure stage
        batch unique tokens <= 500
        store/load books
        for each fixture job:
            received = requested_tokens intersect response_tokens
            succeed only if all requested tokens received
            otherwise schedule retry within the valid stage window
```

### Health-report generation

```text
report_date = local date
query configured discovery window and concrete successful slots
query monitored fixtures and latest lifecycle/component states
query unresolved mappings and open quality issues
query checkpoint/attempt states and latest runs
query raw API calls in provider reset day and allowed quota headers
query linked markets and snapshots by stage

severity = blocking if integrity/config/database rules fail
         else warning if missed/retryable/unavailable work exists
         else healthy
upsert collection_health_report
write generated Markdown atomically via temporary file + rename
return severity for process exit-code decision
```

## 8. Test plan

### Unit tests

- Four lineup stages at exact boundaries; later stages disappear after factual completion.
- T+150, six-hour 30-minute polls, +8h, +24h, and +72h boundaries.
- Canonical status mapping for every supported API-Football code plus unknown values.
- Component validators for exact starter counts, duplicate starters, wrong teams, valid final scores, participant thresholds, invalid values, and empty processed events.
- Checkpoint truth table: retryable/stopping states, legacy `skipped`, mismatched facts, maximum attempts, and priority ordering.
- Job keys change on schedule version but remain stable across restart.
- Retry classifier, `Retry-After` formats, backoff cap/jitter, quota reset timezone, and secret-safe errors.
- Pregame player matching in all five evidence tiers; equal candidates remain unresolved.

### Integration and migration tests

- Apply migrations 001–007 to an empty temporary DB.
- Copy a 006-era fixture DB with legacy `succeeded`, `incomplete`, and `skipped` checkpoints; migrate and verify no fact counts change.
- Load the same raw artifact twice: no duplicate observation rows.
- Load identical content from two retrieval artifacts: physical body remains deduplicated, but two temporal lineup/metadata observations remain available and the earliest pregame capture is preserved.
- Force a loader exception mid-batch: normalized writes and component updates roll back, raw artifact and failed attempt remain.
- Reopen after crash and prove due retry work remains.

### Required scenario tests

- **Restart/idempotency:** repeat a completed run; make no unnecessary requests. Delete or invalidate a required fact while leaving a successful checkpoint; next plan must reopen it.
- **Failure isolation:** one 500/error batch fails while another batch and Polymarket work complete.
- **Rate limit:** 429 with `Retry-After` creates `rate_limited`, persists the error artifact, makes no job disappear, and retries after the deadline.
- **Locking:** two processes start together; exactly one opens DuckDB writable and the other exits 0 as `already_running`. Test crash/stale recovery.
- **Downtime recovery:** after three days and after 21/30 days with `--catch-up-days`, request every missing date exactly once, filter monitored competitions, and recover past final facts in <=20-ID batches.
- **Rescheduling:** a changed kickoff appends schedule history, terminalizes old stage jobs, and creates new stage jobs without relabeling old snapshots.
- **Late lineup:** empty at T-50 and T-35, appears at T-20, stores two 11-player teams, suppresses T-5, and triggers the market book.
- **Missed lineup:** machine starts after kickoff; recovered lineup is usable historical data, while `pregame_lineup_capture` remains `missed`.
- **Postponed match:** no false result/component completion; a later schedule creates a new lifecycle.
- **Live/delayed match:** T+150 does not assume final; polls every 30 minutes within bounds.
- **Partial final data:** result complete but player/team/event component incomplete remains retryable independently.
- **Legitimately empty events:** successful processed empty response completes events with count zero.
- **Corrections:** +24h and +72h refreshes happen once each and can supersede current facts without deleting earlier raw evidence.
- **Player identity:** strong pregame evidence links to history; ambiguous evidence remains unresolved; post-match stats reconcile fixture aliases safely.
- **Polymarket:** hourly/15-minute discovery, final closed refresh, all snapshot stages, closed-token query, <=500 batching, missed pregame stages, and per-fixture token accounting.
- **Health:** known fixture matrix produces exact metrics; report contains no request headers, environment values, or secrets.

### Existing tests to extend, not discard

- `tests/test_collector.py`: preserve competition-scope, 20-ID batch, embedded response, and idempotency coverage while replacing obsolete two-stage expectations.
- `tests/test_validation_harness.py`: preserve immutable compressed raw storage, physical deduplication, and header redaction.
- `tests/test_model_eligibility.py`: preserve exactly three consumer flags and demonstrate independence from operational state.
- `tests/test_player_linking.py`, `tests/test_loaders.py`, and `tests/test_backfill_executor.py`: run unchanged throughout; add pregame cases without weakening historical identity or backfill rules.

### Exact validation commands

Run focused tests during each change set:

```bash
.venv/bin/python -m unittest tests/test_collector.py -v
.venv/bin/python -m unittest tests/test_collection_state.py -v
.venv/bin/python -m unittest tests/test_collector_recovery.py -v
.venv/bin/python -m unittest tests/test_http_retry.py -v
.venv/bin/python -m unittest tests/test_collector_lock.py -v
.venv/bin/python -m unittest tests/test_polymarket_collector.py -v
.venv/bin/python -m unittest tests/test_health_report.py -v
.venv/bin/python -m unittest tests/test_player_linking.py tests/test_loaders.py -v
```

Run the full suite before and after every phase:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

After true dry-run support exists, verify planning without mutation:

```bash
.venv/bin/python scripts/run_collector.py --dry-run
```

For a test copy only, record the database hash and key counts before migration, migrate the copy, then compare the hash-independent invariants. Never point an experimental migration command at the live database.

## 9. Safe execution procedure

### Change-set sequence and review gates

1. **Baseline:** create a `codex/collector-rework` branch, record `git status`, run all 53 baseline tests, and capture read-only warehouse counts. Do not copy `.env` into tests.
2. **Schema only:** implement migration 007 and migration tests. Apply it to an empty temp DB and a copied live DB. Review table schemas and confirm unrelated table counts/fingerprints are identical.
3. **Pure state logic:** implement status mapping, component validators, state transitions, and planner with fake clocks. No network integration yet. Review terminal/retryable policy.
4. **Loader temporal provenance:** add schedule and per-retrieval lineup/market observations. Run all loader, backfill, identity, eligibility, and validation tests. Review that no existing historical stable IDs or source maps are weakened.
5. **Discovery/recovery and status polling:** connect the pure planner to fake HTTP. Pass outage, reschedule, late-lineup, postponed, and correction tests.
6. **Retries and locking:** add the request executor and process lock. Pass failure, rate-limit, transaction, and two-process tests before any scheduler work.
7. **Polymarket and health:** add repeated metadata/books and report generation. Review API volume projections for a high-fixture day.
8. **Documentation/operations:** update docs and add the `launchd` example. Do not install it.
9. **Observation trial:** on a verified backup and with user approval, run read-only dry runs for several days. Compare planned calls with quota and expected fixtures.
10. **Controlled live trial:** run individual manual cycles, inspect health and raw artifacts, and confirm no out-of-scope fixtures are created. Only then request approval to install/load `launchd`.

### Live warehouse migration procedure

1. Stop/unload any scheduler and confirm no collector/backfill process holds DuckDB.
2. Copy `data/warehouse/soccer.duckdb` to a timestamped protected backup outside the live filename.
3. Compute and record SHA-256 for live and backup; verify they match.
4. Run the full test suite and migration against a separate copy first.
5. Capture live read-only counts for all tables, source-map key fingerprints, eligibility distributions, open quality rules, collector rows, and schema migration versions.
6. Apply only the reviewed additive migration in one transaction via the normal migration runner.
7. Re-run the same queries. Existing fact-table counts, source maps, eligibility totals, and quality issues must be unchanged; only new tables/columns/indexes and the migration record may differ.
8. Run the full test suite and a true read-only collector dry run.
9. If any invariant fails, stop. Do not patch raw files or run broad repair scripts. Restore the verified backup after preserving diagnostic evidence.

### Rollback strategy

- Stop scheduling first.
- Preserve every new raw artifact acquired after the backup; do not delete it.
- Restore the pre-migration DuckDB backup to a new candidate path and verify its hash before replacing the live file.
- Reconcile any post-backup provider responses through a reviewed replay after the code/database version is stable. Restoring the database does not authorize discarding raw evidence.
- Revert code/config/launchd changes separately. Never attempt a destructive SQL down migration on the live warehouse.

### Tasks safe for a lower-cost coding agent

The following are bounded and testable enough to delegate one at a time:

- additive migration definitions and empty/temp-copy migration tests;
- pure datetime stage generation and discovery-window logic;
- pure status mapping after the mapping table is approved;
- deterministic component validation SQL with supplied acceptance fixtures;
- fake-HTTP retry and cadence tests;
- CLI argument/config validation;
- Markdown health rendering from a frozen metrics dictionary;
- `launchd` example and documentation, without installation;
- mechanical README/TODO updates after behavior is verified.

The following require a stronger reviewer or explicit user approval before execution:

- applying any migration or repair to the live warehouse;
- changing player identity thresholds, canonical IDs, or existing mappings;
- rewriting existing lineup snapshots or inferring historical capture times;
- changing monitored competitions, quota/reserve policy, or provider status terminal rules;
- changing Polymarket fixture links already stored;
- classifying missing provider data as permanently unavailable;
- installing/enabling the OS scheduler;
- deleting checkpoints, raw artifacts, backups, or historical repair evidence.

Completion means all acceptance scenarios pass, the full historical suite still passes, a copied-warehouse migration preserves all unrelated invariants, dry run is genuinely read-only, live API volume fits reserve policy, and the scheduler remains an explicit user-approved deployment step.
