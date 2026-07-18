# Soccer Bot — Sharp Probability Forecasting System Design

Status: canonical technical modeling direction
Audience: quantitative researchers, data scientists, and implementation agents
Scope: pre-match soccer probability estimation, contract pricing, evaluation,
and production inference

This document defines how Soccer Bot should turn its canonical evidence
warehouse into the sharpest defensible pre-match probabilities it can produce.
It complements `PRODUCT_VISION_AND_BUILD_PLAN.md`, which defines the product,
`DATA_ARCHITECTURE.md`, which defines the evidence foundation, and
`PREDICTION_CONTRACT_CATALOG.md`, which records the reviewed contract scope and
priority.

The objective is not to produce confident picks, maximize historical hit rate,
or fit one classifier per betting market. The objective is to estimate coherent,
well-calibrated predictive distributions that outperform transparent baselines
on later unseen matches and, where evidence supports it, add information beyond
market prices after accounting for spread, fees, liquidity, and uncertainty.

An apparent edge remains a research hypothesis until it survives point-in-time
evaluation and forward paper trading. No model family, feature, or complexity
level receives privileged status without out-of-sample evidence.

## 1. Quantitative mandate

For an information set `I_t` available at time `t`, estimate:

```text
P(future match and player outcomes | I_t)
```

The system should maximize predictive sharpness subject to calibration. In
practical terms:

- probabilities must correspond to observed long-run frequencies;
- useful information should move probabilities away from uninformative priors;
- related probabilities must be mathematically coherent;
- uncertainty must widen when history, identity, or current inputs are weak;
- no observation learned after the declared information cutoff may enter a
  historical feature row;
- every improvement must survive chronological out-of-sample comparison;
- market prices are a demanding benchmark, not proof of truth and not a label;
- economic value is measured at executable prices, not headline probabilities.

The primary research target is probability quality, not classification
accuracy. Accuracy discards the difference between 34% and 90%, while the
platform and any market comparison depend precisely on that difference.

## 2. Product interpretation

Soccer Bot is a fixture-centered probability platform. A user selects a match,
then explores compatible propositions:

```text
Fixture
├── Match outcome
│   ├── Home / draw / away
│   ├── Double chance and draw no bet
│   └── Handicaps and winning margins
├── Goals
│   ├── Exact score
│   ├── Match and team totals
│   ├── Both teams to score
│   └── First team to score
├── Players
│   ├── Goals
│   ├── Assists
│   ├── Participation and expected minutes
│   └── Conditional and unconditional propositions
└── Corners
    ├── Match total
    ├── Team totals
    └── Corner handicaps
```

The interface should not invoke an unrelated model for every click. It should
query a small set of versioned predictive distributions and transform those
distributions into compatible contract probabilities.

## 3. Governing research principles

### 3.1 Forecast distributions, not picks

A prediction is a probability distribution with a declared information state,
not a categorical winner. The system may show the most likely outcome, but
training, selection, calibration, and promotion use proper probabilistic scores.

### 3.2 Build coherence into the architecture

Moneyline, totals, exact score, both-teams-to-score, and goal spreads are
different views of the same match score. Whenever possible, they must derive
from one joint home/away goal distribution.

Player goal probabilities should reconcile with team scoring intensity rather
than independently implying an impossible expected number of goals. Corner
contracts should similarly derive from a joint home/away corner distribution.

Direct contract-specific models remain valuable challengers. If a direct model
reliably improves one target, it can enter a constrained ensemble, but it must
not silently make the platform internally contradictory.

### 3.3 Treat time and retrieval as model inputs

Every feature is a function of an information cutoff. Historical facts have at
least three relevant times:

- when the sporting event occurred;
- when the provider published or represented it;
- when Soccer Bot retrieved it.

The correct time depends on the feature policy, but it must be explicit. A
post-match lineup retrieved during historical backfill is evidence of who
started; it is not automatically evidence that Soccer Bot could have known the
lineup before kickoff.

### 3.4 Let complexity earn its place

Every sophisticated candidate must defeat strong simple baselines under the
same temporal folds. A dynamic Poisson model that is stable, calibrated, and
interpretable is preferable to a large boosted or neural model whose apparent
gain disappears in the next season.

### 3.5 Partial-pool scarce data

Soccer contains many sparse regimes: promoted teams, transfers, substitute
players, new competitions, and rare player events. Estimates should shrink
toward defensible team, position, competition, and global priors. Missing or
thin history must not generate extreme probabilities.

### 3.6 Separate independent information from market information

The platform should maintain two conceptually different forecasts:

1. `independent`: soccer evidence excluding contemporaneous market prices;
2. `market_aware`: independent information combined with eligible, time-correct
   market consensus and liquidity features.

The independent model measures sports-model skill. The market-aware model aims
for the best final probability. An edge analysis must never claim independence
after training directly on the same price being challenged.

### 3.7 Prefer abstention to invented certainty

Unsupported contracts, unsafe player identities, stale lineups, missing
features, severe distribution shift, or insufficient history should produce a
typed unavailable/low-applicability result. Coverage is part of forecast
quality.

