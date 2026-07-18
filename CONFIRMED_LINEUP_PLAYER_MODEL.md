# Confirmed-Lineup and Player Model — Technical Reference

- Status date: 2026-07-18
- Model recipe: `confirmed_lineup_player_v1`
- Information state: `confirmed_lineup_v1`
- Authorization: component research plus prospective shadow only
- Champion replacement: forbidden
- Trading: forbidden

## 1. Executive statement

This document describes Soccer Bot's first coherent confirmed-lineup/player
forecasting system. It updates a T−24h team forecast after two official starting
elevens have been retrieved before kickoff, then allocates the team goal process
across named starters.

The implementation includes:

- a point-in-time positive-exposure player target dataset;
- hierarchical player goal and assist intensities;
- a discrete, shrunk starter-minutes distribution;
- exact reconciliation between player and team expected goals;
- an assist allocation that preserves unassisted goals and prevents a player
  from scoring and assisting the same goal;
- a bounded attacking-lineup shock relative to a team's recent typical lineup;
- strictly pre-kickoff confirmed-lineup selection;
- immutable prospective shadow records;
- chronological component diagnostics, calibration tables, and paired
  calendar-month bootstrap uncertainty;
- publication isolation and operational failure alerts.

It does **not** claim betting edge. The warehouse contains no historical fixture
with two lineups that satisfies the strict timestamp-safe confirmed-lineup
contract. Historical lineups were backfilled after their matches. They are
useful as outcome-side labels and for component research, but they cannot be
relabeled as pre-match observations.

Consequently:

1. the player components may be researched chronologically;
2. actual historical starter state is labeled an oracle-lineup diagnostic;
3. only newly collected, genuinely pre-kickoff lineups can create production-
   shaped shadow forecasts;
4. no lineup adjustment may replace the regulation champion until a new
   prospective gate is complete.

This separation is enforced by configuration and code rather than operator
memory.

## 2. Relationship to the regulation champion

The current champion estimates team rates at stable pre-lineup anchors:

\[
G_H \sim \operatorname{Poisson}(\lambda_H^{24}),
\qquad
G_A \sim \operatorname{Poisson}(\lambda_A^{24}).
\]

The new track adds an ordered information update:

```text
frozen T−24h forecast
    -> first valid two-team confirmed lineup
    -> starter minutes distributions
    -> player scoring and assist shares
    -> candidate lineup-conditioned team rates
    -> immutable shadow artifact
```

The T−24h forecast is retained unchanged. The lineup prediction is a child
artifact with its own timestamp, lineup raw-artifact ID, schedule-observation
ID, model version, and model hash.

There is no model fitted for every hour before kickoff. `confirmed_lineup_v1`
is a distinct information state whose anchor is the first valid lineup snapshot,
not a clock offset.

## 3. Empirical readiness audit

The 2026-07-18 local warehouse audit produced these facts.

| Quantity | Value |
|---|---:|
| Player-eligible fixtures represented in the positive-exposure dataset | 23,590 |
| Positive-minute player targets | 722,569 |
| Distinct modeled players | 18,234 |
| Target kickoff range | 2018-06-14 through 2026-07-10 |
| Confirmed-lineup fixture rows in the warehouse | 23,752 |
| Team snapshots explicitly marked pre-kickoff | 0 |
| Strict two-team timestamp-safe confirmed-lineup fixtures | 0 |
| Lineup team snapshots marked post-kickoff | 394 |
| Lineup team snapshots without capture classification | 47,428 |
| Bench rows audited | 451,733 |
| Bench rows with missing minutes | 248,813 |
| Missing-minute bench rows despite an incoming substitution event | 988 |
| Positive-minute bench rows without an incoming substitution event | 584 |

The positive-exposure dataset excluded:

| Exclusion | Rows |
|---|---:|
| Missing minutes retained as missing, never converted to zero | 243,377 |
| Unsafe placeholder player identity | 213 |
| Rows attached to conflicting final-score fixtures | 100 |
| Nonpositive or invalid minutes | 5 |
| Missing/unsupported position | 2 |

The substitute discrepancies are decisive. A missing minute cannot safely be
interpreted as an unused substitute, and a missing substitution event cannot
always be interpreted as nonappearance. Therefore unconditional substitute
appearance and player-prop probabilities remain disabled.

The reproducible audit command is:

```bash
.venv/bin/python scripts/audit_confirmed_lineup_player_readiness.py
```

