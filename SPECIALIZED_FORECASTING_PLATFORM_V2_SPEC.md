# Soccer Bot — Specialized Forecasting Platform V2 Specification

Status: approved for implementation
Date: 2026-07-19
Scope: model-family architecture, evaluation program, immutable serving
contracts, and the next frontend product surface

## 1. Objective

Build a fixture-first betting-research platform where a user can select an
upcoming soccer match, browse supported bet families, and compare:

- the strongest approved model probability for that market family;
- the corresponding fair decimal multiplier;
- a safely mapped, timestamped Polymarket bid/ask and executable multiplier
  when available;
- the model-market probability difference;
- fixture-specific data depth, recency, completeness, and warnings;
- the producing model's general held-out quality, calibration, version, and
  promotion status.

The platform is research software. It does not claim guaranteed betting edge,
place trades, or replace missing evidence with assumed values.

## 2. Decisions already made

### 2.1 Specialized family architecture

Use the strongest leakage-safe estimate for each market family. Do not require
every independently specialized family to encode one universal probability
distribution.

Initial ownership is:

| Market family | Designated engine |
|---|---|
| Regulation 1X2 | Specialized calibrated three-way champion |
| Exact score, goal marginals, totals, team totals, handicaps, BTTS | Specialized joint regulation-score distribution |
| Player goals and assists | Participation/minutes plus hierarchical player-event models |
| Team and match corners | Joint home/away corner distribution |
| First team and first player to score | No-goal-aware competing-risk timing model |
| Market comparison | Separate timestamp-safe market-evidence layer; never an independent-model feature |

Coherence is required inside each joint family. For example, exact score,
totals, handicaps, and BTTS published from the joint-score engine must reconcile
exactly with that engine's score grid. The specialized score grid is not
required to reproduce the separate 1X2 champion.

### 2.2 Parallel score-model tracks

Continue the frozen `regulation_score_grid_v3_prospective_shadow` exactly as
specified. Do not change its parameters or gate while its prospective holdout
is accumulating.

In parallel, create a new specialized score-distribution challenger whose
primary objective is score-distribution quality. The specialized challenger:

- may disagree with the 1X2 champion;
- reports that disagreement as a diagnostic;
- is judged primarily on exact-score predictive density;
- must protect totals, goal difference, goal marginals, BTTS, calibration, and
  numerical integrity;
- receives a new version and a new untouched evaluation protocol.

V3 and the specialized challenger should generate predictions for the same
eligible future fixtures where possible. Neither track cancels the other.

### 2.3 Main-product versus research presentation

The main bet browser shows one designated estimate per market. Users should not
have to choose among competing models before reading a bet.

Every displayed estimate has one of these statuses:

- `validated`: passed its frozen promotion gate and is the designated family
  champion;
- `experimental`: immutable forward prediction exists, but promotion evidence
  is incomplete;
- `unavailable`: no semantically valid prediction can be emitted at the current
  information state;
- `unsupported`: no approved data/model contract exists.

Alternative-model estimates and model disagreement belong in expanded model
details, not as competing primary prices in the main bet list.

## 3. What training means

Training a candidate and approving a product probability are separate events.

Every family proceeds through:

1. deterministic target and settlement construction;
2. point-in-time dataset generation;
3. transparent structural baselines;
4. chronological candidate development;
5. calibration using predictions not used to fit the calibrator;
6. a frozen confirmation or prospective gate;
7. all-eligible-history production refit after the recipe is selected;
8. immutable forward inference and settlement;
9. promotion only when every declared primary and guardrail condition passes.

A trained model that fails or lacks evidence remains a shadow model. The system
must return the precise reason rather than weakening the gate.

## 4. Current local readiness baseline

The 2026-07-19 read-only audit of the local warehouse snapshot found:

| Evidence | Local count |
|---|---:|
| Result-eligible fixtures | 38,449 |
| Team-model-eligible fixtures | 34,599 |
| Player-model-eligible fixtures | 23,592 |
| Fixtures with complete two-team corner targets | 34,599 |
| Leakage-safe positive-minute player target rows | 722,569 |
| Fixtures represented by leakage-safe player targets | 23,590 |
| Canonical players represented by those targets | 18,234 |
| Fixtures with timed goal events | 22,292 |
| Fixtures with any stored lineup snapshot | 23,752 |
| Fixtures with a strict, two-team, identity-safe pre-kickoff lineup | 0 |

These are local-snapshot development facts, not current Railway counts. Before
freezing a new recipe, rerun the audit against a safely obtained production
snapshot or a stopped, backed-up, read-only production volume inspection.

Consequences:

- regulation-score and corner model development has substantial historical
  target coverage;
- player component training has substantial appearance history, but confirmed-
  lineup promotion lacks a timestamp-safe historical cohort;
- first-score modeling has useful timed-event coverage, but missing-event and
  provider-completeness semantics must be audited before treating absent events
  as no goal or complete no-event exposure;
- no model may use a stricter eligibility flag merely because another target is
  available.

## 5. Shared research constitution

### 5.1 Time and leakage

- Every row has a timezone-aware `prediction_at`.
- Historical and upcoming inference call the same versioned feature builders.
- The target fixture never contributes to its own state.
- A prior result contributes only after its versioned availability time.
- Schedule, lineup, and market inputs use retrieval-time as-of joins.
- Simultaneous fixtures update state in an order-invariant batch.
- Post-kickoff recovered lineups cannot become pregame features.
- Post-cutoff market prices cannot enter earlier predictions.
- Future-row and post-cutoff perturbation tests are mandatory.

### 5.2 Missingness

- Missing values remain `NULL` until a versioned preprocessing recipe handles
  them.
- No missing result, corner, shot, xG, minute, lineup, event, or market value is
  converted to zero without proven provider semantics.
- Missingness indicators may be model features when declared before evaluation.
- Unsafe identities and ambiguous provider mappings fail closed.

### 5.3 Evaluation

- Candidate selection uses expanding-window, rolling-origin, or nested
  chronological folds.
- Calendar-month or competition-time blocks are used for paired uncertainty.
- The already-opened champion final test is a historical audit and cannot be
  reused for tuning or a new promotion claim.
- A recipe created in response to an observed validation failure receives a new
  untouched confirmation window.
- Proper probability scores determine promotion; accuracy and realized betting
  return remain diagnostics.
- Performance is reported by competition, season, probability band, history
  depth, missingness, and information state when support is adequate.

### 5.4 Reproducibility

Each dataset and model artifact records:

- task, contract, model, feature, and information-state versions;
- code revision and dirty-state marker;
- warehouse/source identity and maximum retrieval time;
- eligibility and explicit non-null rules;
- included/excluded counts and reason codes;
- feature schema and hashes;
- fold calendar and unopened evaluation boundary;
- fitting, calibration, and gate configuration;
- logical artifact and prediction hashes;
- all-history refit row count and timestamp;
- predecessor, promotion status, and rollback target.

## 6. Model family A — regulation 1X2

### 6.1 Incumbent

Retain `regulation_champion_v1` as the validated incumbent while challengers are
developed. Its published output remains calibrated regulation home/draw/away at
clean T-72 and T-24.

### 6.2 Challenger program

Candidate families may include:

- the incumbent dynamic rate model with updated training history;
- regularized multinomial models on the point-in-time team state;
- direct gradient-boosted multiclass residuals;
- rating/state-space challengers;
- leakage-safe ensembles of structurally different candidates.

The incumbent final-test report cannot select these candidates. Define a new
nested or forward evaluation period before inspecting new-feature results.

### 6.3 Primary and guardrail metrics

- Primary: three-way log loss.
- Required: Brier score, ranked probability score, calibration intercept/slope,
  adaptive-bin reliability, entropy/resolution, and paired month-block deltas.
- Guardrails: no material degradation in supported competitions, cold-start
  teams, probability tails, or either information horizon.

## 7. Model family B — specialized regulation score distribution

### 7.1 Output

Estimate one normalized joint distribution:

```text
P(home regulation goals = h, away regulation goals = a | information state)
```

All published score-family contracts are deterministic projections of this
grid:

- exact score;
- home and away goal marginals;
- match totals;
- team totals;
- goal difference and handicaps, including pushes and quarter-line splits;
- BTTS.

### 7.2 Baselines and candidates

Mandatory baselines:

- independent Poisson from the current chronological rate model;
- Dixon-Coles;
- the v3 zero-tilt parent-moneyline-preserving conditional baseline;
- frozen v3 shadow predictions when prospectively paired.

Controlled candidates:

- the v2-style regularized exponential tilt, re-versioned under a genuinely new
  specialized-score protocol;
- negative-binomial or bivariate-count dispersion/dependence models;
- conditional result-region score-shape models;
- constrained boosted residual corrections;
- ensembles only after individual candidates earn stable evidence.

Do not build an independent classifier for every exact score or betting line.
Direct totals, BTTS, and goal-difference models may be evaluated as challengers
and diagnostics. If one wins prospectively, it may become a later designated
subfamily champion, but the initial production score family remains one joint
distribution.

### 7.3 Promotion gate

Freeze the exact gate before scoring the new confirmation window.

Recommended primary gate:

- negative exact-score log-loss delta at both T-24 and clean T-72;
- paired calendar-month bootstrap 95% upper endpoint below zero at both
  horizons.

Recommended guardrails:

- nonpositive mean total-goals and goal-difference log-loss deltas;
- bounded mean degradation for home goals, away goals, BTTS, total-goal RPS,
  and goal-difference RPS;
- stable calibration by relevant total/handicap line;
- grid normalization, positive-cell, tail-mass, and settlement coherence tests;
- 1X2 divergence from the designated 1X2 champion reported, never hidden, but
  not an automatic rejection condition.

### 7.4 V3 continuation

- Keep the existing v3 artifact, gate, prospective start, immutable evidence,
  settlement ledger, and readiness program unchanged.
- Continue generation and settlement after every healthy collector cycle.
- Do not use developing specialized-score outcomes to modify v3.
- At maturity, compare v3, its zero-tilt baseline, and the specialized score
  challenger on identically paired fixtures where their protocols permit.

## 8. Model family C — player participation, minutes, goals, and assists

### 8.1 Factorization

Before confirmed lineups, an unconditional player proposition requires:

```text
P(start) × P(minutes | start) × event rate while playing
+ P(substitute appearance) × P(minutes | substitute) × event rate while playing
```

After two valid confirmed lineups, starter status is observed but minutes and
event outcomes remain uncertain.

### 8.2 Existing foundation

Retain `confirmed_lineup_player_v1` as a frozen component/shadow track. It
already supplies:

- position-pooled player goal and assist shrinkage;
- starter-minute distributions;
- exact player-to-team goal and assist mass reconciliation;
- immutable confirmed-lineup shadow evidence;
- explicit prohibition on champion replacement.

### 8.3 Required new work

- validate unused-substitute versus missing-minute semantics;
- build substitute appearance and substitute-minute targets only after that
  validation;
- add pre-lineup participation probabilities;
- model defensive and goalkeeper lineup effects separately;
- construct ordered confirmed-lineup calibration and evaluation cohorts;
- calibrate anytime goal and assist probabilities without reusing fitting rows;
- preserve player identity and lineup retrieval safeguards;
- keep first-scorer output disabled until the timing engine exists.

### 8.4 Promotion gate

- sufficient timestamp-safe settled confirmed-lineup fixtures and competitions;
- improved anytime-goal and anytime-assist log loss;
- calibrated probability tails;
- acceptable minutes log score and MAE;
- near-zero team/player goal and assist reconciliation error;
- no material degradation of the parent team-score distribution;
- stable results by position, history depth, and competition.

Until the prospective cohort exists, training may produce component and shadow
artifacts, but not a public `validated` player price.

## 9. Model family D — joint corner distribution

### 9.1 Eligibility and target