## 4. Information states and prediction timestamps

The product should be state-driven rather than creating arbitrary models for
every number of hours before kickoff.

### 4.1 Rolling pre-lineup state

This state uses information legitimately available before confirmed lineups:

- completed prior fixtures;
- time-correct team and player histories;
- schedule, venue, competition, and rest information;
- safely observed availability information if coverage is later added;
- a distribution over possible starters and minutes;
- eligible pre-lineup market observations for the market-aware forecast.

The state changes only when meaningful evidence changes. A fixed evaluation
anchor such as T-24h provides a comparable historical decision point; it does
not imply that the production application should recompute meaningless hourly
versions of an unchanged forecast.

### 4.2 Confirmed-lineup state

This state becomes available only after two valid starting elevens have been
retrieved before the kickoff known at retrieval time. It conditions on:

- starter and bench status;
- identified players and lineup completeness;
- lineup strength and composition;
- formation or role only where provider semantics are reliable;
- revised player minutes distributions;
- eligible post-lineup market observations for the market-aware forecast.

Historical evaluation may use only lineup observations satisfying the same
pregame retrieval rule. The existing historical backfill is rich outcome
evidence, but not every historical lineup is a valid confirmed-lineup feature
snapshot.

### 4.3 Corrected-lineup state

A pre-kickoff lineup correction creates a new information state and prediction.
The prior prediction remains immutable. Post-kickoff corrections never rewrite
what the platform claimed to know beforehand.

### 4.4 Exact timestamp contract

Every prediction records:

```text
prediction_at
information_state
fixture_schedule_version
source_max_retrieved_at
lineup_snapshot_ids, if any
market_snapshot_ids, if any
feature_definition_version
```

The timestamp is an audit and reproducibility mechanism even when the UI labels
the state simply as `Pre-lineup` or `Confirmed lineups`.

## 5. Contract registry

Before model implementation, the requested prediction-market contracts must be
normalized into a versioned registry. User wording and provider wording are not
sufficiently precise for settlement or model compatibility.

Each contract definition should include:

| Field | Meaning |
|---|---|
| `contract_key` | Stable internal identifier |
| `display_name` | User-facing proposition name |
| `family` | Result, goals, player, corners, or timing |
| `selection_space` | Outcomes or permitted numeric lines |
| `settlement_period` | Regulation, extra time, penalties, or another period |
| `push_policy` | How exact-line ties settle |
| `void_policy` | Postponement, abandonment, non-participation, or no-start rules |
| `participant_condition` | Unconditional, appears, starts, or minutes condition |
| `required_engine` | Predictive distribution that prices the contract |
| `required_state` | Pre-lineup, confirmed-lineup, or either |
| `eligibility_flag` | Result, team, or player fixture eligibility |
| `required_fields` | Explicit non-null and identity requirements |
| `market_mapping_policy` | Rules for accepting a Polymarket semantic match |
| `version` | Settlement/model compatibility version |

Two markets with similar titles are not equivalent if their regulation-time,
extra-time, void, or player-participation rules differ. A market comparison is
enabled only when the registry and preserved market rules prove compatibility.

### 5.1 Contracts and their foundational engines

| Contract group | Primary engine | Optional challenger |
|---|---|---|
| Home/draw/away | Joint goal score grid | Direct multiclass classifier |
| Exact score | Joint goal score grid | None unless coherently normalized |
| Goal totals/team totals | Joint goal score grid | Direct ordinal/binary model |
| Goal spreads/winning margin | Joint goal score grid | Direct margin model |
| Both teams to score | Joint goal score grid | Direct binary model |
| First team/player to score | Event-time competing-risk engine | Score-derived approximation |
| Player goals | Participation/minutes/event engine | Direct conditional classifier |
| Player assists | Participation/minutes/event engine | Direct conditional classifier |
| Match/team corners | Joint corner count grid | Direct count/quantile model |

## 6. System architecture

```text
Immutable raw evidence
        ↓
Canonical warehouse + provenance + eligibility
        ↓
Point-in-time entity states
        ↓
Frozen task datasets + manifests
        ↓
Candidate predictive engines
        ↓
Leakage-safe calibration and distribution stacking
        ↓
Versioned production distributions
        ↓
Contract pricer and semantic settlement layer
        ↓
Application snapshot: probabilities, diagnostics, market comparison
```

The contract pricer is deterministic. It does not train a model. Given a model
distribution and contract definition, it aggregates the relevant probability
mass and applies the settlement rules.

## 7. Engine A — joint match-goal distribution

This is the first core production engine because it prices the broadest set of
high-priority match contracts coherently.

### 7.1 Output

For home goals `H` and away goals `A`, estimate a normalized score grid:

```text
P(H = h, A = a | I_t),  h,a ∈ {0, 1, ..., K}
```

`K` must be high enough that omitted tail mass is negligible. Any residual tail
must be represented explicitly or folded into terminal `K+` buckets; it must
not disappear during normalization.

The first release models regulation time. A later period extension estimates a
coherent joint distribution over first- and second-half scores:

```text
P(H1, A1, H2, A2 | I_t)

H_regulation = H1 + H2
A_regulation = A1 + A2
```

This extension prices first-half and pre-match second-half exact score,
moneyline, handicap, totals, team totals, and both-teams-to-score markets while
preserving the regulation marginal. Fixtures with regulation scores but missing
halftime scores should still inform the regulation component through a
compatible marginal likelihood or ensemble rather than being discarded.

Derived quantities include:

```text
P(home win) = Σ P(h,a) for h > a
P(draw)     = Σ P(h,a) for h = a
P(away win) = Σ P(h,a) for h < a
P(over L)   = Σ P(h,a) for h + a > L
P(BTTS)     = Σ P(h,a) for h > 0 and a > 0
P(score h-a)= P(h,a)
```

### 7.2 Structural baseline

Begin with a dynamic attack/defense count model:

```text
log λ_home = μ_comp,season
             + home_advantage_comp
             + attack_home(t)
             - defense_away(t)
             + β_home · x_t

log λ_away = μ_comp,season
             + attack_away(t)
             - defense_home(t)
             + β_away · x_t
```

The simplest likelihood uses independent Poisson goal counts. The first serious
baseline adds a Dixon–Coles adjustment for low-scoring dependence and time
decay. This is a benchmark, not a permanent assumption.

Identifiability constraints must center attack and defense effects. Competition
and season parameters should partially pool toward broader priors instead of
being fit independently when samples are small.

### 7.3 Dynamic strength state

Static season averages are insufficient. Candidate state updates should compare:

- exponential time-decay maximum likelihood;
- Elo-style online strength updates;
- state-space/random-walk attack and defense effects;
- adaptive evolution variance allowing faster response to structural breaks;
- change-point or regime indicators for promotion, transfer windows, and
  material squad changes where the evidence supports them.

Decay rates and update speeds are hyperparameters selected inside temporal
development folds. They must not be chosen by looking at the final test period.

### 7.4 Dependence and dispersion candidates

The research ladder is:

1. independent Poisson;
2. Dixon–Coles low-score correction;
3. bivariate Poisson/shared-component dependence;
4. negative-binomial or Poisson-gamma overdispersion;
5. flexible copula or score-grid reweighting only if persistent residual
   diagnostics justify the added degrees of freedom.

Zero inflation, mixture regimes, and heavy-tail corrections are admitted only
after residual and probability-integral-transform diagnostics show a stable
problem across multiple development windows.

### 7.5 Hybrid machine-learning correction

A controlled boosted model can improve nonlinear effects without discarding
the count structure. Preferred designs are:

- predict residual corrections to `log λ_home` and `log λ_away`;
- predict a constrained correction to the structural score grid;
- stack a structural distribution with a direct boosted distribution using
  out-of-fold predictions.

The correction layer may use richer interactions, but its output must remain
nonnegative, normalized, and compatible with the score-grid support. Direct
home/draw/away boosting is retained as a challenger and diagnostic; it is not
allowed to silently redefine exact-score probabilities.

### 7.6 Match-model feature groups

Core candidates include:

- dynamic attacking and defensive strength;
- opponent-adjusted goals and expected goals where coverage is consistent;
- exponentially weighted shots, shots on target, and chance proxies;
- home/away-specific strength components with shrinkage;
- rest days and fixtures in trailing 7/14/30-day windows;
- travel, neutral venue, and competition-stage context when reliable;
- promotion/new-team and sparse-history indicators;
- lineup-strength distributions before confirmation;
- realized lineup strength after confirmation;
- goalkeeper and key-player effects only after stable identity/coverage audits;
- competition, season, and cross-competition translation parameters;
- market consensus only in the separate market-aware model.

“Last five matches” is not a sufficient model. Rolling windows can be useful
diagnostics, but learned decay and uncertainty-weighted state estimates should
carry the main signal.

## 8. Engine B — player participation, minutes, goals, and assists

Player propositions require an exposure model. Treating every listed player as
if they will play 90 minutes produces structurally biased probabilities.

### 8.1 Factorization

For player `i`:

```text
P(player outcome | I_t)
  = Σ_s Σ_m P(outcome | minutes=m, state=s, I_t)
              × P(minutes=m | state=s, I_t)
              × P(state=s | I_t)
```

where `state` distinguishes starter, substitute, and non-participant.

Before lineups, integrate over start and minutes uncertainty. After confirmed
lineups, condition on the observed selection role while retaining substitution
and minutes uncertainty.

### 8.2 Participation model

Candidate features include:

- recent starter/substitute/non-participant sequence;
- recent minutes with time decay;
- team membership and transfers as of the cutoff;
- player position and role;
- schedule congestion and rotation patterns;
- manager/formation effects only when reliably represented;
- competition importance/stage;
- safely observed availability evidence;
- confirmed starter/bench state in the lineup model.

The pre-lineup output is a full state distribution, not merely a likely lineup.

### 8.3 Minutes distribution

Minutes are bounded and multimodal. A single Gaussian regression is not a good
default. Candidate formulations include:

- starter/substitute-specific discrete hazards for leaving/entering;
- beta-binomial-like bounded distributions after rescaling;
- ordinal minute bins followed by within-bin refinement;
- survival models with team, player, position, and match-context effects.

The minutes model must represent mass at zero and common substitution intervals.
It should be evaluated with distributional scores, interval coverage, and
calibration by starter/bench state—not only mean absolute error.

### 8.4 Goal process

Conditional on exposure, model a player scoring hazard or count intensity using:

- individual recent goal and expected-goal rates per minute;
- shot volume and shot quality where available;
- penalty and set-piece responsibility when safely known;
- position/role priors;
- team goal intensity;
- opposing defensive and goalkeeper strength;
- confirmed teammate/opponent composition;
- home/away and competition effects;
- player-specific random effects with hierarchical shrinkage.

Rare histories shrink toward position × team-strength × competition priors.
Player goal intensities should be reconciled with the team goal distribution.
Possible implementations include allocating team scoring intensity through a
simplex of player shares, or a joint model with a reconciliation layer. The sum
of player expectations must not imply a materially incompatible team total.

### 8.5 Assist process

Assists are not attached to every goal and provider definitions can differ.
Model:

```text
P(assisted team goal) × P(player is assister | assisted goal, lineup, I_t)
```

Candidate inputs include expected assists, key-pass/chance-creation proxies,
set-piece responsibility, role, teammate finishing strength, team goal
intensity, and minutes. Provider-specific assist semantics must be normalized or
modeled separately; inconsistent definitions must not be merged as one target.

### 8.6 Player output contract

For every displayed player proposition, return:

- unconditional probability;
- conditional-on-start probability when meaningful;
- start/appearance probability;
- expected minutes and interval;
- goal/assist count distribution;
- history depth and shrinkage strength;
- identity and feature coverage state;
- model applicability warning.

## 9. Engine C — joint corner distribution

Corners require `eligible_team_models` plus explicit non-null corner values.
They are not a trivial transformation of goal intensity.

Estimate:

```text
P(C_home = h, C_away = a | I_t)
```

Candidate features include:

- team attacking width and territorial pressure proxies;
- shots, blocked-shot proxies, and possession where coverage is stable;
- opponent corner concessions;
- home/away effects;
- score-state tendencies learned only from valid historical information;
- lineup composition where it demonstrably improves forecasts;
- competition and referee effects only with adequate support;
- rest and congestion.

The baseline ladder is Poisson, negative binomial, bivariate/shared-effect
counts, then constrained boosting or quantile/count challengers. Overdispersion
and tail calibration matter because market lines often sit in the distribution
tails.

## 10. Engine D — event timing and first-score contracts

First-team and first-player-to-score contracts require timing, not merely final
score counts. A score model can provide a rough approximation but cannot fully
represent changing hazards and competing players.

The serious formulation is a competing-risk or marked point-process model:

```text
hazard(team/player scores at minute τ | no prior goal, I_t)
```

The model must include a no-goal outcome. Team hazards may vary by match phase;
player hazards depend on being on the pitch. This engine should be built only
after the score and minutes systems are reliable because it composes both.

## 11. Point-in-time feature system

### 11.1 One reusable state builder

Historical training and upcoming-fixture inference must call the same feature
definitions. The only difference is the requested cutoff and fixture.

Each feature function receives:

```text
entity IDs
fixture ID
prediction_at
information_state
feature-definition version
```

and returns values plus provenance/coverage metadata.

### 11.2 Source-fixture rule

A prior fixture may contribute sporting performance only when its relevant
period ended before `prediction_at` and its use complies with the dataset's
observation policy. The target fixture never contributes to its own features.

Schedule, lineup, availability, and market features require as-of joins using
observation or retrieval time. Selecting the latest row in the warehouse is not
point-in-time feature construction.

### 11.3 Feature metadata

Every derived feature should declare:

- semantic definition and units;
- entity grain;
- source tables and columns;
- time/cutoff rule;
- lookback/decay parameters;
- null policy;
- minimum support;
- leakage classification;
- version and implementation hash.

Missing values remain missing. Imputation is part of the model recipe and adds
explicit missingness indicators where useful.

### 11.4 Feature research discipline

New features enter through a research ledger containing:

- hypothesis and mechanism;
- data availability and coverage;
- expected failure modes;
- fold-local implementation;
- aggregate and stratified score delta;
- calibration change;
- stability across time/competitions;
- latency and operational cost;
- keep/reject decision.

A feature is not promoted because its full-history correlation looks strong.
It must improve chronological out-of-sample distributions after accounting for
the number of experiments attempted.

## 12. Market information and pricing

### 12.1 Independent forecast

The independent forecast excludes contemporaneous prices. Historical bookmaker
quotes may be used only in explicitly labeled market-informed experiments.

This model answers:

```text
What probability does the soccer evidence imply?
```

### 12.2 Market consensus forecast

Construct a transparent market benchmark from eligible as-of prices:

- preserve venue/book/exchange identity;
- use bid/ask or executable side where possible;
- remove overround using versioned methods;
- record spread, depth, liquidity, and staleness;
- exclude semantically incompatible markets;
- never substitute a post-cutoff closing price into an earlier prediction.