The report is written to
`data/reports/players/readiness.json` on the persistent data tree.

## 4. Target construction

### 4.1 Eligibility and grain

Construction starts at `fixture_model_eligibility` and requires:

```sql
eligible_player_models = true
```

The target grain is:

```text
(fixture_id, team_id, player_id)
```

Rows require canonical fixture/player IDs, the `api_football` player-stat
source, a supported position in `{G,D,M,F}`, a safe identity, minutes in
`[1,130]`, nonnegative goals/assists, one consistent logical observation, an
unambiguous final regulation score, and kickoff before the frozen fit boundary.

### 4.2 Missingness

The central invariant is:

\[
\texttt{NULL minutes} \neq 0\text{ minutes}.
\]

Missing minutes are excluded. They are not used to train nonappearance, minutes,
goal, or assist targets. This avoids a severe downward bias in substitute props.

### 4.3 Availability time

Each player result becomes visible after the configured result delay:

\[
t_{available}=t_{kickoff}+150\text{ minutes}.
\]

A row may update player state only when:

\[
t_{available}<t_{prediction}.
\]

If the timestamps are equal, prediction happens first and the result is applied
afterward. Simultaneous fixtures are predicted as one batch before same-time
updates.

### 4.4 Frozen artifact

`build_player_modeling_dataset.py` writes deterministic `targets.parquet` and a
manifest containing schema, counts, hashes, source files, warehouse metadata,
target policy, and exclusions.

Current local identities:

```text
logical rows SHA-256:
f85043d17c64f24136137fbc6b9270e1b1237e80a972226fa5187c4403793ca2

Parquet SHA-256:
ca555acb3e70239ef58b6824491decee92aafaf3b8d8ba33963a67b04bfaf3d9
```

A production rebuild records its own warehouse and artifact hashes.

## 5. Hierarchical scoring and assist rates

For position (k), empirical goal and assist rates per minute are:

\[
r^G_k=\frac{\sum_{i:k(i)=k}G_i}{\sum_{i:k(i)=k}M_i},
\qquad
r^A_k=\frac{\sum_{i:k(i)=k}A_i}{\sum_{i:k(i)=k}M_i}.
\]

For player (p), the Gamma–Poisson posterior means are:

\[
\widehat r^G_p
=\frac{G_p+\kappa_G r^G_{k(p)}}{M_p+\kappa_G},
\qquad \kappa_G=900,
\]

\[
\widehat r^A_p
=\frac{A_p+\kappa_A r^A_{k(p)}}{M_p+\kappa_A},
\qquad \kappa_A=900.
\]

A new player receives the position rate. At 900 historical minutes, raw player
evidence and the position prior receive equal exposure weight. Individual
evidence dominates smoothly as history grows. This prevents extreme estimates
from tiny samples.

## 6. Starter-minutes distribution

Starter minutes are bounded and multimodal, so a Gaussian point regression is
not used. The model uses six bins with upper bounds:

```text
45, 59, 69, 79, 89, 130
```

For position (k), bin counts (n_{kb}) receive a unit Dirichlet prior:

\[
\pi_{kb}=\frac{n_{kb}+1}{\sum_j n_{kj}+B},\qquad B=6.
\]

The empirical position-specific mean minute in each bin is
\(\overline m_{kb}\). Empty bins use their support midpoint.

For player (p), starter-bin counts are shrunk toward the position distribution
with 20 prior starts:

\[
\widehat\pi_{pb}
=\frac{n_{pb}+20\pi_{k(p)b}}{N_p+20}.
\]

Expected starter minutes are:

\[
E[M_p\mid start]
=\sum_b\widehat\pi_{pb}\overline m_{k(p)b}.
\]

The full probability vector is published alongside the expectation.

## 7. Team-to-player goal reconciliation

For each confirmed starter:

\[
w^G_p=E[M_p\mid start]\widehat r^G_p.
\]

The configured unattributed share is (s_{res}=0.03). Named-player shares are:

\[
s^G_p=(1-s_{res})\frac{w^G_p}{\sum_qw^G_q}.
\]

Therefore:

\[
\sum_p s^G_p+s_{res}=1.
\]

The residual covers own goals, players outside the supported allocation, and
irreducible attribution uncertainty.

Given parent team rate \(\lambda_t\):

\[
\lambda^G_p=\lambda_t s^G_p,
\]

