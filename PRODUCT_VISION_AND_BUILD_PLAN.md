# Soccer Bot — Product Vision and Build Plan

Audience: this document assumes working knowledge of supervised learning,
classification and count models, feature engineering, calibration, temporal
validation, and standard probability metrics. It focuses on product and system
decisions rather than introducing basic data-science terminology.

The canonical technical modeling design, including coherent predictive
distributions, information states, feature research, model candidates,
calibration, stacking, market-aware forecasts, and promotion gates, is in
[FORECASTING_SYSTEM_DESIGN.md](FORECASTING_SYSTEM_DESIGN.md).
The reviewed user-facing market scope and implementation priorities are in
[PREDICTION_CONTRACT_CATALOG.md](PREDICTION_CONTRACT_CATALOG.md).

## 1. Product vision

Soccer Bot should become an interactive research platform for upcoming soccer
matches and prediction-market contracts. It runs locally during development
and is designed to be hosted on Railway as a custom web application.

The intended workflow is:

1. The collector runs in the background and keeps current data up to date.
2. The user opens the custom web application.
3. The user selects an upcoming fixture.
4. The user chooses a prediction type: match result, goals, spread, exact score,
   corners, or a player goal/assist proposition.
5. The application loads the relevant production model and current features.
6. It displays a probability together with model quality, training coverage,
   current-data completeness, applicability, and uncertainty.
7. If a safely linked Polymarket market exists, it compares the model
   probability with the observed market price, spread, depth, and timestamp.

An example view is:

```text
Match:                    Arsenal vs Liverpool
Kickoff:                  Today at 20:45
Lineups:                  Confirmed

Proposition:              Bukayo Saka scores at least one goal
Model probability:        31%
Market-implied probability: 25%

Relevant eligible history: 184 appearances
Current feature coverage:   High
Held-out model quality:     Model-card link and key metrics
Applicability:              Medium
Important warning:          Rare-event/player-minute uncertainty
```

This is a decision-support and model-research tool. It must not present a model
estimate or a model-market difference as a guaranteed betting edge.

## 2. System boundaries

The product has four separate parts:

```text
Collector -> DuckDB warehouse -> Training/evaluation -> Immutable snapshot
                                                        -> Read-only API -> Web
```

### 2.1 Collector

The collector obtains evidence; it does not train models. It stores:

- fixture discovery and schedule changes;
- confirmed lineups and their retrieval times;
- results, events, team statistics, and player statistics;
- player-identity reconciliation state;
- Polymarket events, markets, outcomes, and order-book snapshots.

On Railway, the collector wakes every five minutes. Internal planning determines
whether any API request is actually due, so the user's computer can remain off.
If Railway or a provider is unavailable, collection resumes from checkpoints
after service returns. It can recover provider information that still exists,
but it cannot recreate pregame lineups or prices that were never observed.

### 2.2 Warehouse

DuckDB is the canonical local evidence store. It contains historical facts,
current fixtures, provenance, collection state, and data-quality information.

Modeling starts from the relevant `fixture_model_eligibility` flag and then
applies task-specific non-null, temporal, competition, and identity rules. “Use
all data” means all eligible data for that model; it does not mean using
administrative results, unsafe identities, invalid facts, or post-cutoff
information.

### 2.3 Training and evaluation

Training is a separate operation, initially launched manually and eventually
scheduled at a low cadence such as weekly or after a material amount of new data
arrives. It should not rerun when the user clicks a fixture or player.

A training run produces:

- a deterministic dataset and manifest;
- temporal evaluation results;
- a model card;
- a production model artifact;
- preprocessing and feature-schema artifacts;
- hashes and version metadata.

### 2.4 Custom application

The first application is a custom Next.js interface backed by a private,
read-only FastAPI service. Selecting a fixture or horizon reads an existing
production snapshot; it does not retrain the estimator or query DuckDB. This
choice provides full product control and remains deployable on Railway without
coupling the user interface to Python rendering or the writer volume.

The application should eventually expose a separate administrative retraining
action, but ordinary prediction controls must remain fast and deterministic.

## 3. Training, evaluation, and all-data refitting

### 3.1 Development evaluation

Candidate comparison uses chronological data. Model selection and
hyperparameter tuning should use rolling-origin or walk-forward validation.
Tuning and calibration selection occur inside development folds. A final
chronological test period is frozen and used once after the recipe is selected.

