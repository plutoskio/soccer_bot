# Regulation Score Grid v3 — Prospective Coherent Shadow

## Status

`regulation_score_grid_v3_prospective_shadow` is a frozen, non-production
score-distribution challenger. It was created after v2 showed a real score-shape
improvement but failed to preserve the incumbent calibrated regulation
moneyline.

V3 solves that structural problem by construction: every generated score grid
has home-win, draw, and away-win marginals exactly equal to the probabilities
published by `regulation_champion_v1`. It then models only the distribution of
scores conditional on which of those three results occurs.

Historical rows through 2026-07-10 are parameter-estimation inputs. They are
not evaluation evidence. V3 has no retrospective promotion result. Its frozen
prospective holdout begins at 2026-07-17 00:00 UTC, and an eligible prediction
must be generated from a parent snapshot created after the recipe freeze and
strictly before kickoff.

The first eligible shadow snapshot has now been generated from the verified
production champion object with `as_of=2026-07-17T17:40:45.165106Z`. It contains
25 horizon rows across 15 fixtures and was created strictly before every
included kickoff. This begins evidence accumulation; it is not enough evidence
to evaluate or promote v3.

## Mathematical construction

Let the parent rate model produce expected goals \(\lambda_H,\lambda_A\), and
let its calibrated three-way probabilities be

\[
m=(m_H,m_D,m_A),\qquad m_H+m_D+m_A=1.
\]

The raw score measure is independent Poisson:

\[
q(h,a)=e^{-(\lambda_H+\lambda_A)}
\frac{\lambda_H^h}{h!}\frac{\lambda_A^a}{a!}.
\]

Define the result map

\[
R(h,a)=
\begin{cases}
H,&h>a,\\
D,&h=a,\\
A,&h<a.
\end{cases}
\]

For parameter vector \(\theta\) and score features \(f(h,a)\), v3 is

\[
p_\theta(h,a)
=m_{R(h,a)}
\frac{q(h,a)\exp\{\theta^\top f(h,a)\}}
{Z_{R(h,a)}(\theta;\lambda_H,\lambda_A)},
\]

where each result region has its own conditional partition function

\[
Z_r(\theta;\lambda_H,\lambda_A)
=\sum_{(i,j):R(i,j)=r}q(i,j)\exp\{\theta^\top f(i,j)\}.
\]

Therefore

\[
\sum_{(h,a):R(h,a)=r}p_\theta(h,a)=m_r
\]

for every \(r\in\{H,D,A\}\), independent of \(\theta\). Moneyline preservation
is an algebraic invariant rather than a soft penalty or an empirical hope.

The coherent zero-tilt baseline is obtained by setting \(\theta=0\): it takes
the parent's calibrated result marginals and distributes each marginal across
the corresponding Poisson score cells in proportion to \(q(h,a)\). This is the
proper prospective comparator for v3.

## Conditional score-shape model

The frozen score features are

\[
f(h,a)=
\begin{bmatrix}
h/3\\
a/3\\
(\log h!+\log a!)/5\\
\mathbf 1(h=0,a=0)\\
\mathbf 1(h>0,a>0)
\end{bmatrix}.
\]

There is no generic draw indicator. Inside a result-conditional normalization
block, a draw indicator is constant and therefore unidentified. Removing it
keeps the parameterization full rank and the fitted effects interpretable.

For observed historical scores \((h_n,a_n)\), fitting minimizes the conditional
penalized negative log likelihood

\[
\mathcal L(\theta)=
\sum_n\left[
-\log q_n(h_n,a_n)
-\theta^\top f(h_n,a_n)
+\log Z_{R(h_n,a_n),n}(\theta)
\right]
+\frac{25}{2}\lVert\theta\rVert_2^2.
\]

The calibrated parent probability \(m_{R(h_n,a_n)}\) does not appear in this
objective because it is constant with respect to \(\theta\). Thus v3 learns
within-result score shape without refitting, approximating, or degrading the
parent result model.

The score and Fisher information are

\[
g(\theta)=\sum_n\left[f(h_n,a_n)
-E_\theta(f\mid R_n)\right]-25\theta,
\]

\[
I(\theta)=\sum_n\operatorname{Cov}_\theta(f\mid R_n)+25I.
\]