De-vig candidates should include proportional normalization and at least one
favorite/long-shot-aware method. The chosen transformation is selected and
reported, not hidden.

### 12.3 Market-aware forecast

The market-aware model may blend:

- the independent predictive distribution;
- de-vigged consensus probabilities;
- disagreement between venues;
- time to kickoff;
- price movement available by the cutoff;
- liquidity, spread, and staleness;
- model applicability and data-completeness indicators.

Weights must be learned from out-of-fold historical predictions. The model
should be expected to trust the market more when independent data is weak and
less when the platform has high-quality lineup/player information unsupported
by a stale or illiquid market—but that behavior must be learned and validated,
not manually asserted.

### 12.4 Edge and economic evaluation

For a buy decision, compare against executable ask; for a sell, executable bid.
An indicative last trade is not an executable price.

```text
raw_edge = model_probability - executable_price
```

Decision research additionally accounts for:

- fees;
- spread;
- available depth and slippage;
- mapping uncertainty;
- model probability uncertainty;
- multiple simultaneous correlated exposures;
- selection effects from only observing listed/liquid markets.

Profitability is a downstream test. The probability model is not trained to
maximize noisy in-sample ROI.

## 13. Candidate models and ensemble policy

### 13.1 Mandatory baselines

No production candidate is evaluated without:

- empirical competition/season priors;
- Elo or an equivalent online strength rating;
- independent Poisson;
- Dixon–Coles/time-decayed count model;
- regularized generalized linear model;
- de-vigged market consensus where valid coverage exists.

### 13.2 Controlled challengers

Challengers may include:

- hierarchical Bayesian dynamic count models;
- negative-binomial/bivariate models;
- gradient-boosted trees;
- generalized additive models;
- discrete survival/hazard models;
- neural architectures only after dataset size and a clear representation
  advantage justify them.

### 13.3 Ensemble construction

Ensembles use predictions produced strictly out of fold. Candidate methods:

- convex linear pools optimized on log score;
- regularized stacking of predictive distributions;
- regime-aware weights learned only when support is sufficient;
- Bayesian model combination as a sensitivity analysis, not an automatic
  default.

Weights are constrained and regularized. A stacker must not overfit a small
number of development folds. Component forecasts and ensemble weights remain
auditable in every model artifact.

## 14. Temporal evaluation and holdout governance

### 14.1 Fold structure

Use rolling-origin evaluation. A typical fold is:

```text
train: all eligible history through date T
calibrate/validate: the next chronological block
test fold: the following chronological block
```

Hyperparameters, feature decisions, calibrators, and ensemble weights are fit
inside the development history available for each fold. Random fixture splits
are prohibited as the primary evidence.

### 14.2 Frozen final period

After selecting the complete recipe, evaluate once on a later untouched test
period. If the recipe changes after inspecting this period, the period is no
longer final evidence; create a new version and obtain a new forward evaluation.

Record every final-holdout access in the model manifest or research ledger.

### 14.3 Competition and season structure

Report aggregate results and sufficiently supported slices:

- competition and country;
- season and calendar regime;
- club versus international;
- favorite/underdog bands;
- probability bins;
- pre-lineup versus confirmed-lineup;
- promoted/new-team status;
- player history/exposure bands;
- feature completeness and applicability bands.

Aggregate gains that come entirely from one competition or one short period are
not assumed to generalize.

### 14.4 Dependence-aware uncertainty

Fixtures are not independent identically distributed observations. Bootstrap
or resampling intervals should use blocks such as match dates/weeks and should
test sensitivity to competition-level clustering. Report score differences
with uncertainty, not only point estimates.

### 14.5 Research multiple-testing control

Repeated feature and hyperparameter experiments can overfit the development
process even when each uses chronological folds. Maintain:

- a complete experiment ledger;
- bounded candidate families and search budgets;
- stable benchmark folds;
- untouched confirmation periods;
- correction or skepticism proportional to the number of attempted variants.

Small gains must repeat across folds and regimes before promotion.

## 15. Scoring and diagnostics

### 15.1 Primary scores

- Log loss/log predictive density: primary selection score.
- Brier score: probability error and calibration diagnostic.
- Ranked probability score: ordered home/draw/away or ordinal outcomes.
- Distributional log score/CRPS-type measures: count and minutes distributions.

Accuracy, precision, recall, and exact-score hit rate remain descriptive
diagnostics. They are not sufficient model-selection objectives.

### 15.2 Calibration

Evaluate reliability by probability bin with uncertainty. Use adaptive or
equal-mass bins rather than relying on one arbitrary fixed histogram. Report:

- calibration intercept and slope;
- reliability plots;
- expected/maximum calibration error as secondary summaries;
- tail behavior for rare player and exact-score events;
- calibration by competition, model state, and history depth.

Calibration methods are part of the frozen recipe. Candidates include sigmoid/
logistic scaling, isotonic regression when sample size supports it, beta-style
calibration, and distribution-level recalibration. They are fit on leakage-safe
out-of-fold predictions and never assessed on their fitting rows.