Random train/test splitting is not appropriate as the primary evaluation
strategy because it does not reproduce the forward-looking deployment setting
and can mix later team/player states into earlier evaluation periods.

Report aggregate and sufficiently supported competition/season strata. Primary
probabilistic metrics should include:

- log loss;
- Brier score;
- ranked probability score for ordered three-way outcomes where useful;
- reliability curves and calibration error;
- deltas against transparent baselines;
- bootstrap confidence intervals;
- probability-bin, favorite/underdog, competition, season, and coverage
  diagnostics.

Accuracy and class-wise recall remain diagnostics, not the optimization target.

### 3.2 Final production refit

After selecting the feature set, estimator family, hyperparameters, and
calibration method, refit the final production estimator on all eligible
historical rows available at that time.

The production artifact therefore receives the maximum legitimate training
sample. Its displayed performance must come from the frozen temporal evaluation
of the same model recipe—not from scoring the all-data estimator on its training
rows.

For calibrated production probabilities, fit the selected calibrator from
leakage-safe out-of-fold predictions across the eligible training history, or
apply a calibration procedure frozen during development. Do not fit and assess
a calibrator on the same predictions.

Every model version stores both:

- the held-out evaluation record;
- the all-eligible-data production artifact and training manifest.

If the recipe changes after inspecting the final test period, create a new
version and rerun the evaluation process. Do not attach an old model card to a
new recipe.

## 4. Point-in-time correctness

Every training example has an explicit `prediction_at` timestamp. Inputs must
be reproducible using only information retrieved or knowable at or before that
timestamp.

Initial prediction times are:

- T-24h for an early prediction;
- a confirmed-lineup prediction after a valid pregame lineup retrieval.

Historical rolling features may use only source fixtures with kickoff before
`prediction_at`. Schedule, lineup, and market features require as-of joins using
their observation/retrieval timestamps, not simply the latest normalized row.

A lineup can enter the confirmed-lineup dataset only when it was retrieved
before the kickoff known at retrieval time. A recovered post-kickoff lineup is
useful historical evidence but not a valid pregame feature observation.

Leakage tests must perturb future fixtures and future observations and prove
that earlier feature rows and predictions remain unchanged.

## 5. Application behavior

### 5.1 Fixture selection

The opening page lists monitored upcoming fixtures with:

- competition and local kickoff time;
- home and away teams;
- fixture status;
- lineup state;
- latest-data timestamp;
- available production model families;
- linked-market availability.

### 5.2 Prediction selection

The fixture page should eventually support:

- regulation home/draw/away;
- goal difference and spreads;
- total goals and exact score;
- first team to score;
- team and match corners;
- player goals;
- player assists.

Only propositions backed by an approved, compatible model version should be
enabled.

### 5.3 Player propositions

Confirmed starters appear first. Before lineup confirmation, probable or recent
players may be displayed only with explicit participation uncertainty.

The UI must distinguish conditional propositions such as “scores given a start”
from unconditional match propositions such as “scores in this match.” The
production probability for the latter must marginalize over start and minutes
uncertainty.

### 5.4 Evidence displayed with a prediction

Do not collapse all evidence into one unexplained confidence number. Display
separate dimensions:

- **training coverage:** eligible sample size, seasons, competitions,
  missingness, and relevant player/team history;
- **current-data completeness:** freshness, lineup state, identity safety,
  required feature availability, and market-link state;
- **held-out model quality:** proper scoring rules, calibration, baseline
  comparisons, and relevant strata from the model card;
- **applicability:** distance from or support within the training distribution;
- **uncertainty:** ensemble/bootstrap interval or other justified estimate when
  available.

Warnings should identify the actual limitation: sparse player history,
unconfirmed lineup, low competition coverage, missing fields, distribution
shift, stale prices, or weak market linkage.

### 5.5 Polymarket comparison

For a semantically identical and safely linked market, display:

- model probability;
- latest market price and observation time;
- bid/ask spread and depth where available;
- liquidity and staleness;
- model-market probability delta;
- mapping confidence and any warning.

Market evaluation must account for fees, spread, depth, and selection effects.
The first application does not place trades.

## 6. Model families