Start from `fixture_model_eligibility.eligible_team_models` and require two
explicitly non-null regulation corner observations, one per canonical team.

Estimate:

```text
P(home corners = h, away corners = a | information state)
```

Derive team corner totals, match totals, corner difference, and later corner
handicaps from the joint distribution.

### 9.2 Point-in-time features

- opponent-adjusted corners for and against;
- learned recency/decay;
- home/away and competition effects;
- shots, possession, attacking strength, score-state proxies available before
  the cutoff, and their missingness;
- rest and congestion;
- team-history depth and uncertainty;
- lineup/formation features only after timestamp-safe coverage exists.

### 9.3 Candidates

- independent Poisson;
- negative-binomial marginals;
- bivariate/dependent count distributions;
- hierarchical competition/team effects;
- constrained boosted corrections;
- direct over/under models as diagnostics, not automatically independent
  production heads.

### 9.4 Metrics and gate

- joint corner log predictive density;
- home, away, and total-corner log loss or ranked probability scores;
- calibration by commonly traded line;
- tail coverage and interval calibration;
- stability by competition and data depth;
- exact internal settlement coherence.

## 10. Model family E — first-score timing

### 10.1 Prerequisite audit

Before training, prove for each included provider/competition/season that:

- regulation goal events are complete when the final result contains goals;
- event minute and stoppage-time semantics are consistent;
- own goals, penalties, VAR reversals, and disallowed goals are represented
  correctly;
- a scoreless match is distinguishable from missing event data;
- player identities are safe for first-player output.

Fixtures failing the audit are excluded with explicit reason codes.

### 10.2 Model

Use a competing-risk or marked-event survival model with outcomes such as:

```text
home scores first
away scores first
no regulation goal
```

Player-first-scorer output additionally requires player-on-pitch exposure and
must sum consistently with team-level scoring hazards and the no-goal outcome.
Normalizing anytime-scorer probabilities is forbidden.

### 10.3 Metrics and gate

- multiclass/event-time log loss;
- integrated Brier or survival score;
- calibration of the no-goal outcome and first-score probabilities;
- timing calibration by interval;
- comparison with a transparent approximation derived from the joint score
  distribution;
- player-level evaluation only after sufficient timestamp-safe lineup and
  on-pitch evidence exists.

## 11. Market-evidence layer

Market data never enters the independent family models described above.

For a semantically identical, timestamp-safe Polymarket contract, serve:

- best bid and ask;
- midpoint and normalized market-implied probability where appropriate;
- bid/ask spread;
- visible depth and requested-size VWAP;
- fee status and fee estimate when known;
- retrieval timestamp and staleness;
- mapping status and settlement compatibility;
- model-minus-market probability difference.

Unknown fee status, missing depth, incompatible settlement, or post-cutoff
retrieval must remain unknown or unavailable. They are never treated as zero.

## 12. Model registry and routing

Add a versioned family registry with, at minimum:

```text
family_key
contract_keys
model_version
information_states
status
artifact_hash
evaluation_record
calibration_record
effective_from
predecessor
rollback_target
```

The application snapshot builder routes each supported contract to the
designated model version. It must reject:

- two primary models for the same family/contract/information state;
- a model not compatible with the contract settlement version;
- a model without its required calibration or evaluation status;
- a shadow output mislabeled as validated;
- malformed or non-normalized distributions;
- missing model/evidence hashes.

## 13. Immutable application snapshot V4

Replace the moneyline-only public shape with a versioned multi-family snapshot.
The API still validates and serves a small immutable object and never opens
DuckDB.

Recommended structure:

```json
{
  "snapshot_version": "soccer_forecasting_snapshot_v4",
  "as_of": "...",
  "fixtures": [
    {
      "fixture": {},
      "information_states": {},
      "markets": [
        {
          "contract_key": "regulation_total_goals",
          "parameters": {"line": 2.5},
          "selection": "over",
          "model_probability": 0.574,
          "fair_decimal_multiplier": 1.7422,
          "model_family": "regulation_score",
          "model_version": "...",
          "status": "experimental",
          "market_evidence": null,
          "warnings": []
        }
      ],
      "fixture_evidence": {},
      "model_references": []
    }
  ]
}
```

