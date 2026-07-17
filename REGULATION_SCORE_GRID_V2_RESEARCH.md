# Regulation Score Grid v2 — Coherent Distribution Research

## Executive decision

The next modeling layer is a **single coherent joint distribution for the
regulation score**. It is not a collection of separately trained exact-score,
spread, total, and both-teams-to-score classifiers. Every score-dependent
contract must be a deterministic projection of the same matrix

\[
P(H=h,A=a\mid\mathcal I_t),\qquad h,a\in\{0,1,2,\ldots\},
\]

where \(H\) and \(A\) are home and away regulation goals and \(\mathcal I_t\)
is exactly the information legally available at the prediction horizon.

The first v2 research cycle selected a regularized exponential tilt of the
champion model's independent-Poisson score grid. It substantially improved
exact-score likelihood in a second, later chronological window, but **failed
the complete confirmation gate** because it did not preserve the incumbent
moneyline calibrator. It is therefore a rejected development challenger, not a
production candidate. The already-opened final test beginning 2025-07-01 was
not read.

## Why one joint score distribution is mandatory

Let \(p_{ha}=P(H=h,A=a)\). A valid score model obeys

\[
p_{ha}\ge 0,\qquad \sum_{h=0}^{\infty}\sum_{a=0}^{\infty}p_{ha}=1.
\]

All CORE regulation markets are then projections of the same probability
measure:

\[
\begin{aligned}
P(\text{home win}) &= \sum_{h>a}p_{ha},\\
P(\text{draw}) &= \sum_{h=a}p_{ha},\\
P(\text{away win}) &= \sum_{h<a}p_{ha},\\
P(H+A\le k) &= \sum_{h+a\le k}p_{ha},\\
P(H-A\le k) &= \sum_{h-a\le k}p_{ha},\\
P(\text{BTTS}) &= \sum_{h>0,a>0}p_{ha},\\
P(H=x,A=y) &= p_{xy}.
\end{aligned}
\]

Asian quarter lines are also functions of the grid. A quarter handicap is
split into its adjacent half-stakes; each grid cell maps to win, half-win,
push, half-loss, or loss under the registry's explicit settlement convention.
This guarantees that moneyline, exact-score, handicap, total, team-total, and
BTTS prices cannot contradict each other because of independently fitted
heads. `ScoreGrid` implements these projections and settlement rules.

## Baseline distribution

The production rate model produces corrected expected regulation goals
\(\lambda_H>0\) and \(\lambda_A>0\) for a fixture and horizon. The v2 baseline
is independent Poisson:

\[
q_{ha}
=P(H=h)P(A=a)
=e^{-(\lambda_H+\lambda_A)}
  \frac{\lambda_H^h}{h!}
  \frac{\lambda_A^a}{a!}.
\]

This baseline is attractive because it is coherent, parsimonious, and makes
the relationship between rates and all downstream markets explicit. Its main
structural limitations are conditional independence, Poisson equidispersion,
and a rigid relationship between the two marginal rates and the shapes of the
score, total, and difference distributions.

The production champion applies a scalar temperature to the three-way
moneyline after aggregating the raw score grid. That output is deliberately
not coherent with the raw grid, but it is the incumbent forecast that a new
joint distribution must protect. Therefore distribution metrics are compared
with the independent-Poisson grid, while challenger moneyline metrics are
compared with a separately fitted **moneyline-temperature control**:

\[
\widetilde m_r(T)=\frac{m_r^{1/T}}{\sum_{s\in\{H,D,A\}}m_s^{1/T}},
\]

where \(m_r\) is the raw grid's three-way marginal. This scalar control is fit
only on each chronological fit half and applied to its later validation half.
It is a benchmark, never represented as a coherent score distribution.

The implementation constructs Poisson probabilities by recurrence,

\[
P(X=0)=e^{-\lambda},\qquad
P(X=k)=P(X=k-1)\frac{\lambda}{k},
\]

rather than repeatedly evaluating factorials. Each marginal is extended until
its omitted tail is at most \(10^{-12}\), with at least scores 0–12 represented
and a hard safety maximum of 60 goals per team. The outer product is
renormalized after truncation. This makes the finite numerical grid sum to one
while keeping truncation error materially below forecast uncertainty.

## Candidate 1: coherent temperature scaling

The one-parameter control candidate is

\[
p_T(h,a)=\frac{q_{ha}^{1/T}}
{\sum_{i,j}q_{ij}^{1/T}},\qquad 0.5\le T\le2.
\]

At \(T=1\), this is exactly the baseline grid. Values above one flatten the
joint distribution; values below one sharpen it. Unlike applying separate
temperatures to moneyline or exact-score probabilities, joint-grid temperature
scaling preserves full cross-market coherence. The parameter is fitted by
minimizing exact-score negative log likelihood over the fit half of a
chronological window, using a bounded golden-section search in \(\log T\).

## Candidate 2: regularized exponential tilt

The selected family is