These product-facing families are implemented through the smaller set of
coherent probability engines defined in
[FORECASTING_SYSTEM_DESIGN.md](FORECASTING_SYSTEM_DESIGN.md). Related contracts
should derive from a shared distribution whenever possible; direct classifiers
remain baselines or constrained challengers rather than independent sources of
contradictory probabilities.

### 6.1 Regulation result

Three-class home/draw/away model based primarily on point-in-time team strength,
form, opponent adjustment, rest, competition, and home advantage. It is a
mandatory baseline and challenger for the regulation-result view. The first
production probabilities should normally derive from the joint-score engine so
moneyline, exact-score, total, and spread outputs remain coherent.

### 6.2 Joint score distribution

Home/away count model supporting coherent exact score, totals, goal difference,
spreads, and both-teams-to-score probabilities. Candidate formulations include
Poisson/Dixon–Coles, negative-binomial, bivariate count models, and constrained
boosted alternatives. This is the first core production engine and the first
interactive match-contract vertical slice.

### 6.3 Player goals and assists

Use a two-part or joint exposure/event framework:

- start probability and minutes distribution before lineups;
- observed starter/bench state after valid lineup retrieval;
- goal/assist rate conditional on exposure, role, team attack, opponent defense,
  and player history;
- marginalization over participation uncertainty for unconditional props.

Unresolved identities remain excluded rather than globally merged by name.

### 6.4 Corners

Team and match corner counts use team-stat-eligible rows with explicit corner
non-null checks. Candidate families include Poisson, negative-binomial, and
boosted count/quantile models, with particular attention to dispersion, tails,
and competition heterogeneity.

Each family has its own dataset, task specification, evaluation, production
artifact, and eligibility requirements.

## 7. Reproducibility and artifacts

Every dataset/training run records:

- task and model version;
- prediction time and settlement definition;
- code revision and configuration;
- warehouse identity/hash;
- SQL or dataset-builder version;
- eligibility and non-null rules;
- competition/date coverage;
- included/excluded counts and reasons;
- feature schema;
- temporal folds and frozen test period;
- evaluation metrics;
- all-data refit row count and timestamp;
- calibration procedure;
- hashes of datasets, preprocessing, and model artifacts.

Generated feature tables and model artifacts stay ignored by Git. Code,
reviewed configuration, task definitions, and concise model cards belong in
Git.

## 8. Safe application access

The application must not hold a connection to the writable live warehouse.

The recommended initial design is:

1. The collector remains the only warehouse writer.
2. A preparation command safely reads current warehouse state.
3. It applies the production feature and inference pipeline.
4. It atomically replaces a small application snapshot containing upcoming
   fixtures, predictions, diagnostics, and timestamps.
5. The read-only API validates and serves the snapshot, removing filesystem
   provenance paths from its public contract.
6. Next.js fetches the API server-side; the browser never receives a private
   service address or warehouse access.

This design is implemented for the regulation-moneyline snapshot. The API
fails closed on invalid JSON, unsupported output families, malformed
probabilities, duplicate fixture/horizon rows, and unavailable cold-start
storage. On Railway, immutable snapshots pass through S3-compatible object
storage so the API and web services never mount the collector volume. See
`DESIGN.md` and `RAILWAY_APPLICATION_DEPLOYMENT.md`.

## 9. Sequential build plan

The phases are dependency ordered. Phase 0 is operational monitoring and can
run in parallel with the offline work in Phases 1–5. Detailed deliverables and
promotion gates are defined in `FORECASTING_SYSTEM_DESIGN.md`.

### Phase 0 — Observe and schedule the collector

1. Run several real cycles and inspect health reports, retries, quota use,
   fixture scope, player identities, and market snapshots.
2. Resolve systemic warnings and document controlled warnings.
3. Enable the tracked Railway cron job as the only production scheduler.
4. Verify restart recovery, persistent health reports, backups, and
   temporary-provider-failure behavior.

Exit: unattended collection produces healthy or explicitly understood warning
reports without scope or integrity regressions.

### Phase 1 — Freeze contracts and information states

Normalize the desired user propositions into a versioned contract registry.
Specify settlement period, numeric lines, pushes, voids, player-participation
conditions, eligibility, required fields, and compatible probability engine.