\[
\sum_p\lambda^G_p+\lambda_t s_{res}=\lambda_t.
\]

Under Poisson thinning:

\[
G_p\sim\operatorname{Poisson}(\lambda^G_p),
\qquad P(G_p\ge1)=1-e^{-\lambda^G_p}.
\]

Outputs include 0, 1, 2, and 3+ goal probabilities.

## 8. Assist allocation and scorer–assister consistency

The probability that a team goal receives a recorded assist is estimated from
unique fixture-team goals and player assists:

\[
q_A=\frac{\sum A_p}{\sum G_{team}}.
\]

The frozen fit gives:

```text
q_A = 0.6752983595810301
```

This is provider-specific and requires a settlement-semantics review before
comparison to any external player-assist contract.

Raw player weights are:

\[
w^A_p=E[M_p\mid start]\widehat r^A_p.
\]

They are normalized so \(\sum_ps^A_p=q_A\), while enforcing:

\[
s^G_p+s^A_p\le1.
\]

If an unconstrained share violates this cap, excess assist mass is redistributed
over uncapped players in proportion to their raw assist weights. Thus a player
cannot score and assist the same goal.

Player assist expectation is:

\[
\lambda^A_p=\lambda_t s^A_p.
\]

The exact identities are:

\[
\sum_p\lambda^A_p=\lambda_tq_A,
\qquad
\lambda_{unassisted}=\lambda_t(1-q_A),
\]

\[
\sum_p\lambda^A_p+\lambda_{unassisted}=\lambda_t.
\]

For the score-or-assist union, Poisson marking gives:

\[
P(G_p\ge1\cup A_p\ge1)
=1-\exp[-\lambda_t(s^G_p+s^A_p)].
\]

The two anytime probabilities are never simply added.

## 9. Candidate lineup adjustment

For a confirmed eleven, define its attacking index:

\[
I_{t,current}=\sum_{p\in starters}
E[M_p\mid start]\widehat r^G_p.
\]

The artifact retains the mean of up to the team's 20 most recent complete
historical starting-eleven indices, \(\overline I_{t,typical}\). Reliability is:

\[
\omega_t=\min\left(1,\frac{N_{lineup,t}}{20}\right).
\]

The candidate log-rate shock is:

\[
\delta_t=\operatorname{clip}\left(
0.25\omega_t\log\frac{I_{t,current}}{\overline I_{t,typical}},
-0.12,+0.12\right).
\]

The shadow candidate is:

\[
\lambda^{LU}_t=\lambda^{24}_te^{\delta_t}.
\]

The maximum multiplicative change is approximately \(e^{0.12}=1.1275\).

This adjustment currently represents attacking composition only and has no
timestamp-safe confirmed-lineup backtest. The artifact therefore exposes both
base and candidate rates but always writes:

```text
authorized_to_replace_champion_rate = false
```

Any attempted authorization is a critical operational alert.

## 10. Confirmed-lineup information contract

A lineup is eligible only when:

1. both team snapshots use the same immutable raw artifact;
2. both use the same schedule observation;
3. that observed kickoff equals the current canonical kickoff;
4. both snapshots are complete and confirmed;
5. both are explicitly marked pre-kickoff;
6. retrieval is strictly earlier than the kickoff known at retrieval;
7. exactly the two fixture teams are represented;
8. each team has exactly 11 distinct starters;
9. every starter identity is resolved and non-placeholder;
10. retrieval is no later than inference `as_of`;
11. inference `as_of` itself is strictly before kickoff, so a pregame lineup
    cannot be reconstructed into a prospective artifact after play begins;
12. a T−24h parent forecast exists;
13. the T−24h parent prediction strictly precedes lineup retrieval.

The first artifact satisfying all conditions becomes the anchor:

\[
t_{T-24}<t_{lineup}<t_{kickoff}.
\]

Equality fails closed. Later recovered lineups cannot rewrite the first
prediction.

## 11. Output contract

For starters, shadow output contains canonical identity, position, historical
minutes/starts, expected minutes, the minute-bin distribution, expected goals,
0/1/2/3+ goal probabilities, anytime goal, expected assists, 0/1/2/3+ assist
probabilities, anytime assist, score-or-assist, and sparse-history warnings.

For substitutes, identity and selection role are retained but unconditional
minutes/goals/assists are `NULL`. The warning is:

```text
substitute_appearance_target_not_semantically_validated
```