A backtracked Newton/Fisher iteration solves
\(I(\theta)\Delta=g(\theta)\). Convergence requires either a maximum parameter
step below \(10^{-8}\) or an average score norm below the same threshold. A
line-search stall is accepted only when the average score norm is below
\(10^{-7}\); otherwise the fit fails closed. Every emitted horizon fit must be
marked converged.

## Frozen shadow artifact

The all-history fit uses only chronologically generated rich-rate predictions
with kickoff before 2026-07-11. It contains:

| Horizon | Fit fixtures | Iterations |
|---|---:|---:|
| T−24h | 15,458 | 4 |
| Clean T−72h | 14,139 | 4 |

The logical model SHA-256 is
`d17aa0334ad85914a396089430ad588ef8ca9381227de044106c1c777cbe00c7`.

The fitted coefficients are:

| Horizon | Home goals | Away goals | Log-factorial sum | 0–0 | BTTS |
|---|---:|---:|---:|---:|---:|
| T−24h | 0.29758 | 0.30564 | −0.71157 | 0.00488 | −0.04075 |
| Clean T−72h | 0.26249 | 0.27463 | −0.66656 | 0.00904 | −0.02151 |

These are coefficients on the scaled features above. They are not evidence of
held-out accuracy. Their only legitimate interpretation at this stage is the
shape learned from training data for future testing.

## Prospective gate frozen before eligible predictions

The gate is recorded separately in
`config/models/regulation_score_grid_v3_prospective_gate.json`. This prevents a
model refit from silently rewriting the decision rule.

Evidence requirements per horizon are at least:

- six complete calendar-month blocks;
- 2,000 paired fixtures;
- five competitions;
- immutable model, gate, parent-prediction, and score-grid hashes;
- snapshot creation strictly before kickoff;
- no kickoff before 2026-07-17;
- no parameter or gate change before the decision.

The primary gate requires v3 exact-score log loss to improve in mean at both
horizons and for both paired calendar-month bootstrap 95% upper endpoints to be
below zero. The bootstrap uses 2,000 replicates and seed 20260717.

Total-goal and goal-difference mean log-loss deltas must be nonpositive at both
horizons. Home-goal, away-goal, BTTS log loss, total RPS, and goal-difference
RPS may degrade by at most 0.001 in mean. The absolute parent-versus-grid
moneyline difference may never exceed \(10^{-10}\). Every gate must pass;
market profitability cannot substitute for proper-distribution improvement.

If the gate fails, any revised features, penalties, thresholds, or model form
must use a new version and a new untouched forward holdout.

The executable program is frozen separately in
`config/models/regulation_score_grid_v3_evaluation.json`. July 2026 is excluded
as a partial holdout month. A month matures seven days after its UTC month end,
and the cutoff is the first mature month where both horizons meet all three
minimums. The scheduled collector reports only counts; it neither reads metric
fields nor runs the gate. A human must invoke the write-once command after a
readiness warning. The exact estimands, bootstrap resampling law, Type-7
quantiles, conjunction logic, and decision-integrity checks are documented in
`PROSPECTIVE_EVALUATION_PROGRAM.md`.

## Numerical and data guardrails

- Poisson support extends to at least 12 goals per team and until omitted
  marginal tail mass is below \(10^{-12}\), with a hard maximum of 60.
- Every finite grid cell must be strictly positive and the grid must sum to one.
- Parent probabilities must be finite, positive, contain exactly home/draw/away,
  and sum to one.
- The three score-grid result marginals are checked against the parent to
  absolute tolerance \(10^{-10}\).
- Training rejects duplicate fixture/horizon rows, post-cutoff rows, negative
  scores, scores outside numerical support, insufficient horizon samples,
  singular information matrices, and non-converged fits.
- The shadow uses regulation only: stoppage time included; extra time,
  shootouts, and administrative results excluded upstream.
- No market data enters fitting. Missing data is never replaced with zero.
- An old parent snapshot cannot be relabeled as prospective. The local July 15
  parent snapshot is rejected because it predates the July 17 recipe freeze;
  the accepted first snapshot comes from the post-freeze production object.

## Outputs and contract pricing