The precise schema must avoid repeating large score grids in every market row.
Full research distributions may be stored by hash/reference while the public
snapshot contains the bounded projections needed by the UI.

## 14. API V2

Keep server-side Next.js access to a private, read-only FastAPI service.

Recommended endpoints:

```text
GET /v2/snapshot
GET /v2/fixtures
GET /v2/fixtures/{fixture_id}
GET /v2/fixtures/{fixture_id}/markets
GET /v2/models
GET /v2/models/{model_version}
POST /v2/price
```

`POST /v2/price` accepts only registry-approved contracts, selections, lines,
and information states. It returns typed unavailability rather than inventing a
price.

## 15. Frontend information architecture

### 15.1 Primary navigation

Use a fixture-first product structure:

```text
Upcoming matches
  -> selected fixture
       -> All bets
       -> Match result
       -> Goals
       -> Players
       -> Corners
       -> Model & data
```

Add a cross-fixture opportunity view only after the individual match workflow,
market evidence, sorting semantics, and experimental-status controls are
correct.

### 15.2 Fixture page

The selected fixture page contains:

1. competition, teams, kickoff, status, and lineup state;
2. information-state selector such as T-72, T-24, or confirmed lineup;
3. market-family navigation;
4. a scannable bet table/list;
5. fixture-specific evidence and warnings;
6. expandable model and market provenance.

Each bet row should show, where valid:

```text
selection
model probability
fair decimal multiplier
Polymarket bid / ask or executable multiplier
model-market difference
model status
market observation age
```

### 15.3 Fixture evidence

Keep this evidence close to the market list:

- eligible prior matches for both teams at the exact cutoff;
- matches in recent windows and time since last competitive fixture;
- explicit post-offseason or sparse-recent-activity state;
- xG, shots, corners, lineup, player, and event coverage relevant to the
  selected market family;
- freshness and maximum source retrieval time;
- cold start, missing features, distribution shift, and identity warnings;
- global training size shown separately from selected-team history.

Do not collapse these dimensions into one unexplained confidence score.

### 15.4 Model information

Provide both contextual and global model information:

- producing family and version on every bet;
- validated/experimental status;
- training and held-out periods;
- primary proper score and baseline delta;
- calibration summaries;
- supported competitions and information states;
- current readiness counts for shadow models;
- known limitations and failure modes;
- alternative-model estimates and disagreement in an expanded research view.

### 15.5 Product language

- Say `Fair multiplier`, not bookmaker odds, for the model-derived reciprocal.
- Say `Market ask` or `Market bid` for executable observed prices.
- Say `Experimental`, not `almost validated`.
- Say `Unavailable: confirmed lineup not captured`, not `coming soon`.
- Never label model-market disagreement as guaranteed edge.

## 16. Frontend visual and interaction requirements

The current midnight trading-desk identity may evolve, but the redesign must
retain these functional principles:

- probabilities and executable comparisons are the dominant information;
- dense information remains scannable without a dashboard-card mosaic;
- market-family navigation and bet rows are optimized for repeated comparison;
- evidence is progressively disclosed without being hidden;
- validated, experimental, stale, warning, and unavailable states do not rely
  on color alone;
- desktop and mobile preserve the same decision hierarchy;
- keyboard navigation, visible focus, reduced motion, semantic tables/lists,
  and screen-reader labels are required;
- loading, empty, API-unavailable, stale, and partial-market states fail closed.

Exact art direction, density, navigation behavior, and mobile composition should
be finalized with the user before frontend implementation.

## 17. Implementation sequence

### Phase 1 — Freeze the registry and evaluation program

- add the specialized family registry;
- freeze per-family targets, information states, metrics, and gates;
- define the new untouched evaluation policy;
- preserve v3 configuration and evidence unchanged;
- add readiness audits for corners and timing events.

Exit: every intended output has one owner, target, status vocabulary, and
predeclared promotion gate.

