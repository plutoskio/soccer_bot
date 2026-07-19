# Railway Application Deployment

## Decision

The custom product is deployable on Railway as two new services alongside the
existing collector:

1. `soccer_bot` remains the only collector and the only writer to its `/app/data`
   volume. After each healthy run it also becomes the sole authorized snapshot
   producer; its existing cron and restart policy remain run-once.
2. `soccer-bot-api` is a private FastAPI service. It reads a validated immutable
   prediction snapshot from Railway object storage and never opens DuckDB.
3. `soccer-bot-web` is the public Next.js service. Browser requests reach this
   service; server-side rendering calls `soccer-bot-api` over Railway private
   networking.

This is an intentional security and consistency boundary. The web tier cannot
lock, corrupt, or query around the point-in-time rules in the warehouse.

## Data flow

```text
collector volume -> leakage-safe inference command -> validated latest.json
                                                       |
                                                       v
                                             Railway object storage
                                                       |
                                                       v
browser -> public Next.js -> private FastAPI -> validated snapshot only
```

Publishing one object is atomic from the reader's perspective. The API retains
the last valid in-memory copy during a transient storage error, but a cold API
start fails closed if storage is unavailable or the JSON contract is invalid.

## Service configuration

Create both application services from the same connected GitHub repository.
Configure their Railway root directories and config file paths separately:

- API: repository root with config `/railway.api.json`
- Web: root directory `/apps/web` with config `/apps/web/railway.json`

Do not point either at `/railway.json`; that file is exclusively the production
collector cron definition.

### API variables

Create a Railway bucket, then map its S3-compatible credentials using Railway
reference variables. The API expects:

```dotenv
SOCCER_SNAPSHOT_S3_BUCKET=...
SOCCER_SNAPSHOT_S3_KEY=regulation_champion_v1/latest.json
SOCCER_PLATFORM_SNAPSHOT_S3_KEY=specialized_platform_v1/latest.json
SOCCER_SNAPSHOT_S3_ENDPOINT=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=auto
SOCCER_SNAPSHOT_CACHE_SECONDS=30
SOCCER_SNAPSHOT_STALE_SECONDS=21600
```

Use the exact bucket-provided region if it differs from `auto`. Do not commit
credentials. The API should have no public Railway domain. Railway uses
`/health` for process liveness; internal diagnostics use `/ready`, which also
loads and validates the current snapshot.

### Collector publication variables

Map the same private bucket into `soccer_bot` using Railway reference variables:

```dotenv
SOCCER_SNAPSHOT_S3_BUCKET=${{soccer-bot-predictions.BUCKET}}
SOCCER_SNAPSHOT_S3_KEY=regulation_champion_v1/latest.json
SOCCER_PLATFORM_SNAPSHOT_S3_KEY=specialized_platform_v1/latest.json
SOCCER_SNAPSHOT_S3_ENDPOINT=${{soccer-bot-predictions.ENDPOINT}}
AWS_ACCESS_KEY_ID=${{soccer-bot-predictions.ACCESS_KEY_ID}}
AWS_SECRET_ACCESS_KEY=${{soccer-bot-predictions.SECRET_ACCESS_KEY}}
AWS_DEFAULT_REGION=${{soccer-bot-predictions.REGION}}
```

Do not resolve or print the referenced values. Publication configuration,
including the frozen logical model hash and committed production artifact path,
lives in `config/collector.json`.

### Web variables

Set the API's Railway private-network URL, including the port exposed by the API
service:

```dotenv
SOCCER_API_URL=http://${{soccer-bot-api.RAILWAY_PRIVATE_DOMAIN}}:${{soccer-bot-api.PORT}}
```

Use the actual Railway service name and internal port. Only the web service
needs a public domain. `SOCCER_API_URL` is read server-side and is not shipped to
the browser.

The API follows Railway's Uvicorn recommendation and passes an empty host so it
binds across available address families. The Node service binds to `::`. This
preserves private connectivity in legacy IPv6-only environments and remains
compatible with dual-stack environments.

## Snapshot publication

Generate the snapshot using the already-frozen champion:

```bash
.venv/bin/python scripts/predict_upcoming_regulation.py
```

Validate without an upload:

```bash
.venv/bin/python scripts/publish_prediction_snapshot.py --dry-run
```

After reviewing the printed model version, cutoff, row count, key, and hash,
remove `--dry-run` to upload. This command changes the application-facing
object, so it should be run only from the authorized prediction producer with
the correct bucket variables.