Each eligible shadow record contains the entire joint score grid, top exact
scores, home-goal, away-goal, total-goal and goal-difference marginals, BTTS,
the parent and implied moneyline, and a logical grid hash. The full grid can be
passed to the existing `ScoreGrid` settlement layer to price:

- exact score;
- regulation moneyline;
- goal handicaps, including Asian quarter lines;
- match totals and team totals, including quarter lines;
- both teams to score.

All of these are projections of the same probability measure. First scorer,
player goals/assists, corners, and first team to score still require separate
event or player processes; the final-score grid cannot identify event order or
player attribution.

## First eligible source snapshot and canonical evidence

The current production collector had already published a post-freeze champion
snapshot to its validated S3-compatible object key. It was downloaded
read-only; no live warehouse inspection, scheduler stop, or database mutation
was required.

Parent object:

```text
as_of: 2026-07-17T17:40:45.165106+00:00
prediction rows: 25
fixtures: 15
prediction_rows_sha256: 4b774417eb7e4a5e34792b7b77cf3d49d9b72e972d7838382a015a4fb81018b7
object_bytes_sha256: c78fadbc8436bb067bb4a68e8c4d1958106f29ef6082e9e366bf136a567edfcd
```

Derived shadow artifact:

```text
path: data/predictions/regulation_score_grid_v3_shadow/20260717T174045Z.json
created_at: 2026-07-17T17:45:23.810020+00:00
prediction rows: 25
fixtures: 15
file_sha256: 824ecc70570417a351be8d1d428aad3a696172b2b2438fa32e5f5924496f2ce4
maximum parent-moneyline error: 8.881784197001252e-16
maximum grid normalization error: 1.1102230246251565e-16
minimum score-cell probability: 3.254728700035796e-27
```

That timestamped file is preserved as the first source snapshot. Production now
materializes its oldest valid row for each `(fixture_id, information_state)` as
an individual immutable file under `evidence/`. The per-pair file—not the
mutable `latest.json` alias and not every five-minute refresh—is the canonical
evaluation unit. Its prediction, model, gate, parent-source, originating
snapshot, evidence-record, and per-grid hashes prove exactly which pre-match
distribution was scored.

Writing every full snapshot every five minutes would grow by roughly 316 MB per
day at the observed snapshot size. The current writer therefore retains one
full forecast per evaluation pair, writes a compact receipt only when a cycle
adds new evidence, and replaces only `latest.json`. Existing timestamped
snapshots are imported oldest-first exactly once. The complete settlement
design, metrics, temporal checks, and hash chain are specified in
`PROSPECTIVE_SETTLEMENT_LEDGER.md`.

## Commands

Fit the frozen shadow artifact:

```bash
.venv/bin/python scripts/fit_score_grid_v3_shadow.py
```

Generated ignored artifacts:

```text
data/models/regulation_score_grid_v3_shadow/model.json
data/models/regulation_score_grid_v3_shadow/manifest.json
```

After a new `regulation_champion_v1` snapshot exists with `as_of` at or after
the recipe freeze, create a shadow snapshot:

```bash
.venv/bin/python scripts/predict_score_grid_v3_shadow.py
```

To retrieve the exact current production parent object without exposing
credentials or touching the warehouse:

```bash
railway run --service soccer_bot .venv/bin/python \
  scripts/download_prediction_snapshot.py \
  --output data/predictions/regulation_champion_v1/production_latest.json
```

The command rejects a pre-freeze parent snapshot and any row generated at or
after kickoff. It writes only non-production shadow artifacts under:

```text
data/predictions/regulation_score_grid_v3_shadow/
```

The guarded collector path can generate the private shadow after a successful
champion upload and persist it under `/app/data`. Shadow failure is isolated
and cannot invalidate an already-successful public champion publication. No
public API exposure or automatic betting action is part of v3 at this stage.

After shadow evidence is durable, the collector invokes the read-only
prospective settlement updater. Completed eligible fixtures are appended to a
separate hash-chained ledger. It then updates count-only evaluation readiness.
Per-fixture scores remain unaggregated during routine operation. When the
deterministic frozen minimum is reached, an operational warning requests the
explicit one-shot evaluation; no collector cycle can run it automatically.