Define rolling pre-lineup and confirmed-lineup information states. Retain T-24h
as a comparable pre-lineup evaluation anchor, not as a claim that every hour
requires a different production model. Implement and test target/settlement
construction independently of modeling.

Exit: every enabled contract settles deterministically from canonical facts and
has an explicit point-in-time information policy.

### Phase 2 — Build the temporal state, dataset, and feature layer

Implement reusable point-in-time feature construction with as-of joins. Initial
features include dynamic attacking and defensive strength, learned recency,
opponent-adjusted performance, goals and chance proxies, home/away effects,
rest/congestion, lineup-strength state, and competition/season partial pooling.

Produce a deterministic Parquet dataset, manifest, coverage report, and leakage
tests.

Exit: the same warehouse/code revision produces the same dataset hash, and
future-row perturbations cannot change past features.

### Phase 3 — Build the evaluation harness and baselines

Implement rolling-origin folds, a frozen final test period, fold-local tuning,
calibration evaluation, bootstrap intervals, and stratified diagnostics.

Baselines include class priors, Elo/team strength, independent Poisson,
Dixon–Coles, regularized generalized linear models, and temporally valid market
probabilities where coverage permits.

Exit: one command evaluates any compatible estimator and generates a versioned
model card.

### Phase 4 — Select and refit the first joint-score engine

Build a dynamic joint home/away score distribution with Poisson and
Dixon–Coles baselines, competition/season partial pooling, and a direct
home/draw/away challenger. Research bivariate/overdispersed counts and one
controlled boosted correction only after the structural baselines are valid.

Select using development folds, evaluate once on the frozen test period, fit
calibration/stacking only from leakage-safe out-of-fold predictions, and refit
the chosen recipe on all eligible history.

Exit: one saved distribution reproducibly prices moneyline, exact score, goal
totals, team totals, both-teams-to-score, and goal-spread contracts.

### Phase 5 — Build upcoming-fixture inference and app snapshots

Reuse the historical feature definitions for current fixtures, validate schema
compatibility, produce predictions and diagnostic metadata, and atomically
write the application snapshot.

Exit: one read-only command prepares all supported upcoming fixtures without
modifying or locking the warehouse long-term.

### Phase 6 — Build the custom application vertical slice

Implement fixture selection in Next.js, backed by a private read-only FastAPI
boundary. The first completed slice shows calibrated regulation home/draw/away,
fair decimal odds, T−72/T−24 selection, expected goals, history and signal
coverage, raw-to-calibrated movement, model identity, cutoffs, and applicability
warnings. Exact-score, total, team-total, both-teams-to-score, and compatible
spread controls remain locked until distribution-level calibration is coherent.

The evidence surface also separates three concepts that must never be conflated:

- the global number of eligible fixtures used to fit the selected horizon;
- each team's prior match count available at the exact prediction cutoff;
- each team's xG and shot-history depth.

Sufficiency is reported against frozen recipe thresholds: 1,000 fixtures for a
walk-forward fit, fewer than five team matches as cold start, and 20 rich-signal
observations as full history. These labels describe data support, not guaranteed
prediction accuracy.

Exit: a real upcoming fixture can be selected and every displayed value traces
to a snapshot, model version, and evaluation record.

### Phase 7 — Add period-score markets and nonlinear corrections

Research controlled boosted residuals, bivariate/negative-binomial candidates,
and regularized stacking for the joint-score engine. Promote added complexity
only when the proper-score improvement is stable across later seasons and does
not damage calibration or coherence.

Add a coherent joint first-half/second-half distribution for pre-match period
exact score, moneyline, spread, totals, team totals, and both-teams-to-score.
The period and regulation probabilities must reconcile exactly.

### Phase 8 — Add lineup and player goal/assist models

Build point-in-time player histories, participation/minutes models, and
conditional event-rate models. Evaluate rare-event calibration, exposure bins,
competition coverage, and player-history sample-size effects. Support both
pre-lineup marginal predictions and confirmed-lineup predictions.

Refit finalized recipes on all eligible player history. Add player selection,
conditional/unconditional proposition labels, player-specific coverage, and
identity warnings to the UI.

### Phase 9 — Add corner models

Build point-in-time team-stat features and compare count/boosted models for team
and total corners. Evaluate dispersion, tails, over/under calibration, and
competition heterogeneity. Refit the selected recipes on all eligible team-stat
history and add them to the registry/UI.