Snapshot generation and publication are implemented as a guarded
post-collection stage. `run_collector.py` closes DuckDB before inference but
retains the collector lock until publication finishes. Blocking collector
health skips publication. Generation or upload failure is isolated from the
collector result and never authorizes an invalid candidate. The producer
uploads only after independent provenance/time checks; the storage command
performs the complete serving-contract validation, replaces one object, reads
it back byte-for-byte, and validates it again before reporting success.

The deployed model is the committed, frozen
`artifacts/production/regulation_champion_v1/model.json`; its logical hash is
checked before every generation. This avoids depending on ignored local model
files inside a Railway build.

## Safe rollout

1. Create the bucket and upload one reviewed snapshot.
2. Deploy the API with no public domain; confirm `/health` reports process
   liveness and `/ready` reports the intended model version and nonzero fixture
   count.
3. Deploy the web service with its private API reference and add the public
   domain only after the error, empty, desktop, and mobile states pass review.
4. Stop the collector schedule, preserve a verified backup, deploy the producer,
   verify a candidate against the live warehouse, then restore the cron.
5. Do not run `railway up` from this checkout as an exploratory test. Connected
   GitHub deployments should use the service-specific config paths above.

The deployment is not a claim that probabilities are live trading advice. The
first release remains read-only research software and prices regulation 1X2
only.

## Production deployment record — 2026-07-15

The first controlled production rollout is complete in Railway project
`renewed-caring`, environment `production`:

- collector: the pre-existing `soccer_bot` cron service is unchanged and
  remains the only process with its persistent `/app/data` volume;
- object storage: bucket `soccer-bot-predictions-w2ax1h` in Amsterdam contains
  the reviewed object `regulation_champion_v1/latest.json`;
- snapshot: model `regulation_champion_v1`, as-of `2026-07-15T00:30:00Z`, 10
  horizon rows across six fixtures, SHA-256
  `8baf823d9ff708487d6b90d838635b05ca633fc18e2c2090e66d664db0f157ff`;
- API: `soccer-bot-api` is deployed in EU West with no public domain. `/health`
  passed Railway's liveness probe and internal `/ready` returned the expected
  model and six fixtures;
- web: `soccer-bot-web` is deployed in EU West at
  <https://soccer-bot-web-production.up.railway.app>;
- browser verification: fixture selection, horizon selection, probability
  rendering, stale-state disclosure, desktop layout, and mobile layout passed;
  the browser reported no console or page errors and no horizontal overflow.

The 2026-07-15 data-sufficiency extension was deployed as API deployment
`cab2dc30-b25a-4a93-b159-e0fb1c0b90f6` and web deployment
`922502ab-3843-45dc-8e74-e0d7c3b6a58f`. The live application now shows, for
the selected horizon, its global training-fixture count, each team's pre-cutoff
result history, least-covered xG/shots history, and the corresponding frozen
recipe thresholds. This metadata is published as snapshot contract v2; the
prediction-row hash remained unchanged. Snapshot age is
computed from `as_of`, so republishing old evidence cannot create false
freshness.

The initial reviewed snapshot remains the rollback reference. Stale data stays
visible with an explicit warning; an invalid or unavailable snapshot fails
closed. The two application services were deployed from the reviewed local
checkout. Their source is now intentionally committed and pushed, but the two
services remain manual Railway deployments and automatic application
redeployment is not enabled.

## Guarded publisher rollout — 2026-07-15

Automatic publication was first activated in collector deployment
`c314a7c9-53c7-4541-9b90-1c1e136ff268`; source-linked deployment
`6251e139-5b6f-4910-9dba-472a634d71bd` was subsequently runtime-verified.

- The schedule was replaced temporarily by `sleep infinity`; the tracked
  `railway.json` was restored locally immediately after that deployment.
- The stopped production DuckDB was 2,889,363,456 bytes. A compressed local
  backup is retained at
  `data/backups/production/soccer-20260715T200224Z.duckdb.gz`; gzip validation
  passed and its decompressed SHA-256 exactly matched production:
  `36269c7b4fcb79aeef001fe626c5be9a337ba4df981035a022192c92fc1ea760`.
- Railway Pro was enabled and the volume was resized online from 5 GB to 10 GB
  on 2026-07-15. Railway reported 3,996.7 MB used and status `Ready` after the
  resize. The operation created a 3.91 GB manual restore point named `Online
  resize to 10000MB`; the UI offers `Restore` and delete but no separate lock
  toggle. Native daily backups are enabled with six-day retention.