\[
p_\theta(h,a\mid\lambda_H,\lambda_A)
=\frac{q_{ha}(\lambda_H,\lambda_A)
       \exp\{\theta^\top f(h,a)\}}
      {Z(\theta;\lambda_H,\lambda_A)},
\]

with fixture-specific partition function

\[
Z(\theta;\lambda_H,\lambda_A)
=\sum_{i,j}q_{ij}(\lambda_H,\lambda_A)
       \exp\{\theta^\top f(i,j)\}.
\]

The frozen sufficient statistics are

\[
f(h,a)=
\begin{bmatrix}
h/3\\
a/3\\
(\log h!+\log a!)/5\\
\mathbf 1(h=a)\\
\mathbf 1(h=0,a=0)\\
\mathbf 1(h>0,a>0)
\end{bmatrix}.
\]

The scaling constants are numerical conditioning choices, not learned
parameters. The terms provide limited flexibility to adjust the effective
home and away rate, tail/concentration behavior, diagonal mass, the special
0–0 cell, and positive-positive mass. The model is still low dimensional and
globally shared within a horizon; it does not have enough freedom to memorize
teams or fixtures.

For fit fixtures \(n=1,\ldots,N\), the penalized objective is

\[
\mathcal L(\theta)
=-\sum_{n=1}^{N}\log p_\theta(h_n,a_n\mid\lambda_{H,n},\lambda_{A,n})
+\frac{\rho}{2}\lVert\theta\rVert_2^2,
\qquad \rho=25.
\]

Equivalently, the score of the penalized log likelihood is

\[
g(\theta)=
\sum_n\left[f(h_n,a_n)-E_{p_{\theta,n}}f(H,A)\right]-\rho\theta,
\]

and its negative Hessian / Fisher information is

\[
I(\theta)=\sum_n\operatorname{Cov}_{p_{\theta,n}}[f(H,A)]+\rho I.
\]

The implementation solves \(I(\theta)\Delta=g(\theta)\) and uses a
backtracked Newton/Fisher step. The ridge term both regularizes the model and
keeps the information matrix well conditioned. Convergence requires a maximum
parameter step below \(10^{-8}\), with a maximum of 30 iterations. A fit needs
at least 1,500 fixtures per horizon. Grid transformations use log weights and
log-sum-exp normalization, with a \(10^{-15}\) scoring floor, so every finite
cell is positive and numerical underflow cannot create accidental impossible
scores.

### Interpretation of the fitted tilt

The repeated qualitative pattern is more important than any single coefficient:

- the negative log-factorial coefficient further penalizes high-count cells,
  concentrating mass relative to the rate-matched independent Poisson;
- the positive draw term increases diagonal mass;
- the separate negative 0–0 term partly offsets the generic draw uplift at
  exactly 0–0;
- the positive BTTS term reallocates some mass toward positive-positive cells;
- home/away goal terms make small global marginal corrections.

These effects are simultaneous and normalized by \(Z\). A coefficient is not a
standalone additive probability change. It changes every cell through both its
direct exponential factor and the partition function.

## Chronological research design

The experiment deliberately avoids the already-opened final-test period. The
configuration forbids every row with kickoff at or after
`2025-07-01T00:00:00Z`; both input queries and model fitting enforce that
boundary. The loader joins rate predictions to the frozen feature artifact by
`fixture_id` and `information_state` to obtain observed regulation scores. It
does not manufacture targets and it rejects duplicate fixture/horizon rows.

Two nested chronological decisions are used:

| Stage | Fit interval | Validation interval | Purpose |
|---|---:|---:|---|
| Selection | 2023-07-01 to 2024-01-01 | 2024-01-01 to 2024-07-01 | Choose the candidate family |
| Confirmation | 2024-07-01 to 2025-01-01 | 2025-01-01 to 2025-07-01 | Score the frozen selected family once |

Each boundary is start-inclusive and end-exclusive. T−24h and clean T−72h are
fitted and evaluated separately because their rate predictions and eligible
samples differ. Validation outcomes never enter fitting or affect another
validation fixture's forecast.

The selection metric is exact-score log loss. A candidate must improve its
mean paired loss at both horizons. Weighted total-goals, goal-difference, and
moneyline log loss are frozen tie breakers. Only the selected family advances
to confirmation.

The confirmation gate requires:

1. the upper endpoint of the paired calendar-month bootstrap 95% interval for
   exact-score log-loss delta to be below zero at both horizons;
2. nonpositive mean deltas for home-goal, away-goal, total-goal, and
   goal-difference log loss at both horizons;
3. a moneyline log-loss mean delta versus the fitted moneyline-temperature
   control no worse than +0.001 at either horizon.

Uncertainty is computed from 2,000 paired calendar-month block bootstrap
replicates using seed 20260717. Pairing preserves fixture-level comparison,
while resampling whole months retains an important layer of within-month
dependence and regime clustering. There are only six validation month blocks
per stage, so the intervals should not be interpreted as asymptotic proof of a
permanent edge.

## Results

The temperature candidate improved selection-period exact-score mean log loss
slightly, but its month-block intervals crossed zero. The exponential tilt was
selected, with fixture-weighted exact-score log-loss delta −0.00694 in the
selection stage.