### 15.3 Sharpness and resolution

A trivially conservative model may look calibrated while adding little value.
Track entropy, probability dispersion, and resolution alongside calibration.
The target is greater sharpness without losing reliability.

### 15.4 Coherence tests

Automated tests must verify:

- each distribution is nonnegative and sums to one;
- derived contracts exactly reconcile with the source distribution;
- home/draw/away equals the corresponding score-grid mass;
- totals and handicap complements reconcile, including pushes;
- player probabilities respect participation conditioning;
- expected player goals reconcile within tolerance to team goal intensity;
- market settlement transformations match registry fixtures.

### 15.5 Economic diagnostics

After probability validation, report:

- score delta versus market consensus;
- closing-line value where a valid later snapshot exists;
- simulated return after fees/spread/slippage;
- turnover, exposure, drawdown, and concentration;
- performance by edge band and liquidity band;
- forward paper-trading results.

Economic metrics never replace proper scores because realized returns over a
small sample are extremely noisy.

## 16. Calibration, uncertainty, and applicability

The UI must keep these concepts separate:

- `calibration`: historical reliability of the model recipe;
- `aleatoric uncertainty`: irreducible outcome randomness;
- `parameter uncertainty`: uncertainty from limited training evidence;
- `data uncertainty`: missing/stale/unsafe current inputs;
- `applicability`: similarity to supported training regimes;
- `market uncertainty`: price staleness, spread, depth, and mapping confidence.

Potential uncertainty methods include fold/ensemble dispersion, hierarchical
posterior intervals, block bootstrap, and conformal-style diagnostics where
their assumptions and target are appropriate. No single “confidence score”
should collapse all dimensions.

Typed production outcomes should include:

```text
available
available_with_warning
unsupported_contract
insufficient_history
unsafe_player_identity
missing_required_feature
lineup_not_confirmed
stale_information
out_of_distribution
incompatible_market_rules
```

## 17. Leakage and invariance test suite

The dataset layer is not complete until tests prove:

1. Mutating a future fixture cannot change an earlier feature row.
2. Mutating a post-cutoff observation cannot change an earlier row.
3. The target fixture cannot contribute to its own rolling state.
4. Full-season aggregates cannot enter an earlier-season prediction unless
   recomputed as of the cutoff.
5. Post-kickoff lineup retrieval cannot enter a confirmed-lineup dataset.
6. Post-cutoff market prices cannot enter earlier market-aware features.
7. Schedule corrections select the correct schedule version and information
   state.
8. Rebuilding from the same warehouse/config/code produces the same data hash.
9. Training and upcoming inference share feature values for an equivalent
   historical cutoff.
10. Nulls remain null until the versioned preprocessing recipe handles them.

## 18. Reproducibility and production artifacts

Every dataset build records:

- task/contract and information-state versions;
- warehouse identity and source maximum retrieval time;
- code revision and dirty-state marker;
- feature definitions and hashes;
- eligibility and explicit non-null rules;
- included/excluded counts and reason codes;
- date, competition, and season coverage;
- target distribution and feature missingness;
- output path, row count, and content hash;
- fold calendar and frozen-test declaration.

Every model version records:

- model family and full hyperparameters;
- training dataset manifest/hash;
- preprocessing and feature schema;
- fold-local predictions and evaluation reports;
- calibration and ensemble recipes;
- component and final artifact hashes;
- all-data production refit timestamp and row count;
- independent versus market-aware classification;
- compatibility with contract and information-state versions;
- promotion status and predecessor/rollback target.

The production model is refit on all eligible history only after the recipe is
frozen. Its displayed quality comes from temporal unseen data, never from its
all-data training fit.

## 19. Research roadmap

The roadmap is ordered by information and interface dependencies, not by the
number of visible UI features.

### Phase 0 — Lock the research constitution

Deliver:

- adopt this document as the technical modeling direction;
- define model-card, experiment-ledger, and final-holdout policies;
- preserve the distinction between independent and market-aware forecasts;
- establish deterministic artifact locations and version conventions.

Exit gate: another researcher can explain what qualifies as evidence for a
model improvement and what is prohibited.

### Phase 1 — Build the contract and information-state registry

Deliver:

- enumerate the user-requested contracts;
- formalize settlement, line, push, void, and participation semantics;
- map each contract to its foundational engine;
- define rolling pre-lineup and confirmed-lineup task specifications;
- implement deterministic target/settlement tests independent of modeling;
- record which contracts current warehouse fields can and cannot support.

Exit gate: every enabled contract has an unambiguous target and can be settled
deterministically from canonical facts.

### Phase 2 — Build the temporal state and dataset substrate

Deliver:

- reusable as-of feature API;
- point-in-time team and player histories;
- dynamic entity state interfaces;
- frozen Parquet datasets and JSON manifests;
- coverage/missingness reports;
- the complete leakage/invariance test suite;
- identical historical and upcoming feature paths.

Exit gate: the same warehouse, code, and config produce the same dataset hash;
future perturbations do not alter past rows.

