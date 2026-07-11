# Daily Collection Rework Specification

Status: implemented through migration 013; unattended scheduling remains intentionally uninstalled pending observation
Scope: upcoming fixtures, confirmed lineups, post-match data, recovery after downtime, and Polymarket snapshots

Implementation reference: `COLLECTOR_IMPLEMENTATION_PLAN.md`, migrations
007–013, `src/soccer_bot/collection_planner.py`,
`src/soccer_bot/collection_state.py`, `src/soccer_bot/request_executor.py`,
`src/soccer_bot/locking.py`, and `src/soccer_bot/health.py`. The “Current
implementation” section below records the baseline when this specification was
written; the required-behavior sections remain the acceptance contract.

## 1. Current implementation

The repository already has a restart-safe run-once collector in `scripts/run_collector.py` and `src/soccer_bot/collector.py`. It currently:

- discovers the current day's monitored API-Football fixtures once;
- checks confirmed lineups at kickoff minus 50 minutes and retries at minus 35 minutes;
- checks post-match details at kickoff plus 150 minutes and retries at plus 1,590 minutes;
- loads results, lineups, events, team statistics, and player statistics from embedded fixture responses;
- discovers Polymarket soccer events once per match day;
- links Polymarket events to fixtures;
- captures order books after lineups and near kickoff;
- batches up to 20 API-Football fixture IDs;
- uses DuckDB checkpoints to avoid repeating completed work.

This is a working foundation, but it is not yet a reliable unattended daily collection service.

## 2. Required behavior

The collector must be driven by missing database facts, not only by whether a request was previously attempted. A fixture is complete only when its required components exist and pass validation. Checkpoints record attempts and scheduling state; they do not override actual database completeness.

The target lifecycle is:

```text
Discovered
    -> Scheduled
    -> Pregame monitoring
    -> Confirmed lineup captured
    -> Kickoff passed
    -> Waiting for final status
    -> Post-match components collected
    -> Validated
    -> Correction refresh completed
    -> Terminal
```

Exceptional terminal states must include:

```text
postponed
cancelled
abandoned
administrative result
data unavailable
pregame snapshot missed
```

## 3. Rolling fixture discovery and downtime recovery

Every invocation should verify discovery coverage across configurable windows:

```text
Past recovery window:  today - 14 days
Future planning window: today + 7 days
```

On startup, the collector should:

1. Read successful fixture-discovery checkpoints.
2. Find dates inside the recovery window that were never discovered.
3. Query API-Football once for each missing date.
4. Store all monitored fixtures.
5. Examine every discovered past fixture for missing final data.
6. Batch missing detail requests in groups of at most 20 fixtures.
7. Mark pregame-only information as missed where it cannot be reconstructed.

For outages beyond the normal safety window, support an explicit command such as:

```bash
.venv/bin/python scripts/run_collector.py --catch-up-days 30
```

A lineup retrieved after the match remains useful historical lineup data, but it must never be represented as information captured before kickoff. The same restriction applies to market prices.

## 4. Fixture schedule refreshes

Upcoming fixtures can be rescheduled, postponed, or cancelled. Discovery should run:

- daily for the next seven days;
- every six hours for today and tomorrow;
- again near kickoff;
- after a postponement or cancellation signal.

Add a schedule-observation table rather than silently losing prior schedules:

```text
fixture_schedule_observation
- fixture_id
- provider_status
- scheduled_kickoff
- retrieved_at
- raw_artifact_id
```

This preserves the kickoff time known when a lineup or market snapshot was captured.

## 5. Adaptive confirmed-lineup collection

Use the following initial attempt schedule:

```text
T-50 minutes
T-35 minutes
T-20 minutes
T-5 minutes
```

At each stage:

1. Batch all due fixtures.
2. Load and preserve the raw response.
3. Require two teams with exactly 11 distinct starters each.
4. Stop polling immediately after a complete lineup is stored.
5. Record whether the lineup was retrieved before the kickoff known at retrieval time.

Formation and bench completeness are useful but should not block a starting lineup from being usable.

If the machine was unavailable before kickoff, distinguish:

```text
lineup data: available
pregame lineup capture: missed
```

## 6. Pregame player identity

The current historical identity linker works best when player-stat records and lineups occur in the same post-match response. Pregame responses normally do not yet contain player statistics, so pregame lineup aliases may fail to connect to canonical players and their historical records.

Pregame identity resolution should use, in order:

1. Exact API player `(provider ID, normalized name)` match.
2. Same provider ID with a compatible name.
3. A unique compatible player who recently appeared for that team.
4. Compatible name, matching shirt number, and team context.
5. Otherwise leave the alias unresolved.

Ambiguous players must never be guessed. After post-match player statistics arrive, revisit unresolved aliases for the fixture and safely reconcile both pregame and post-match lineup snapshots.