- A read-only live query found England–Argentina, fixture
  `13011bbe-0327-5da4-a615-e0cbadd6f06a`, scheduled for 19:00 UTC. Pre-kickoff
  producer validation emitted its T−24 row correctly. Since controlled rollout
  completed after kickoff, the current publisher correctly omitted it from the
  upcoming set rather than publishing a backdated fixture.
- The first scheduled run exited cleanly with warning-only collector health,
  `blocking_reason: null`, and `prediction_publication.status: uploaded`.
- The published snapshot is as-of `2026-07-15T20:27:40.917313Z`, with 14 rows
  across 13 fixtures and prediction-row SHA-256
  `c1ab3ee68196b729fa46fad839f7c8495351343ca5670cbddf8e48dd2ea736cb`.
- Public browser QA confirmed the fresh 22:27 Luxembourg timestamp, all 13
  fixtures, no stale banner, and no runtime errors.

After the source was committed and pushed, collector deployment
`6251e139-5b6f-4910-9dba-472a634d71bd` reached `SUCCESS` on exact commit
`e2c756cb802835e882216521d2f2f6f6f8b4cea8`. Its 2026-07-15 21:00 UTC run
published 14 rows across 13 fixtures with the same reviewed row hash and no
blocking health condition.

Rollback is code-only unless the warehouse itself is changed: redeploy the
previous collector revision or set `prediction_publication.enabled` to `false`.
The application continues serving the last validated object during a producer
failure.

## Guarded Polymarket and player-shadow rollout — 2026-07-18

The market-evidence schema and confirmed-lineup player shadow were released by
the same stopped-writer protocol used for the original publisher rollout.

- Commit `c3616bb5bdda94ff89caa025a68eac7045866ae5` temporarily replaced the
  collector command with `sleep infinity` and removed the cron schedule.
  Maintenance deployment `fcb2edf2-2e83-436b-80da-8d11f457d3dd` reached
  `SUCCESS`; `/proc` contained only PID 1 `sleep infinity` before backup.
- Railway created a fresh manual restore point named
  `2026-07-18 13:44 UTC`, size 5.92 GB, with `Restore` available. Existing
  daily and resize restore points were left untouched.
- The two reviewed feature commits were rebased on the maintenance commit and
  deployed as exact source commit
  `389c833781c76924337079fa691eb08c14e200cd`. Deployment
  `1d134d46-1a2f-45b1-90cb-22793f476fc2` reached `SUCCESS` with no cron and
  `sleep infinity` still effective.
- The production artifact, player configuration, and migration file matched
  their local SHA-256 digests byte for byte. The complete 264-test suite,
  collector dry-run, and `git diff --check` passed on the exact rebased tree.
- Read-only inspection found only migration
  `014_polymarket_market_evidence` pending. It was applied transactionally at
  `2026-07-18T13:48:04.011503Z`; immediate read-only verification found both
  mapping tables, every required order-book column, zero synthetic mapping
  rows, and no remaining migration.
- One supervised collector run exited zero. Its durable publication receipt is
  as-of `2026-07-18T13:53:34.492668Z`: 16 champion rows across nine fixtures,
  prediction-row SHA-256
  `e8d6ed7abd0089860fd18f96431f08cc9c3fcf092899d21739298a43edb99a84`,
  and 16 coherent score-grid shadow rows.
- The player shadow matched logical model SHA-256
  `bca9a13af829032b43de9e7cbbd94e070f36fcfbda76675972565748b8e8963a`
  and configuration SHA-256
  `1fa75dd3f847d5c863aabab9ffd59068d79ec3912380363368c00dc2d652e36f`.
  It returned the healthy cold-start state `no_eligible_confirmed_lineups`,
  wrote no player prediction, and remained unauthorized to replace the
  champion.
- The Polymarket layer wrote 2,194 contract mappings, 1,040 canonical outcome
  mappings, and retained 248 complete timing-valid T−5 books. It performed no
  order or trading action and wrote no outcome/performance field.
- Operations ended warning-only because the already-frozen T−24h/T−72h rows
  did not have historical pre-cutoff books. `should_fail_run` was false, volume
  use was 59.519%, and no critical alert was active.

The tracked final configuration restores `python scripts/run_collector.py`,
cron `*/5 * * * *`, and restart policy `NEVER`. Release is complete only after
Railway reports those exact effective values and a post-restore automatic cycle
publishes a fresh receipt; the maintenance and backup identifiers above remain
the rollback audit trail.