### Phase 3 — Establish baselines and evaluation harness

Deliver:

- rolling-origin fold generator;
- final-test-period governance;
- proper scoring, calibration, sharpness, and stratification reports;
- block-bootstrap score-difference intervals;
- class-prior, Elo, Poisson, Dixon–Coles, and regularized-GLM baselines;
- market-consensus benchmark where time-correct coverage permits;
- automated versioned model cards.

Exit gate: one command evaluates any compatible candidate against the same
baselines and produces reproducible fold predictions.

### Phase 4 — Produce the first joint-score engine

Deliver:

- dynamic attack/defense state;
- independent Poisson and Dixon–Coles candidates;
- partial pooling across competition/season regimes;
- score-grid contract pricer;
- direct home/draw/away challenger;
- residual, dispersion, and coherence diagnostics;
- all-data refit of the selected recipe.

Exit gate: one saved distribution reproducibly prices moneyline, exact score,
goal totals, team totals, both-teams-to-score, and goal spreads.

### Phase 5 — Add the period-score extension and nonlinear challengers

Deliver:

- coherent joint first-half/second-half score distribution;
- pre-match first-half and second-half contract pricing;
- use of regulation-only rows through a compatible marginal likelihood or
  ensemble when halftime targets are missing;
- controlled boosted residual/intensity model;
- bivariate/negative-binomial challengers;
- out-of-fold calibration candidates;
- regularized distribution stacker;
- feature and model ablation report;
- stability analysis across competitions and seasons.

Exit gate: first-half, second-half, and regulation probabilities reconcile, and
added complexity is promoted only if its proper-score gain is stable, material
relative to uncertainty, and does not damage calibration/coherence.

### Phase 6 — Ship the first interactive vertical slice

Deliver:

- upcoming-fixture feature/inference command;
- atomic read-only application snapshot;
- fixture selection and match/period-contract explorer;
- regulation and eligible period score-grid-derived probabilities;
- version, coverage, calibration, applicability, and warning displays;
- compatible Polymarket comparison where available.

Exit gate: a real upcoming fixture can be explored across match contracts and
every number traces to a model, distribution, cutoff, and manifest.

### Phase 7 — Build player exposure and event engines

Deliver:

- participation-state model;
- starter/substitute minutes distributions;
- hierarchical player goal model;
- hierarchical assist model with provider-semantic controls;
- team/player goal-intensity reconciliation;
- pre-lineup marginal and confirmed-lineup conditional forecasts;
- player selection and proposition explorer in the app.

Exit gate: supported players receive coherent count distributions with explicit
participation/minutes uncertainty and honest sparse-history behavior.

Implementation status (2026-07-18): `confirmed_lineup_player_v1` now implements
the starter-minutes distribution, hierarchical goal/assist rates, exact
team/player reconciliation, strict pre-kickoff lineup selection, chronological
component diagnostics, and immutable prospective shadow inference. It does not
yet satisfy the phase exit gate: substitute appearance semantics are unresolved,
there are zero historical two-team lineups captured before kickoff, calibration
requires a new prospective cohort, defensive lineup contribution is absent, and
the player explorer is not exposed in the application. See
`CONFIRMED_LINEUP_PLAYER_MODEL.md`.

### Phase 8 — Build the corner engine

Deliver:

- point-in-time team corner states;
- Poisson and negative-binomial baselines;
- dependence/tail challengers;
- joint corner grid and contract pricer;
- corner calibration by line and competition;
- corner explorer in the app.

Exit gate: match and team corner totals/handicaps reconcile to one validated
joint distribution.

### Phase 9 — Build event-timing contracts

Deliver:

- no-goal-aware competing-risk team hazard model;
- player hazard integration with minutes/lineup states;
- first-team and first-player-to-score probabilities;
- timing calibration and failure diagnostics.

Exit gate: timing contracts outperform score-derived approximations on later
unseen matches and remain coherent with match scoring probabilities.

### Phase 10 — Build knockout qualification and tournament simulation

Deliver:

- competition/season-specific knockout and aggregate rules;
- first-leg and aggregate-state representation;
- extra-time and penalty-shootout structural models with uncertainty;
- match-level to-qualify probability;
- group/table/bracket state representation where required;
- path simulation for supported tournament outright markets;
- reconciliation and simulation tests across all advancement paths.

Exit gate: qualification and outright probabilities sum coherently across
mutually exclusive paths, reproduce known deterministic states, and outperform
transparent strength-based simulations on later tournaments. Unsupported rules
or insufficient evidence return a typed unavailable result.

### Phase 11 — Build market-aware probability fusion

Deliver:

- semantic market mapping audit;
- versioned de-vig and executable-price transformations;
- sufficient pregame and post-lineup snapshot history;
- independent-versus-market disagreement dataset;
- out-of-fold market-aware stacker;
- liquidity/staleness/applicability features;
- closing-line and paper-trading evaluation.

Exit gate: the market-aware forecast improves proper scores over both the
independent model and market consensus, or the project records honestly that it
does not. Edge claims additionally require forward results after costs.