The later confirmation results were:

| Horizon | Fixtures | Exact-score delta | Month-block 95% interval | Total-goals delta | Goal-difference delta | Moneyline delta vs calibrated control | BTTS delta |
|---|---:|---:|---:|---:|---:|---:|---:|
| T−24h | 2,671 | −0.00530 | [−0.00877, −0.00263] | −0.00388 | −0.00281 | **+0.00218** | −0.00158 |
| Clean T−72h | 2,446 | −0.00599 | [−0.01049, −0.00240] | −0.00464 | −0.00278 | **+0.00167** | −0.00162 |

All deltas are challenger minus independent-Poisson baseline and lower is
better. Distribution-view deltas use the raw score-grid baseline; moneyline
deltas use the stronger, fit-half-only temperature control. Home- and away-goal
marginal mean log loss improved at both horizons. The away-goal intervals
crossed zero at both horizons, as did the clean-T−72h goal-difference interval.

The control temperatures fit on 2024-H2 were 1.18858 at T−24h and 1.19410 at
clean T−72h. Relative to those controls, the coherent tilt's moneyline loss was
worse by +0.00218 and +0.00167. Both exceed the frozen +0.001 tolerance. Their
month-block intervals cross zero, so the evidence does not establish certain
harm, but the point estimates violate the guardrail at both horizons. A sharp
process does not waive a failed primary-market protection rule because another
metric looks attractive.

The correct status is `research_candidate_failed_confirmation_gate`. No
production refit, inference integration, or publication is authorized for this
candidate.

## What this enables—and what it does not

Once a future challenger is promoted and refit under a frozen production
recipe, one score grid can
price:

- exact score;
- three-way regulation moneyline;
- European and Asian goal handicaps, including quarter lines;
- match totals and team totals, including quarter lines;
- both teams to score;
- derived score-consistent combinations that have an explicit settlement
  definition.

It cannot identify first team to score, scoring time, scorer, assists, corners,
cards, or other event paths from the final score alone. First-team-to-score
requires at least a competing-risks or marked point-process layer with an
explicit no-goal state. Player goals and assists require confirmed-lineup,
minutes, role, substitution, and player-event models. Corners require the team
eligibility dataset and a separate count process. Those models may consume
team score intensity, but they must not be silently inferred from the score
grid.

## Guardrails and failure conditions

- Start from `fixture_model_eligibility`; score markets require
  `eligible_result_models`, and each consumed feature must still be non-null.
- Keep regulation settlement explicit: stoppage time included, extra time and
  shootouts excluded, administrative results excluded.
- Never train on an outcome or post-match field whose availability is after
  the information cutoff.
- Preserve simultaneous-kickoff batching and delayed result availability in
  upstream features.
- Do not use Football-Data closing odds as features; absent quote timestamps
  make them retrospective benchmarks only.
- Do not tune this family or its gate against the previously inspected final
  test.
- Reject nonpositive rates, nonfinite probabilities, duplicate fixture/horizon
  rows, insufficient fit samples, unsafe timestamps, optimizer failure, and
  any grid that does not normalize.
- Report proper scoring rules, calibration, sample counts, time blocks, and
  paired uncertainty—not hit rate or cherry-picked profitable examples.
- Compare prices with timestamped, executable market books only. A model-score
  improvement is not by itself evidence of a tradable edge after spread,
  slippage, fees, limits, and latency.

## Promotion path

1. Treat exponential tilt v1 as diagnostic evidence, not a model awaiting
   automatic promotion. Its failure reveals the missing design requirement:
   score-shape correction must preserve the incumbent three-way calibration.
2. Under a new nested research window, test a constrained or hierarchical
   coherent family whose objective explicitly controls moneyline marginal
   loss while improving score shape. Freeze that family before its later
   confirmation window.
3. Accumulate a genuinely forward holdout with enough calendar blocks and
   competition coverage to test exact-score improvement and projection-level
   non-degradation.
4. Only if the complete gate passes, refit the frozen recipe on all
   then-eligible pre-holdout history, create an immutable manifest, and connect
   it to upcoming-fixture inference.
5. Publish the promoted grid plus deterministic CORE contract projections, with
   normalization, settlement, staleness, coverage, and horizon checks at the
   boundary.
6. Only then evaluate timestamped market value and bet-sizing policies. Keep
   probability estimation, market comparison, and bankroll/risk decisions as
   separate audited layers.

## Reproduction

The research configuration is
`config/models/regulation_score_grid_v2.json`. Run:

```bash
.venv/bin/python scripts/research_score_grid_v2.py
```

The ignored research artifact directory is
`data/features/regulation_team_state_v1/regulation_walk_forward_v1/rich_rate_v1/score_grid_v2/`.
It contains per-fixture evaluation rows, a report, and a manifest with source
and artifact SHA-256 hashes. The command is deterministic given the recorded
inputs and seed. It performs no warehouse writes and records
`opened_final_test_accessed: false`.