Team output contains parent and candidate expected goals, current/typical lineup
indices, history depth, bounded candidate shock, player and residual goal mass,
assist and unassisted mass, and the false champion-authorization flag.

First-scorer probabilities are absent. They require player-on-pitch competing
risks and a no-goal outcome; normalizing anytime probabilities would be wrong.

## 12. Immutable prospective publication

`predict_confirmed_lineup_player_shadow.py` reads the warehouse in DuckDB
read-only mode, the frozen player artifact/config, and the current champion
snapshot. Each eligible fixture writes:

```text
data/predictions/confirmed_lineup_player_v1/evidence/
    <fixture_id>/<lineup-retrieval-timestamp>.json
```

Creation uses exclusive file semantics. An existing path must have byte-identical
content or the run fails. Each record has a logical SHA-256. `latest.json` is a
mutable index, while `receipts.jsonl` is append-only.

No realized results, prices, bets, orders, or performance statistics enter the
prediction artifact.

## 13. Component diagnostic

### 13.1 Interpretation

The diagnostic evaluates player-vs-position goal shrinkage, player-vs-position
assist shrinkage, and starter-minute distributions. Actual starter status is a
post-match label, so the report is explicitly:

```text
oracle_lineup_component_diagnostic_only
```

It cannot establish confirmed-lineup forecast performance.

### 13.2 Chronology

Position priors use warmup observations available before 2022-07-01. Player
states replay chronologically. The diagnostic ends before 2026-07-15. The
score-grid prospective period is never inspected.

### 13.3 Results

The diagnostic contains 413,015 starter predictions across 49 calendar-month
blocks.

| Target | Mean log-loss delta, challenger − baseline | Paired month-block 95% interval | Bootstrap P(challenger better) |
|---|---:|---:|---:|
| Anytime goal | −0.0071128 | [−0.0076664, −0.0065441] | 1.000 |
| Anytime assist | −0.0035838 | [−0.0040272, −0.0031154] | 1.000 |

The hierarchy beat the position baseline on both metrics in every calendar-year
fold. Starter-minute MAE ranged from 8.50 to 8.83 minutes by year.

### 13.4 Calibration

Raw component probabilities are not calibrated for release. The tables show
underprediction in the lowest bins and overprediction in upper bins. Examples:

- goal predictions in `[0.20,1]`: mean 0.2816, observed 0.2549;
- assist predictions in `[0.20,1]`: mean 0.2351, observed 0.1829;
- goal predictions below 0.02: mean 0.00614, observed 0.01413.

Favorable discrimination/log-loss evidence is therefore not a promotion
decision. Calibration must use an ordered, timestamp-safe confirmed-lineup
cohort rather than repeated fitting to this oracle-lineup diagnostic.

## 14. Prospective gate

The configuration freezes these minimums before prospective inspection:

- six complete calendar months;
- 2,000 settled eligible fixtures;
- five competitions;
- improved anytime-goal and anytime-assist log loss;
- acceptable starter-minutes log score and MAE;
- effectively zero goal and assist reconciliation error;
- no material parent score/moneyline degradation;
- fixed model, config, and data-contract identities.

Evaluation uses paired calendar-month blocks and is not repeatedly opened for
tuning. Calibration receives its own ordered fit period after sufficient
timestamp-safe predictions exist.

## 15. Guardrails

Data guardrails:

- start from `eligible_player_models`;
- never join providers by names;
- reject placeholder identities;
- keep missing minutes `NULL`;
- reject conflicting observations and ambiguous scores;
- require every target column explicitly;
- preserve raw artifacts.

Time guardrails:

- strict pre-kickoff lineup retrieval;
- exact schedule match;
- parent forecast strictly before lineup;
- result visibility uses strict inequality;
- simultaneous fixtures update in batches;
- prospective outcomes are excluded from fitting and diagnostics.

Probability guardrails:

- player goals plus residual equal the team rate;
- assists plus unassisted goals equal the team rate;
- scorer and assister marks cannot overlap for one player/goal;
- count distributions normalize;
- sparse players shrink to positions;
- unknown players require a supported confirmed position;
- candidate team shocks are clipped.

Operational guardrails:

- player shadow failure is isolated from champion publication;
- model version/hash must match configuration;
- counts must be internally consistent;
- champion activation remains false;
- evidence is immutable;
- market data are not features;
- no trading action exists.

## 16. Blocked capabilities

Unconditional substitute props remain blocked until unused substitutes can be
distinguished from missing minutes and substitution-event completeness is
validated.