### Phase 2 — Shared datasets and evaluation interfaces

- generalize artifact manifests and chronological fold interfaces;
- build corner and timing targets;
- extend player participation targets only where semantics pass;
- implement per-family calibration and stratified reports;
- add leakage and reproducibility tests.

Exit: one command can build and hash each family dataset without modifying the
warehouse.

### Phase 3 — Train structural baselines

- rerun/refit 1X2 baselines under the new evaluation window;
- train score-distribution baselines and the specialized challenger;
- train player component baselines;
- train Poisson/negative-binomial corner baselines;
- train a transparent first-score/no-goal baseline on audited event coverage.

Exit: every family has a reproducible baseline artifact and chronological
report, even if no challenger is promoted.

### Phase 4 — Train controlled challengers

- add only the candidate families declared in this specification or a reviewed
  amendment;
- tune inside development folds only;
- fit calibration without scoring on calibration rows;
- freeze candidates before confirmation/prospective issuance.

Exit: each family has a frozen candidate or a documented baseline-retention
decision.

### Phase 5 — Forward inference and settlement

- issue immutable shadow predictions;
- continue v3 in parallel;
- settle targets only after outcomes become valid;
- report count-only readiness automatically;
- keep one-shot promotion evaluation human-triggered.

Exit: forward evidence has complete hashes, timestamps, model identities, and
settlements.

### Phase 6 — Snapshot/API V4

- implement multi-family projection and routing;
- validate every public value and status;
- attach compatible Polymarket evidence;
- preserve last-valid cache behavior and cold-start fail-closed behavior;
- retain the moneyline V1 endpoint during a controlled compatibility period.

Exit: one immutable snapshot can serve every enabled family without warehouse
access.

### Phase 7 — Frontend V2

- finalize visual direction with the user;
- implement fixture and market-family navigation;
- implement bet comparison rows;
- implement fixture evidence and model information;
- implement alternative-model research details;
- verify all responsive, keyboard, loading, stale, empty, and error states.

Exit: every displayed number traces to a contract, information state, model,
artifact, and market observation.

## 18. Validation commands

The implementation should provide family-specific commands such as:

```text
build_<family>_modeling_dataset.py
evaluate_<family>_baselines.py
fit_<family>_challenger.py
predict_<family>_shadow.py
settle_<family>_prospective.py
check_<family>_evaluation_readiness.py
evaluate_<family>_prospective.py
```

Repository-wide validation remains:

```bash
.venv/bin/python -m unittest discover -s tests -v
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
git diff --check
```

Long dataset builds, fits, and prospective monitoring should be run deliberately
with their expected output, time estimate, and safe stopping condition reviewed
first.

## 19. Definition of done

The program is complete only when:

1. every market family has a deterministic target and settlement contract;
2. every trained model has a leakage-safe dataset and reproducibility manifest;
3. each published estimate is routed from one designated family model;
4. v3 continues unchanged until its frozen decision point;
5. specialized score-model disagreement with 1X2 is visible as a diagnostic;
6. experimental models are never mislabeled as validated;
7. missing or semantically unsafe evidence produces typed unavailability;
8. compatible Polymarket prices include timestamps, spread/depth, and mapping
   state;
9. the frontend exposes fair multipliers, market prices, evidence depth, and
   model quality without implying guaranteed edge;
10. full tests, artifact hashes, production boundaries, and rollback paths pass.

## 20. Approved implementation decisions

The user approved the following choices on 2026-07-19:

1. specialized family ownership in Section 2.1;
2. parallel continuation of frozen v3 and a new specialized score challenger;
3. experimental predictions visible in the main market browser with explicit
   status, but excluded from default opportunity ranking;
4. fixture-first navigation before a cross-fixture opportunity screen;
5. one joint score model initially owns all score-derived markets, while direct
   totals/BTTS/difference models remain challengers;
6. the existing visual design is not treated as fixed; the frontend will be
   redesigned after the underlying multi-family contract is implemented, with
   exact art direction and density refined during that work.