### Phase 10 — Add timing, qualification, and tournament markets

Add first-team and first-player-to-score only after a no-goal-aware
competing-risk/event-time model outperforms score-derived approximations. Build
match-level qualification from explicit competition rules, aggregate state,
extra time, and penalties. Add tournament outrights only through validated
group/bracket simulation over all remaining paths.

### Phase 11 — Add market-aware fusion and automate operations

1. Map only semantically identical, high-confidence markets.
2. Preserve an independent soccer-only forecast and train a separately labeled
   market-aware forecast from out-of-fold predictions and eligible as-of prices.
3. Benchmark against de-vigged market consensus and backtest deltas from
   snapshots available at each historical decision time.
4. Account for bid/ask, fees, depth, liquidity, staleness, and mapping risk.
5. Add model promotion/rollback, retention, stale-model alerts, and secret-safe
   logging.
6. Add manual retraining/promotion commands.
7. Schedule retraining only after repeated manual runs are reproducible.

Exit: versions can be trained, evaluated, promoted, served, and rolled back
reproducibly. Automated trading remains out of scope.

## 10. Proposed project layout

```text
apps/web/                      Custom Next.js fixture-selection interface
apps/api/                      Read-only FastAPI snapshot/pricing boundary
config/contracts/              Versioned settlement and proposition definitions
config/models/                 Versioned task and model settings
scripts/build_datasets.py      Point-in-time dataset construction
scripts/train_models.py        Evaluation and all-data production refit
scripts/predict_upcoming_regulation.py  Current features and predictions
scripts/publish_prediction_snapshot.py  Validated object-storage publication
src/soccer_bot/datasets/       Targets, as-of joins, and feature builders
src/soccer_bot/modeling/       Folds, estimators, calibration, metrics, registry
src/soccer_bot/prediction/     Artifact loading and current-match inference
data/features/                 Generated ignored datasets/manifests
models/                        Generated ignored production artifacts
reports/models/                Model cards and generated diagnostics
```

Create the structure incrementally. The first joint-score vertical slice should
establish shared interfaces before player, corner, timing, and market-aware
families are added.

## 11. First-release acceptance criteria

The first meaningful release is complete when:

1. The collector runs reliably.
2. An upcoming monitored fixture appears in the custom website.
3. The UI explores calibrated regulation home/draw/away probabilities and
   identifies their current score-grid coherence limitation explicitly.
4. The production model was refit on all currently eligible historical data.
5. Displayed performance comes from chronological unseen matches.
6. Predictions expose data time, model version, sample/coverage information,
   applicability, and warnings.
7. Dataset and model manifests make the result reproducible.
8. Tests prove future information cannot enter historical feature rows.
9. Missing, stale, or unsupported inputs return a typed explanation rather than
   an invented probability.

After this vertical slice, extend the same infrastructure through player,
corner, timing, and market-aware models in the phase order above.

## 12. Immediate next action

The first regulation contract registry, deterministic settlement layer,
information-state task specification, reviewed target exclusions, and
regulation-score target builder, and chronological team-state feature builder
are implemented. The local snapshot currently yields 73,258 feature rows from
38,445 targets across clean T-72h and T-24h horizons.

The selected recipe is independent Poisson plus chronological Understat xG and
API-Football shots corrections, followed by temperature scaling. The richer
features first passed a development-only internal validation year. Their
coefficients were then refit on all development matches, temperature was fit
only on the calibration year, and the frozen final test was scored once. The
calibrated rich model improves final-test log loss over calibrated independent
Poisson by 0.00453 at T-24h and 0.00434 at clean T-72h, with both paired
month-block 95% intervals below zero.

The historical strict as-of Polymarket benchmark has zero complete eligible
three-way fixtures. Prospective T−72h/T−24h collection is now active under a
frozen pre-cutoff policy. Regulation mappings, full-depth snapshots, immutable
champion/book evidence, fee-aware ladder walks, and count-only coverage/alerts
are implemented. This is accumulation infrastructure, not evidence that an
edge exists. Football-Data closing consensus is a useful retrospective
yardstick over 12,458 fixtures, but cannot be used as an earlier-time feature
because `quoted_at` is missing. On its covered final-test subset it still beats
the champion by about 0.042 log-loss points at both horizons, establishing the
remaining performance gap.

