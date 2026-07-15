# Soccer Bot — Prediction Contract Catalog

Status: reviewed product scope and implementation priority
Scope: pre-match soccer markets supported by Soccer Bot
Last coverage audit: 2026-07-13 against the local warehouse snapshot

This catalog turns the desired user-facing markets into an implementation
decision. It says which markets Soccer Bot should build, which should wait for a
dependency or data repair, and which do not belong in this repository.

Contract inclusion does not mean immediate UI availability. A contract is
enabled only when its target, settlement semantics, training coverage,
probability engine, calibration, and current inputs all pass their promotion
gates.

## 1. Decision summary

### Build as the first match-market release

- Regulation-time exact score.
- Regulation-time moneyline: home, draw, away.
- Regulation-time goal spreads/handicaps.
- Regulation-time match goal totals.
- Regulation-time team goal totals.
- Regulation-time both teams to score.

These all derive coherently from the first joint home/away regulation-score
distribution.

### Keep and build after the regulation engine

- First-half exact score, moneyline, spread, total goals, team totals, and both
  teams to score.
- Pre-match second-half exact score, moneyline, spread, total goals, team
  totals, and both teams to score.
- Anytime goalscorer.
- Player to score or assist.
- Player assists.
- First team to score.
- First goalscorer.
- Team corners.
- Total match corners.
- Match-level to qualify.
- Tournament outright winner.

These remain part of the product vision, but each depends on a later probability
engine or additional validation described below.

### Keep in the catalog but do not enable yet

- Player shots.

The current target coverage is not safe enough. Training only on non-null shot
rows could select disproportionately active shooters and produce biased
probabilities. This market remains blocked until provider null semantics are
proven or a complete event-derived target is built.

### Do not build in Soccer Bot

- Combat-sports method of victory.
- Combat-sports method of finish.

Combat sports require different entities, event structure, evidence providers,
settlement rules, features, and models. Adding them now would dilute the soccer
research system and create false reuse. If a multi-sport platform is desired
later, combat sports should be a separate domain package or repository sharing
only generic artifact, calibration, registry, and UI interfaces.

## 2. Status vocabulary

| Status | Meaning |
|---|---|
| `CORE` | Include in the first coherent match-market release |
| `NEXT` | High-priority contract with a clear dependency after the core engine |
| `LATER` | Valuable contract requiring a distinct later engine or more evidence |
| `BLOCKED_DATA` | Desired contract, but current target semantics/coverage are unsafe |
| `OUT_OF_SCOPE` | Deliberately excluded from this soccer project |

## 3. Information-state scope

This catalog covers pre-match forecasting in two states:

- `pre_lineup`: latest eligible information before confirmed lineups, evaluated
  at a standardized historical anchor such as T-24h;
- `confirmed_lineup`: two complete starting elevens retrieved before kickoff.

The production UI can update when meaningful information changes. The fixed
historical anchor exists to make evaluation comparable and leakage-safe.

Second-half markets in this catalog mean a forecast made before the match for
goals scored during regulation's second half. They do not mean a new prediction
made at halftime using the first-half score or live match state. In-play models
are outside the current system boundary.

## 4. Canonical soccer settlement conventions

These are proposed internal defaults. A linked prediction-market contract is
compatible only when its preserved rules agree.

### 4.1 Regulation time

Regulation includes first-half and second-half stoppage time. It excludes extra
time and penalty shootouts.

### 4.2 First half

First half includes first-half stoppage time. It ends at the halftime whistle.

### 4.3 Second half

Second half includes second-half stoppage time and excludes extra time. The
second-half score is:

```text
home_second_half = home_regulation - home_halftime
away_second_half = away_regulation - away_halftime
```

Rows are valid only when both halftime and regulation scores are present and
the derived values are nonnegative.

### 4.4 Postponed, abandoned, and administrative results

The internal sporting target excludes unplayed administrative results and any
fixture without a valid sporting settlement for the requested period. External
market void/postponement rules are preserved separately and must match before a
market comparison is displayed.

### 4.5 Player participation

The platform should distinguish:

- unconditional match probability;
- conditional-on-start probability;
- conditional-on-appearance probability, if a contract explicitly uses it.

External markets vary on whether a non-starter or non-participant is void. The
contract pricer must apply the market's exact rule; it must not silently treat a
void as a loss or a non-participant as a confirmed zero.

## 5. Match and period markets

### 5.1 Regulation-time exact score — `CORE`

Output:

```text
P(home regulation goals = h, away regulation goals = a)
```

Engine: joint regulation-score distribution.

Why keep it:

- it is a foundational distribution rather than one isolated proposition;
- it prices moneyline, totals, team totals, spreads, and both-teams-to-score;
- the warehouse has strong regulation-result coverage;
- coherence can be tested exactly.

### 5.2 Regulation-time moneyline — `CORE`

Selections:

- home win;
- draw;
- away win.

Engine: sum the corresponding exact-score grid cells. A direct multiclass model
is a mandatory challenger and diagnostic, but the production output should
normally remain coherent with the score grid.

### 5.3 Regulation-time spreads/handicaps — `CORE`

Supported forms should eventually include:

- integer European handicaps;
- half-goal handicaps;
- Asian quarter-goal handicaps when settlement splitting is implemented and
  tested;
- winning-margin distributions.

Engine: regulation goal-difference distribution derived from the score grid.

Every line definition must encode push and split-stake behavior. “Home -1” is
not one universal contract unless the handicap convention is specified.

### 5.4 Regulation-time match goal totals — `CORE`

Selections: over/under a registry-approved line.

Engine: distribution of `home_goals + away_goals` derived from the score grid.

Integer lines require explicit push handling. Quarter-lines require tested
stake splitting.

### 5.5 Regulation-time team totals — `CORE`

Selections: home or away team over/under a line.

Engine: the corresponding marginal goal distribution from the score grid.

### 5.6 Regulation-time both teams to score — `CORE`

Selections: yes/no.

Engine:

```text
P(BTTS=yes) = Σ P(h,a), for h > 0 and a > 0
```

The contract should use regulation time unless external rules explicitly state
otherwise.

### 5.7 First-half market family — `NEXT`

Keep:

- exact score;
- moneyline;
- spreads/handicaps;
- total first-half goals;
- first-half team totals;
- both teams to score in the first half.

Engine: joint first-half home/away goal distribution.

The local snapshot currently has valid halftime scores for 34,730 of 38,449
result-eligible fixtures, approximately 90.3%. This is enough to justify the
family, while the missingness pattern still needs competition/season auditing.

### 5.8 Pre-match second-half market family — `NEXT`

Keep:

- exact second-half score;
- second-half moneyline;
- second-half spreads/handicaps;
- total second-half goals;
- second-half team totals;
- both teams to score in the second half.

Engine: second-half score distribution, ultimately coupled to the first-half
and regulation distributions.

The preferred long-run model estimates a coherent period score system:

```text
P(H1, A1, H2, A2 | information)

H_regulation = H1 + H2
A_regulation = A1 + A2
```

This prevents first-half, second-half, and regulation probabilities from
contradicting one another. The initial regulation engine may use all eligible
regulation results; the later period engine can use a missing-target likelihood
or compatible ensemble so fixtures without halftime scores still inform the
regulation marginal.

### 5.9 First team to score — `LATER`

Selections:

- home team;
- away team;
- no goal, if supported by the external contract.

Engine: no-goal-aware competing-risk/event-time model.

Why not derive it only from final scores: final counts do not identify which
team scored first. The local snapshot has player/team-linked goal events for
22,292 result-eligible fixtures, but event completeness and score reconciliation
must be audited before target construction. Own goals and simultaneous/provider
ordering anomalies require explicit settlement tests.

### 5.10 Match-level to qualify — `LATER`

Keep this contract, but distinguish it from regulation moneyline.

The probability may depend on:

- single-leg versus two-leg rules;
- first-leg score and aggregate state;
- extra-time rules;
- penalty-shootout probability;
- away-goals rules for the specific competition/season;
- home advantage in the current leg;
- bracket and opponent identity.

The local snapshot contains only 78 fixtures with extra-time scores and 50 with
penalty scores. That is not enough to fit unconstrained extra-time and shootout
models confidently across regimes. Begin with structured priors and seek more
knockout evidence before enabling this contract.

### 5.11 Tournament outright winner — `LATER`

Keep as a long-term platform market, not as an early fixture prop.

Engine: tournament simulation over:

- current group/table or bracket state;
- remaining schedule and qualification rules;
- future opponent uncertainty;
- regulation, extra-time, and shootout models;
- correlation and team-strength uncertainty.

An outright probability cannot be produced honestly by applying the match
moneyline model once. It requires competition-format logic and simulation of
all remaining paths. Build only after match-level to-qualify probabilities are
validated.

## 6. Player markets

### 6.1 Anytime goalscorer — `NEXT`

Keep as a highest-priority player contract.

Required engine:

```text
participation state
    × minutes distribution
    × player scoring hazard conditional on exposure
    × team scoring intensity
```

Outputs must include both unconditional match probability and
conditional-on-start probability. The current warehouse has complete non-null
goals and assists for 722,825 positive-minute player observations across 23,592
player-eligible fixtures.

### 6.2 First goalscorer — `LATER`

Keep, but build after anytime scoring and event timing.

Required engine:

- player participation/minutes state;
- competing player/team goal hazards;
- no-goal probability;
- own-goal and non-player selection treatment;
- confirmed-lineup conditioning where available.

It must not be approximated by normalizing anytime-scorer probabilities; that
would ignore scoring order, no-goal mass, substitutions, and changing exposure.

### 6.3 Player to score or assist — `NEXT`

Keep.

Target:

```text
1(player records at least one goal OR at least one assist)
```

The probability is a union, not the sum of anytime-goal and anytime-assist
probabilities:

```text
P(G ∪ A) = P(G) + P(A) - P(G ∩ A)
```

The player engine must estimate or preserve goal/assist dependence. This target
has complete goal/assist fields on eligible positive-minute observations.

### 6.4 Player assists — `NEXT`

Keep as a highest-priority player contract.

Required engine:

- participation and minutes;
- team goal intensity;
- probability a team goal receives a credited assist under the provider/market
  definition;
- player share of assisted goals;
- chance-creation, set-piece, role, teammate, and opponent effects;
- hierarchical shrinkage for sparse histories.

Provider assist definitions must be compatible with market settlement rules.

### 6.5 Player shots — `BLOCKED_DATA`

Keep in the desired catalog, but do not enable or train yet.

Current local coverage among positive-minute rows on player-eligible fixtures:

| Target | Non-null rows | Eligible fixtures with all participant rows non-null |
|---|---:|---:|
| Goals | 722,825 | 23,592 |
| Assists | 722,825 | 23,592 |
| Total shots | 283,670 | 250 |
| Shots on target | 165,205 | 267 |

The critical question is whether provider null means zero attempts or missing
measurement in each payload regime. Soccer Bot's data policy forbids silently
turning null into zero.

Unblocking requirements:

1. Audit provider payload semantics by source, competition, and season.
2. Compare player shot totals with team totals and complete event feeds.
3. Establish whether zero-shot players are explicitly represented.
4. Create a shot-target eligibility rule separate from general player
   eligibility.
5. Train only after missingness is understood and target selection is unbiased.

If a complete event source becomes available, support both total shots and
shots on target as separate contract definitions rather than one ambiguous
“player shots” label.

## 7. Football corner markets

### 7.1 Team corners — `NEXT`

Keep.

Selections: home or away team over/under a line and, later, team corner
handicaps.

Engine: home/away marginal from the joint corner distribution.

Target eligibility starts from `eligible_team_models` and additionally requires
the selected canonical corner observations to be non-null. The local snapshot
has 34,599 team-model-eligible fixtures, providing a strong starting population.

### 7.2 Total match corners — `NEXT`

Keep.

Engine:

```text
P(C_home, C_away | information)
```

and the derived distribution of `C_home + C_away`.

Negative-binomial and dependent-count candidates are important because corner
variance and tail calibration may differ materially from goal counts.

Half-specific corner markets are not currently requested and should not be
added until the warehouse contains validated period-specific corner targets.

## 8. Excluded combat-sports markets

### 8.1 Method of victory — `OUT_OF_SCOPE`

Do not build in this repository.

### 8.2 Method of finish — `OUT_OF_SCOPE`

Do not build in this repository.

Before any future combat project, clarify whether these phrases are distinct in
the target venue. Depending on the rules, method of victory may include
decision, while method of finish may refer only to stoppage categories. That
semantic problem belongs in a combat-specific contract registry.

## 9. Engine dependency map

```text
Joint regulation score engine
├── Regulation exact score
├── Regulation moneyline
├── Regulation spreads
├── Regulation totals and team totals
└── Regulation both teams to score
        ↓
Joint period score engine
├── First-half market family
└── Pre-match second-half market family

Joint regulation score engine
        ↓
Player participation + minutes engine
        ↓
Player goal + assist engine
├── Anytime goalscorer
├── Player assists
└── Player scores or assists
        ↓
Competing-risk timing engine
├── First team to score
└── First goalscorer

Joint corner engine
├── Team corners
└── Total match corners

Knockout/tournament simulation
├── To qualify
└── Outright winner
```

## 10. Recommended product order

1. Regulation score engine and its six coherent match-market families.
2. Period score engine for first-half and pre-match second-half markets.
3. Player participation/minutes, anytime goals, assists, and score-or-assist.
4. Joint corner engine.
5. Event-timing engine for first-team and first-player scoring.
6. Knockout qualification and tournament simulation.
7. Player shots only after the target-data audit passes.

The platform can expose each engine when it passes its own evaluation gate. It
does not need to wait for every contract in this catalog before the first useful
release.

## 11. Implemented foundation and next action

The `CORE` regulation contracts are now specified in
`config/contracts/regulation_v1.json`. The shared score-grid pricer implements
exact score, moneyline, both-teams-to-score, integer/half/quarter totals, team
totals, and goal handicaps with explicit win/half-win/push/half-loss/loss
settlement. Contract routing and coherence are covered by unit tests.

The first target builder starts from `eligible_result_models`, derives one
regulation score target per fixture, excludes four reviewed source-score
conflicts through `regulation_score_exclusions_v1.json`, and fails on any new
unreviewed conflict. Against the 2026-07-13 local snapshot it produces 38,445
targets.

The first point-in-time feature builder is now implemented for the joint
regulation-score dataset. It creates clean T-72h and T-24h snapshots with
dynamic attack/defense state, opponent adjustment, mean reversion, uncertainty,
home advantage, rest/congestion, and coverage metadata. On the 2026-07-13 local
snapshot, 38,445 targets produce 73,258 feature rows. Those rows are now frozen
with a reproducibility manifest. The expanding-window run produces 142,384
prediction rows across independent Poisson and Dixon-Coles, with every match
predicted before its own result becomes available. Dixon-Coles is slightly
better on the final-test point estimates, but paired calendar-month uncertainty
intervals cross zero. The next contract-level benchmark is a point-in-time,
no-vig market consensus comparison where historical price coverage is valid.