## 7. Status-aware post-match collection

Kickoff plus 150 minutes should be the first status check, not an assumption that the match has finished.

Suggested behavior:

```text
Initial check: T+150 minutes
While still live/delayed: every 30 minutes, up to 6 hours
Incomplete final-data retry: +8 hours
Correction refresh: +24 hours
Final correction refresh: +72 hours
```

The collector must explicitly handle final, postponed, cancelled, abandoned, suspended, and administrative-result statuses.

## 8. Component-level completion

Track each component independently:

| Component | Completion rule |
|---|---|
| Result | Final status and regulation score |
| Lineups | Two teams with 11 distinct starters |
| Team statistics | Two valid team blocks, or explicitly unavailable |
| Player statistics | Unique players, valid teams and values, and a reasonable participant count |
| Events | Response successfully processed, including a legitimately empty response |
| Identity linking | Measured separately; unresolved links produce warnings |
| Correction refresh | Required final refresh completed |

Supported component states should include:

```text
pending
retryable
complete
unavailable
missed
invalid
terminal
```

Missing passing data remains a warning. Invalid identities, scores, starter counts, or team assignments remain blocking.

## 9. Checkpoint semantics

Extend `collection_checkpoint` with fields equivalent to:

```text
next_attempt_at
maximum_attempts
priority
terminal_reason
last_run_id
```

Only the following states stop future work:

```text
succeeded
terminal
skipped_with_reason
```

The following remain eligible for retry:

```text
pending
incomplete
failed
rate_limited
```

The planner must compare checkpoint state with actual database completeness on every run.

## 10. Failure handling and locking

Add bounded request retry behavior:

- retry temporary network failures;
- honor `Retry-After`;
- retry HTTP 429 and selected 5xx responses;
- do not retry permanent 4xx responses;
- record every failed attempt;
- continue processing unrelated batches;
- fail the entire run only for configuration, database, or system-wide failures.

Continue storing raw error responses.

Add a collector lock so two scheduler invocations cannot operate concurrently. A second invocation should exit cleanly while an active lock exists. Stale locks must expire after a configured timeout.

## 11. Polymarket collection schedule

Event discovery must not become permanently complete after one request per day. Suggested discovery cadence:

```text
Within seven days: every 60 minutes
On match day: every 15 minutes
After closure: one final metadata refresh
```

Suggested initial order-book snapshots:

```text
T-24h
T-6h
T-90m
Immediately after confirmed lineup
T-15m
T-5m
After closure
```

This provides useful price movement without requiring high-frequency collection.

## 12. Daily health report

Generate a daily report containing:

- dates successfully discovered;
- expected monitored fixtures;
- fixtures with pregame lineups captured;
- fixtures whose pregame capture was missed;
- final results collected;
- missing team, player, lineup, or event components;
- unresolved player identities;
- failed and retryable jobs;
- API calls used and remaining reserve;
- linked Polymarket fixtures and snapshot counts.

The collector should exit nonzero only for blocking problems.

## 13. Operating-system schedule

Provide a macOS `launchd` configuration that invokes the run-once collector every five minutes. Internal planning decides whether any API work is due.

If the Mac is asleep, post-match data can be recovered later. Pregame lineups and contemporaneous Polymarket prices cannot be recreated after the fact.

## 14. Existing behavior to preserve

- immutable compressed raw artifacts;
- content hashing and physical deduplication;
- DuckDB as the canonical warehouse;
- stable internal fixture, team, and player identities;
- batches of at most 20 API-Football fixtures;
- monitored-competition filtering;
- the validated historical backfill pipeline;
- conservative player matching;
- separate regulation, extra-time, and penalty scores;
- warnings rather than fabricated values for missing optional data.

## 15. Recommended implementation order

1. Rolling discovery and downtime catch-up.
2. Durable retryable checkpoint states.
3. Strict component-level completion.
4. Four-stage lineup polling.
5. Pregame player identity resolution and post-match reconciliation.
6. Status-aware post-match retries and correction refreshes.
7. Collector lock and HTTP backoff.
8. Daily health report.
9. Repeated Polymarket discovery and staged snapshots.
10. `launchd` template and setup instructions.

## 16. Acceptance tests

The rework is complete only when tests demonstrate that:

- a three-day outage recovers missed fixtures and final data;
- recovered lineups are not mislabeled as pregame observations;
- rescheduled fixtures receive new jobs;
- a lineup appearing at T-20 is captured after earlier failed checks;
- postponed matches do not create false completed records;
- partial player data remains retryable;
- a 429 delays work without losing jobs;
- two collector processes cannot run simultaneously;
- repeated runs make no unnecessary requests;
- pregame lineup players connect to historical records when evidence is strong;
- ambiguous players remain unresolved;
- existing historical backfill behavior and the current test suite remain intact.
