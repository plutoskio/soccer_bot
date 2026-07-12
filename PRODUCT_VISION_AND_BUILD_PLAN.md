# Soccer Bot — Product Vision and Build Plan

Audience: this document assumes working knowledge of supervised learning,
classification and count models, feature engineering, calibration, temporal
validation, and standard probability metrics. It focuses on product and system
decisions rather than introducing basic data-science terminology.

## 1. Product vision

Soccer Bot should become a local, interactive research application for upcoming
soccer matches and Polymarket markets.

The intended workflow is:

1. The collector runs in the background and keeps current data up to date.
2. The user opens a local website.
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
Collector -> DuckDB warehouse -> Training/evaluation -> Local application
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

### 2.4 Local application

The first application should be a local Streamlit website. Selecting a fixture
or player performs inference with an existing production model; it does not
retrain the estimator.

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

### 6.1 Regulation result

Three-class home/draw/away model based primarily on point-in-time team strength,
form, opponent adjustment, rest, competition, and home advantage. This is the
first vertical slice because it establishes the shared dataset, evaluation,
registry, inference, and UI interfaces.

### 6.2 Joint score distribution

Home/away count model supporting coherent exact score, totals, goal difference,
spreads, and both-teams-to-score probabilities. Candidate formulations include
Poisson/Dixon–Coles, negative-binomial, bivariate count models, and constrained
boosted alternatives.

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

The local website should not hold a long-lived connection to the writable live
warehouse.

The recommended initial design is:

1. The collector remains the only warehouse writer.
2. A preparation command safely reads current warehouse state.
3. It applies the production feature and inference pipeline.
4. It atomically replaces a small application snapshot containing upcoming
   fixtures, predictions, diagnostics, and timestamps.
5. Streamlit reads the snapshot and model cards rather than the live writer.

This design must be tested during lock contention and partial snapshot failure.
The exact snapshot format should be chosen during implementation.

## 9. Sequential build plan

The phases are dependency ordered. Phase 0 is operational monitoring and can
run in parallel with the offline work in Phases 1–4.

### Phase 0 — Observe and schedule the collector

1. Run several real cycles and inspect health reports, retries, quota use,
   fixture scope, player identities, and market snapshots.
2. Resolve systemic warnings and document controlled warnings.
3. Enable the tracked Railway cron job as the only production scheduler.
4. Verify restart recovery, persistent health reports, backups, and
   temporary-provider-failure behavior.

Exit: unattended collection produces healthy or explicitly understood warning
reports without scope or integrity regressions.

### Phase 1 — Freeze the first prediction task

Define T-24h regulation home/draw/away:

```text
y in {home_win, draw, away_win}
```

Specify settlement, population, eligibility, competitions, seasons, prediction
timestamp, exclusions, and evaluation calendar. Implement and test target
construction independently of modeling.

Exit: target rows are deterministic and reviewed.

### Phase 2 — Build the temporal dataset and feature layer

Implement reusable point-in-time feature construction with as-of joins. Initial
features include dynamic team strength, rolling/weighted form, goals for and
against, home/away effects, opponent adjustment, rest/congestion, and
competition/season context.

Produce a deterministic Parquet dataset, manifest, coverage report, and leakage
tests.

Exit: the same warehouse/code revision produces the same dataset hash, and
future-row perturbations cannot change past features.

### Phase 3 — Build the evaluation harness and baselines

Implement rolling-origin folds, a frozen final test period, fold-local tuning,
calibration evaluation, bootstrap intervals, and stratified diagnostics.

Baselines include class priors, an Elo/team-strength model, and temporally valid
market probabilities where coverage permits.

Exit: one command evaluates any compatible estimator and generates a versioned
model card.

### Phase 4 — Select and refit the first production model

Compare a bounded candidate set: multinomial logistic regression, a
Poisson/Dixon–Coles-derived classifier, and one controlled gradient-boosted tree
model. Select using development folds, evaluate once on the frozen test period,
then refit the chosen recipe on all eligible history.

Fit calibration using leakage-safe out-of-fold predictions. Save production,
preprocessing, schema, evaluation, manifest, and hash artifacts.

Exit: a fixed feature vector produces reproducible probabilities from the saved
production artifact.

### Phase 5 — Build upcoming-fixture inference and app snapshots

Reuse the historical feature definitions for current fixtures, validate schema
compatibility, produce predictions and diagnostic metadata, and atomically
write the application snapshot.

Exit: one read-only command prepares all supported upcoming fixtures without
modifying or locking the warehouse long-term.

### Phase 6 — Build the Streamlit vertical slice

Implement fixture selection, home/draw/away probabilities, data/model versions,
training coverage, held-out metrics, calibration plots, applicability warnings,
lineup status, and a match-result Polymarket comparison when the link and price
are valid.

Exit: a real upcoming fixture can be selected and every displayed value traces
to a snapshot, model version, and evaluation record.

### Phase 7 — Add joint-score and derived match markets

Develop and validate the joint home/away score distribution. Add exact score,
totals, goal difference/spreads, and both-teams-to-score. Add first-team-to-score
only if the chosen model includes a justified timing component.

After selection, refit each finalized recipe on all eligible history and add it
to the common registry/UI.

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

### Phase 10 — Expand market evaluation and automate operations

1. Map only semantically identical, high-confidence markets.
2. Backtest model-market deltas from snapshots available at each historical
   decision time.
3. Account for bid/ask, fees, depth, liquidity, and staleness.
4. Add model promotion/rollback, retention, stale-model alerts, and secret-safe
   logging.
5. Add manual retraining/promotion commands.
6. Schedule retraining only after repeated manual runs are reproducible.

Exit: versions can be trained, evaluated, promoted, served, and rolled back
reproducibly. Automated trading remains out of scope.

## 10. Proposed project layout

```text
app.py                         Local Streamlit entry point
config/models/                 Versioned task and model settings
scripts/build_datasets.py      Point-in-time dataset construction
scripts/train_models.py        Evaluation and all-data production refit
scripts/build_app_snapshot.py  Current features and predictions for the UI
src/soccer_bot/datasets/       Targets, as-of joins, and feature builders
src/soccer_bot/modeling/       Folds, estimators, calibration, metrics, registry
src/soccer_bot/prediction/     Artifact loading and current-match inference
src/soccer_bot/app/            Streamlit pages and presentation logic
data/features/                 Generated ignored datasets/manifests
models/                        Generated ignored production artifacts
reports/models/                Model cards and generated diagnostics
```

Create the structure incrementally. The first result-model vertical slice
should establish shared interfaces before additional model families are added.

## 11. First-release acceptance criteria

The first meaningful release is complete when:

1. The collector runs reliably.
2. An upcoming monitored fixture appears in the local website.
3. The UI shows regulation home/draw/away probabilities.
4. The production model was refit on all currently eligible historical data.
5. Displayed performance comes from chronological unseen matches.
6. Predictions expose data time, model version, sample/coverage information,
   applicability, and warnings.
7. Dataset and model manifests make the result reproducible.
8. Tests prove future information cannot enter historical feature rows.
9. Missing, stale, or unsupported inputs return a typed explanation rather than
   an invented probability.

After this vertical slice, extend the same infrastructure through score,
player, corner, and broader market models in the phase order above.

## 12. Immediate next action

Create and review the Phase 1 task specification for T-24h regulation
home/draw/away. Then implement its point-in-time dataset. This establishes the
target, temporal semantics, manifests, and leakage tests that every later model
family will reuse.