Defensive lineup contribution remains blocked pending a player-on-pitch model
covering defenders, goalkeepers, cards, substitutions, opponents, and teammate
interactions.

First scorer remains blocked pending a no-goal-aware competing-risk event-time
engine.

Market edge remains unproven. It additionally requires timestamp-matched player
books, rule compatibility, fees, spread, slippage, liquidity, and selection-
bias analysis. None is a model feature here.

## 17. Reproducible commands

```bash
.venv/bin/python scripts/audit_confirmed_lineup_player_readiness.py
.venv/bin/python scripts/build_player_modeling_dataset.py
.venv/bin/python scripts/evaluate_player_components.py
.venv/bin/python scripts/fit_confirmed_lineup_player_model.py
.venv/bin/python scripts/predict_confirmed_lineup_player_shadow.py
```

The final command may legitimately report `no_eligible_confirmed_lineups`.
Zero output is correct when no lineup passes every timing and identity gate.

## 18. Code and configuration map

| Responsibility | File |
|---|---|
| Frozen recipe and gate | `config/models/confirmed_lineup_player_v1.json` |
| Targets and lineup selection | `src/soccer_bot/datasets/players.py` |
| Hierarchy, reconciliation, diagnostics | `src/soccer_bot/modeling/player_hierarchy.py` |
| Readiness audit | `scripts/audit_confirmed_lineup_player_readiness.py` |
| Dataset build | `scripts/build_player_modeling_dataset.py` |
| Component evaluation | `scripts/evaluate_player_components.py` |
| Shadow fit | `scripts/fit_confirmed_lineup_player_model.py` |
| Immutable inference | `scripts/predict_confirmed_lineup_player_shadow.py` |
| Publication isolation | `src/soccer_bot/prediction_publication.py` |
| Operational alerting | `src/soccer_bot/operational_alerts.py` |
| Tests | `tests/test_confirmed_lineup_player.py` |

## 19. Activation status

The implementation packages a compressed, hash-verified production shadow
artifact and enables the failure-isolated player-shadow block in collector
configuration. It was activated in Railway's production shadow path on
2026-07-18 under the repository's stopped-cron/current-backup procedure.

The activation evidence is deliberately stronger than a successful process
exit:

- Railway manual restore point `2026-07-18 13:44 UTC` was created from the
  stopped 5.92 GB volume before migration or model execution;
- model deployment `1d134d46-1a2f-45b1-90cb-22793f476fc2` ran exact source
  commit `389c833781c76924337079fa691eb08c14e200cd` under `sleep infinity`, with
  no cron schedule;
- the deployed compressed artifact's file SHA-256 was
  `40b3c0eb6e4ac37591a8c0fb2fe1227133f6cf7187011ec770a9e4ff50453624`;
- its decoded logical model SHA-256 matched the frozen configuration exactly:
  `bca9a13af829032b43de9e7cbbd94e070f36fcfbda76675972565748b8e8963a`;
- the deployed configuration SHA-256 matched exactly:
  `1fa75dd3f847d5c863aabab9ffd59068d79ec3912380363368c00dc2d652e36f`;
- one supervised production cycle completed with exit code zero and produced
  a durable publication receipt as-of `2026-07-18T13:53:34.492668Z`;
- the player block returned the explicitly healthy status
  `no_eligible_confirmed_lineups`, with zero prediction records, zero records
  added, and `champion_replacement_authorized: false`;
- the independent regulation champion and score-grid shadow each published 16
  rows across nine fixtures, proving that the zero-row player condition did
  not suppress or corrupt parent publication;
- the operations watchdog independently re-read the receipt identities and
  reported no player-model alert and no critical alert.

Zero player rows are the only scientifically correct output for this cycle:
no production fixture had a strictly pre-kickoff, exact-schedule, two-team
confirmed lineup satisfying every identity and cutoff requirement. It is not
permission to weaken the gate, substitute a likely lineup, or backfill an
after-kickoff lineup as if it were known beforehand.

The next scientific action is not another historical lineup backtest. It is to
collect and settle the first genuinely timestamp-safe confirmed-lineup cohort
under this already frozen protocol. The first nonzero record must still be
audited for lineup retrieval time, generation time, kickoff version, parent
forecast cutoff, model/config hashes, team-rate reconciliation, and immutable
write-once behavior before it is treated as valid prospective evidence.