### Phase 12 — Institutionalize the research loop

Deliver:

- champion/challenger model registry;
- promotion and rollback commands;
- drift, calibration, stale-model, and coverage monitoring;
- scheduled retraining only after manual reproducibility is established;
- model/data retention policy;
- forward paper portfolio with exposure and correlation controls.

Exit gate: model versions can be evaluated, promoted, monitored, and rolled
back without changing historical evidence or silently changing definitions.

## 20. Immediate implementation sequence

The next concrete work should be:

1. Obtain the user's desired contract list in informal language.
2. Create `config/contracts/` with versioned normalized specifications.
3. Implement a deterministic settlement/target library and tests.
4. Define `pre_lineup_24h_v1` and `confirmed_lineup_v1` information-state policies.
5. Build the point-in-time team-state dataset for the joint-score engine.
6. Implement the chronological evaluation harness and mandatory baselines.
7. Fit Poisson and Dixon–Coles models before adding boosted corrections.

Feature engineering begins in step 5, but its definitions are constrained by
the contract and information-state work in steps 1–4. This prevents expensive
features from being built against ambiguous targets or invalid historical
cutoffs.

Current implementation position: steps 1–7 have a complete first vertical slice
for the `CORE` regulation contracts. The deterministic target builder produces
38,445 targets from the 2026-07-13 local snapshot after four reviewed score
conflicts are excluded. The chronological state builder produces 73,258 clean
T-72h/T-24h rows with simultaneous-result batching, explicit result delay,
uncertainty, rest/congestion, and leakage tests. The frozen dataset manifest and
expanding-window evaluator produce 142,384 paired independent-Poisson and
Dixon-Coles prediction rows. Prediction events precede same-timestamp results,
and all simultaneous results update as one batch. Dixon-Coles has small
favorable final-test point estimates, but all paired calendar-month bootstrap
intervals include zero; Dixon-Coles remains a challenger.

The next research sequence is also complete. Temperature scaling is fit only
on the calibration fold. A chronological Understat-xG/API-Football-shots rate
correction first passed a development-only internal validation year, after
which the recipe was frozen, refit on all development data, calibrated on the
next year, and scored once on the final test. Against calibrated independent
Poisson, the calibrated rich model improves final-test log loss by 0.00453 at
T-24h and 0.00434 at clean T-72h; both paired month-block 95% intervals exclude
zero. It is the current regulation-moneyline champion.

The strict timestamped Polymarket audit finds no complete eligible three-way
fixture histories yet. Untimestamped Football-Data closing consensus is kept
only as a retrospective no-vig yardstick. It beats the champion by about 0.042
log-loss points on the covered final-test subset, so the remaining gap is
recorded rather than hidden.

The champion is now refit and packaged on all eligible local history. The
versioned artifact contains horizon-specific global rate scales, rich xG/shots
coefficients, and the frozen evaluation temperatures. The manifest binds it to
the warehouse snapshot, feature/task/contract configurations, selection
evidence, logical training rows, and inference schema.

Upcoming inference uses a separate no-outcome feature type, replays the same
chronological team and rich-rate states, and requires the exact current kickoff
to have been observed by the horizon cutoff. Horizons discovered late or
retrospectively rescheduled fail closed. Cold-start/prior-only forecasts are
returned with typed warnings rather than silently modified by an unevaluated
rule. The calibrated output is currently regulation moneyline only because
temperature scaling is not coherent with the raw score grid. Full decisions and
parameters are in `REGULATION_CHAMPION_MODEL.md`.

The fixture-selection application and timestamped Polymarket evidence path are
now connected. The confirmed-lineup/player component and immutable shadow path
also exist, but remain disabled for public output. The next evidence is a new
prospective cohort of lineups genuinely retrieved before kickoff; distribution-
level calibration and any team-rate promotion must use that cohort rather than
the opened result-model final test or backfilled post-kickoff lineups.

## 21. Sources and intellectual baseline

This design uses established probabilistic-forecasting ideas as baselines,
then requires Soccer Bot-specific improvements to survive its own temporal
evidence:

- Mark Dixon and Stuart Coles, [Modelling Association Football Scores and
  Inefficiencies in the Football Betting Market](https://doi.org/10.1111/1467-9876.00065).
- Tilmann Gneiting and Adrian Raftery, [Strictly Proper Scoring Rules,
  Prediction, and Estimation](https://doi.org/10.1198/016214506000001437).
- Alexandru Niculescu-Mizil and Rich Caruana, [Predicting Good Probabilities
  with Supervised Learning](https://www.cs.cornell.edu/~alexn/papers/calibration.icml05.crc.rev3.pdf).
- Yuling Yao, Aki Vehtari, Daniel Simpson, and Andrew Gelman, [Using Stacking to
  Average Bayesian Predictive Distributions](https://doi.org/10.1214/17-BA1091).

The competitive advantage is not a secret algorithm promised in advance. It is
the combination of time-correct evidence, coherent distributions, conservative
identity handling, partial pooling, disciplined experimentation, market-grade
benchmarks, and the willingness to reject attractive ideas that fail later
unseen data.