The champion has now been refit on all eligible local history. Its model
artifact stores horizon-specific rate scales, rich-rate coefficients, and the
frozen evaluation temperatures; its manifest hashes the warehouse snapshot,
feature definitions, task/contract files, selection report, feature rows, rich
rows, and logical model. Upcoming inference replays the historical state engine
and emits only due horizons whose current kickoff was already known at the exact
cutoff. The first reproducible snapshot emitted 10 rows across six fixtures.
See `REGULATION_CHAMPION_MODEL.md` for the full handoff.

The immutable snapshot is now connected to a custom fixture-selection UI
through a fail-closed read-only API. The first vertical slice exposes only the
supported calibrated regulation moneyline, both prediction horizons, fair
decimal odds, evidence coverage, calibration movement, model identity, and
warnings. Desktop, mobile, interaction, reduced-motion, and API-unavailable
states have been browser-tested. Separate Railway service definitions preserve
the existing collector and writer volume.

The reviewed snapshot, private API, and public web service are deployed on
Railway. The public fixture-selection application is
<https://soccer-bot-web-production.up.railway.app>. The sole collector now
performs guarded post-run publication after closing DuckDB while retaining its
lock. The live application consumed the first automatic snapshot successfully.
See `RAILWAY_APPLICATION_DEPLOYMENT.md` for the verified topology, backup, and
rollout record.

The production evidence surface now exposes data sufficiency directly. T−24
shows 38,445 global training fixtures and clean T−72 shows 34,813, while every
selected matchup separately reports home/away prior fixtures and rich-signal
history. Pass/below-threshold labels come from the frozen recipe and explicitly
state when a large global sample cannot compensate for sparse team history.

The application/producer changes are committed and pushed. Operationally, the
collector volume has been resized from 5 GB to 10 GB, the resize-created 3.91 GB
manual restore point is retained, daily native backups are enabled with six-day
retention, and volume-usage alerts are active.

### Active roadmap after the first production moneyline model

Work in the following order. A later product surface must not be presented as
actionable until its underlying probability layer has passed leakage-safe
forward evaluation.

1. **Finish production protection.** Add alerts for failed prediction
   publication and excessive snapshot age. Restore a production backup into an
   isolated Railway volume and validate the warehouse, immutable raw evidence,
   staged state, reports, and recovery procedure without touching the live
   writer volume.
2. **Open a new evaluation period.** Freeze the current champion and its opened
   final-test report. Define a new forward window, or nested walk-forward
   development folds, before choosing any new feature or model. The old final
   test remains a historical audit and must not influence challenger selection.
3. **Build point-in-time lineup and player strength.** Model expected and then
   confirmed starters, absences, recent minutes, substitutions, position,
   goals, assists, shots, and player contributions to team attack and defence.
   Every observation must respect the prediction cutoff, preserve missingness,
   and use `eligible_player_models` independently from result-model eligibility.
4. **Challenge the probability engine.** Test lineup/player features, improved
   goal-rate and score-distribution models, and distribution-level calibration
   against the frozen champion. Promotion requires better unseen-match proper
   scoring rules and calibration, stable results across time/competitions, and
   no material degradation in important subgroups.
5. **Accumulate executable market benchmarks.** Continue capturing complete,
   timestamped Polymarket books under
   `polymarket_regulation_market_evidence_v1`. T−72 and T−24 are now paired
   strictly before their exact model cutoffs with spread, depth, fee, and
   immutable provenance. Add a separately ordered confirmed-lineup protocol
   only when that prediction model exists; do not pretend a post-lineup book
   preceded a prediction made from the lineup.
6. **Unlock contracts in validated layers.** First add regulation spreads and
   totals derived from a validated score distribution. Then add exact score,
   both teams to score, and first-team-to-score contracts. Unlock player goals,
   assists, and shots only after the player model passes forward validation.
   Treat corners as a separate count-model research track with its own data,
   eligibility, calibration, and promotion gate.

The immediate quantitative priority is step 2 followed by step 3: establish the
new evaluation boundary, then research confirmed-lineup and player-strength
features inside it. Operational alerts and the isolated restoration test can
proceed in parallel because they do not use or influence model evaluation.
