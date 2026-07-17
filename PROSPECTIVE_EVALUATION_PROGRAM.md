# Regulation Score Grid v3 — Frozen Prospective Evaluation Program

## Status and purpose

This document specifies the one-shot prospective evaluation program for
`regulation_score_grid_v3_prospective_shadow`. The implementation and its
decision rule were frozen before inspecting any eligible prospective
performance aggregate.

The program answers one narrow scientific question:

> Holding the production champion's expected-goal rates and calibrated
> regulation moneyline fixed, does the frozen v3 conditional score-shape model
> predict the realized joint regulation score more sharply than the matching
> parent-preserving independent-Poisson conditional law?

It does not answer whether a bookmaker or exchange price is profitable. It
does not authorize automatic publication, promotion, or betting. A pass makes
the challenger eligible for human promotion review. A failure rejects this
challenger version; changing the model or decision rule requires a new version
and an untouched forward holdout.

The controlling artifacts are:

```text
config/models/regulation_score_grid_v3_prospective_gate.json
config/models/regulation_score_grid_v3_settlement.json
config/models/regulation_score_grid_v3_evaluation.json
src/soccer_bot/prospective_evaluation.py
scripts/check_score_grid_v3_evaluation_readiness.py
scripts/evaluate_score_grid_v3_prospective.py
```

Production outputs are separate from immutable forecasts and settlements:

```text
/app/data/predictions/regulation_score_grid_v3_evaluation/readiness.json
/app/data/predictions/regulation_score_grid_v3_evaluation/decision.json
```

`readiness.json` is replaceable and count-only. `decision.json` is write-once
and contains the one permitted performance evaluation.

## 1. Objects being compared

For fixture \(i\), horizon \(s\), and finite regulation-score support
\(\mathcal S\), the production champion supplies expected goals
\(\lambda_{H,i,s},\lambda_{A,i,s}\) and a calibrated regulation moneyline

\[
m_{i,s}=(m_H,m_D,m_A),\qquad m_H+m_D+m_A=1.
\]

The common independent-Poisson base measure is

\[
q_{i,s}(h,a)=
e^{-(\lambda_H+\lambda_A)}
\frac{\lambda_H^h}{h!}
\frac{\lambda_A^a}{a!}.
\]

Let \(R(h,a)\in\{H,D,A\}\) map a score to home win, draw, or away win. The
baseline conditions the Poisson law inside each result region and then assigns
that region the champion's calibrated mass:

\[
p_{0,i,s}(h,a)=m_{R(h,a)}
\frac{q_{i,s}(h,a)}
{\sum_{(u,v):R(u,v)=R(h,a)}q_{i,s}(u,v)}.
\]

The v3 candidate replaces the within-region conditional law with a frozen
exponential tilt \(\theta_s^\top f(h,a)\):

\[
p_{\theta,i,s}(h,a)=m_{R(h,a)}
\frac{q_{i,s}(h,a)e^{\theta_s^\top f(h,a)}}
{\sum_{(u,v):R(u,v)=R(h,a)}
q_{i,s}(u,v)e^{\theta_s^\top f(u,v)}}.
\]

Consequently, for every result region \(r\),

\[
\sum_{(h,a):R(h,a)=r}p_{\theta,i,s}(h,a)
=\sum_{(h,a):R(h,a)=r}p_{0,i,s}(h,a)=m_{r,i,s}.
\]

This is the key experimental control. Candidate and baseline have the same
fixture, information cutoff, expected-goal rates, finite support, and parent
moneyline. Their only intended difference is how probability is distributed
among exact scores within home-win, draw, and away-win regions.

## 2. Unit of observation and pairing

The atomic evaluation key is

\[
K_{i,s}=(\text{fixture\_id}_i,\text{information\_state}_s).
\]

The two frozen information states are:

- `pre_lineup_24h_v1`;
- `pre_lineup_72h_clean_v1`.

Each key can appear at most once in the hash-chained settlement ledger. The
same realized score is used to score both candidate and baseline. Therefore
every loss difference is paired:

\[
d_{i,s}^{(k)}=L_k(p_{\theta,i,s},Y_i)
-L_k(p_{0,i,s},Y_i),
\]

where \(Y_i=(H_i,A_i)\) is the realized regulation score and \(k\) identifies a
proper scoring rule. Negative \(d\) means the candidate is better.

Pairing removes outcome variation that would arise if models were evaluated on
different fixtures. It also prevents five-minute forecast refreshes from
overweighting a fixture: only the first valid immutable evidence record for a
fixture/horizon pair is eligible.

## 3. Prospective population and calendar window

The prospective holdout starts at `2026-07-17T00:00:00Z`. July 2026 is a
partial calendar month and is excluded from the decision window. The first
eligible full month is therefore August 2026.

For a calendar month \(t\), define its maturity time as

\[
\operatorname{mature}(t)=
\operatorname{start}(t+1)+7\text{ days}.
\]

For example, January 2027 matures at `2027-02-08T00:00:00Z`. The seven-day
delay gives completed results time to arrive and pass settlement validation.
A row belongs to a month by its UTC scheduled kickoff, not by prediction time,
result retrieval time, or settlement time.

At any operational time \(T\), a month is available only if

\[
\operatorname{mature}(t)\le T.
\]

The deterministic cutoff is the first available month \(c\) for which both
horizons independently satisfy every frozen evidence minimum over all months
from August 2026 through \(c\), inclusive.

The minimums at each horizon are:

\[
B_s(c)\ge6,
\qquad N_s(c)\ge2000,
\qquad C_s(c)\ge5,
\]

where:

- \(B_s(c)\) is the number of nonempty mature UTC calendar months containing
  gate-eligible settled rows;
- \(N_s(c)\) is the number of gate-eligible settled fixture/horizon rows;
- \(C_s(c)\) is the number of distinct canonical competitions.

The program includes every calendar month from August through the cutoff; it
does not choose a favorable subset. An empty month contributes no block and
therefore cannot help satisfy the six-block requirement. In the fastest
possible case, August 2026 through January 2027 supply six nonempty blocks and
the first possible evaluation time is February 8, 2027, provided both horizons
also have 2,000 rows and five competitions. Those counts are not assumed; the
actual cutoff can be later.

The search stops at the first qualifying cutoff. Waiting for a later,
potentially more favorable month after the first cutoff would create an
outcome-dependent stopping rule and is forbidden.

## 4. Eligibility and fail-closed evidence validation

Only settlement rows with `eligible_for_prospective_gate = true` enter counts
or performance calculations. That field must equal the conjunction of every
stored integrity check. The evaluator verifies the ledger hash chain and
rejects duplicate evidence keys or duplicate fixture/horizon pairs.

For every ledger envelope, it verifies:

- ledger, model, logical-model, and gate versions;
- exact settlement-config, prospective-gate, and model-artifact hashes;
- one of the two frozen horizons;
- a nonempty canonical competition ID;
- a timezone-aware kickoff;
- a nonempty Boolean integrity vector;
- exact equality between gate eligibility and `all(integrity_checks)`.

Metric fields are deliberately not accessed by the automatic readiness path.
After readiness and only inside the explicit one-shot path, the evaluator also
requires finite candidate, baseline, and stored-delta values for every frozen
metric. For each metric it recomputes

\[
\widehat d=L_{\text{candidate}}-L_{\text{baseline}}
\]

and requires agreement with the stored delta to absolute tolerance
\(10^{-15}\), with zero relative tolerance. Nonfinite values, missing metrics,
or inconsistent arithmetic fail closed before a decision is written.

## 5. Automatic readiness is count-only

The collector automatically runs the readiness command only after a successful
or no-op verified settlement update. This path may inspect:

- artifact identities;
- the append-only hash chain;
- kickoff months;
- competition IDs;
- eligibility Booleans;
- fixture, month-block, and competition counts.

It does not access per-fixture metric fields, compute a mean, run a bootstrap,
or expose any performance statistic. The readiness artifact contains explicit
guard fields:

```text
performance_statistics_exposed: false
automatic_decision_execution: false
explicit_one_shot_command_required: true
```

Both the publication subprocess boundary and the operational watchdog reject a
receipt that contains performance vocabulary such as log loss, RPS, Brier,
mean delta, confidence interval, or candidate-minus-baseline. This makes the
anti-peeking policy executable rather than merely documentary.

Readiness has three valid states:

1. `locked_insufficient_evidence` — at least one minimum is not met and no
   decision is written;
2. `ready_for_explicit_one_shot_evaluation` — the deterministic cutoff exists,
   but the collector still does not evaluate performance;
3. `decision_already_exists` — an immutable decision has already been written
   and validated.

The transition to state 2 raises an operational warning. It does not fail the
collector and does not execute the decision command.

## 6. Proper scoring rules

### 6.1 Exact-score log loss

For realized score \(Y_i=(H_i,A_i)\),

\[
L_{\text{exact}}(p,Y_i)=-\log p(H_i,A_i).
\]

This is the primary metric because the challenger changes the joint score law.
Log loss is strictly proper: in expectation, a forecaster minimizes it by
reporting the true distribution. It strongly penalizes assigning too little
mass to the realized cell.

### 6.2 Marginal and derived-event log loss

The settlement layer deterministically marginalizes the score grid to total
goals, goal difference, home goals, away goals, and both-teams-to-score. For a
discrete derived target \(Z=g(H,A)\),

\[
p_Z(z)=\sum_{(h,a):g(h,a)=z}p(h,a),
\qquad L_Z(p,z_i)=-\log p_Z(z_i).
\]

The frozen secondary log-loss metrics are:

- total goals;
- goal difference;
- home goals;
- away goals;
- both teams to score.

### 6.3 Ranked probability score

For an ordered outcome with categories \(1,\ldots,K\), cumulative forecast
\(F(k)\), and realized category \(y\), the ranked probability score is

\[
\operatorname{RPS}(F,y)=
\frac{1}{K-1}\sum_{k=1}^{K-1}
\left(F(k)-\mathbb 1\{y\le k\}\right)^2.
\]

The program evaluates total-goals RPS and goal-difference RPS. Unlike category
log loss, RPS respects ordering: probability placed near the realized total or
margin is penalized less than probability placed far away.

All fixture-level scores are produced by the previously frozen settlement
layer. The evaluator does not re-price markets from mutable present-day data.

## 7. Point estimands

For horizon \(s\), metric \(k\), and selected set \(\mathcal I_s(c)\), the point
estimate is the fixture-weighted paired mean

\[
\bar d_s^{(k)}=
\frac{1}{N_s(c)}\sum_{i\in\mathcal I_s(c)}d_{i,s}^{(k)}.
\]

Every fixture/horizon row has equal weight. Competitions and months are not
reweighted to equal size. This estimand describes average loss difference over
the actual eligible prospective fixture mix accumulated by the frozen cutoff.

## 8. Paired calendar-month cluster bootstrap

Fixtures within a league-month are not independent in the naive iid sense.
Team form, seasonal regimes, provider conditions, and calibration errors can
persist over time. The uncertainty calculation therefore resamples calendar
months as clusters while preserving candidate-baseline pairing.

For one horizon, let the observed nonempty month labels be

\[
\mathcal B=(b_1,\ldots,b_B).
\]

For bootstrap replicate \(r\):

1. draw \(B\) month labels independently with replacement from
   \(\mathcal B\);
2. for every drawn label, concatenate all paired fixture deltas in that month;
3. if a label is drawn more than once, include its entire fixture set that many
   times;
4. calculate the ordinary mean across the concatenated fixture deltas.

Thus

\[
\bar d_s^{*(r)}=
\frac{\sum_{j=1}^{B}\sum_{i\in b_j^*}d_{i,s}}
{\sum_{j=1}^{B}|b_j^*|}.
\]

This is intentionally fixture-weighted after cluster resampling. It is not the
unweighted mean of month means. Candidate and baseline are never independently
resampled.

The frozen bootstrap parameters are:

```text
replicates: 2000
seed: 20260717
lower quantile: 0.025
upper quantile: 0.975
quantile rule: linear Type 7
```

Each horizon creates a fresh deterministic pseudorandom generator with the
frozen seed. This prevents call order or another horizon's sample count from
changing its interval.

For sorted bootstrap estimates
\(x_{(1)}\le\cdots\le x_{(R)}\), Type-7 quantile interpolation at probability
\(p\) uses zero-based position

\[
h=(R-1)p,
\]

and linearly interpolates between indices \(\lfloor h\rfloor\) and
\(\lceil h\rceil\). The stored interval is the percentile interval

\[
[Q_{0.025},Q_{0.975}].
\]

## 9. Frozen decision gates

Define every delta as candidate minus baseline, so negative is better.

### 9.1 Primary gate

At each horizon separately, exact-score log loss must satisfy both

\[
\bar d_s^{(\text{exact})}<0
\]

and

\[
Q_{0.975}\left(\bar d_s^{*(\text{exact})}\right)<0.
\]

The first condition requires an observed mean improvement. The second requires
the upper endpoint of the paired month-cluster percentile interval to remain
strictly below zero. A tiny negative mean with uncertainty crossing zero fails.

### 9.2 Nondegradation gates

At each horizon,

\[
\bar d_s^{(\text{total-goals log loss})}\le0,
\qquad
\bar d_s^{(\text{goal-difference log loss})}\le0.
\]

These derived distributions must not degrade in mean.

### 9.3 Strict tolerance gates

At each horizon, each of the following mean deltas must be at most \(0.001\):

\[
\begin{aligned}
\bar d_s^{(\text{home-goals log loss})}&\le0.001,\\
\bar d_s^{(\text{away-goals log loss})}&\le0.001,\\
\bar d_s^{(\text{BTTS log loss})}&\le0.001,\\
\bar d_s^{(\text{total-goals RPS})}&\le0.001,\\
\bar d_s^{(\text{goal-difference RPS})}&\le0.001.
\end{aligned}
\]

These tolerances prevent a joint-score gain from hiding a material regression
in important marginals or ordered distributions.

### 9.4 Parent-moneyline invariance

For every selected row and both candidate and baseline, let

\[
\epsilon_{i,s}=
\max_{r\in\{H,D,A\}}|\widehat m_{i,s,r}-m_{i,s,r}|.
\]

The maximum over all rows and both model sides must satisfy

\[
\max_{i,s}\epsilon_{i,s}\le10^{-10}.
\]

This is a numerical invariant, not a performance concession. A breach means
the candidate-control experiment no longer cleanly holds moneyline fixed.

### 9.5 Conjunction

There is no compensating trade-off across metrics or horizons:

\[
\operatorname{PASS}=
\bigwedge_{s\in\{24h,72h\}}
\bigwedge_{g\in\mathcal G}g_s.
\]

Every primary and secondary check must pass at both horizons. One failed
Boolean produces an overall failure.

## 10. One-shot and immutability semantics

The explicit command has no option to select a cutoff, relax a threshold,
change a seed, increase replicates, or force evaluation before readiness. It
derives the cutoff solely from the frozen config, verified ledger, and supplied
UTC evaluation time.

The decision records:

- evaluation, model, logical-model, gate, settlement-config, evaluator-module,
  and evaluation-config identities;
- full observed ledger length, head, and file hash at decision time;
- selected row count and a SHA-256 over the ordered selected record hashes;
- first month, deterministic cutoff, and cutoff maturity time;
- frozen bootstrap, metric, and evidence policies;
- per-horizon counts, point estimates, interval, and every gate Boolean;
- overall pass/fail and its permitted operational meaning;
- an internal logical record SHA-256.

The file is created with exclusive temporary-file creation, file `fsync`, a
hard-link create-if-absent operation, and directory `fsync`. It is never
replaced by a normal second run. A later invocation validates the existing
decision and returns only its identity and cutoff, not its performance table.

Because the settlement ledger may legitimately grow after evaluation, existing
decision validation checks the original ledger prefix rather than demanding
that the entire current file remain byte-identical. It requires:

- the current verified chain to contain at least the original row count;
- the record at the original boundary to have the stored head hash;
- recomputation of the exact originally selected window from that prefix;
- exact selected-row count and selected-record-hash digest agreement.

Appending later valid settlements is allowed. Truncating or changing the
decision's evidence prefix fails closed.

## 11. Operational integration and alerts

After each verified settlement cycle, publication invokes only the count-only
readiness command. Its receipt is checked against the collector-pinned
evaluation-config SHA-256 and the settlement receipt's ledger row count.

The watchdog raises critical alerts for:

- readiness subprocess failure or malformed status;
- exposure of performance fields or automatic-decision behavior;
- evaluation-config identity mismatch;
- disagreement between settlement and readiness ledger counts.

It raises a warning when the deterministic cutoff becomes ready. This warning
does not fail the cron because human review and deliberate invocation are part
of the frozen protocol.

A settlement or readiness failure never rewrites immutable evidence, edits the
ledger, removes the previous public champion, or substitutes fabricated data.

## 12. Commands

The safe routine command is count-only:

```bash
.venv/bin/python scripts/check_score_grid_v3_evaluation_readiness.py
```

Before readiness it reports counts and writes `readiness.json`. It is safe to
run repeatedly because it computes no performance aggregate.

After the operational warning says the deterministic cutoff is ready, the
explicit one-shot command is:

```bash
.venv/bin/python scripts/evaluate_score_grid_v3_prospective.py
```

Before readiness this command also returns count-only locked status and writes
no decision. At readiness it calculates the frozen program and creates
`decision.json` once. Afterward it returns the existing decision identity.

No operator should open or summarize raw prospective metric rows before the
one-shot command. The absence of an automatic decision is intentional, not an
unfinished scheduler step.

## 13. Verification coverage

The tests cover:

- exclusion of partial July 2026 and exact seven-day month maturity;
- independent failure of month, fixture, and competition minimums;
- exclusion of gate-ineligible rows from counts;
- proof that automatic readiness accepts a valid envelope without metric
  fields and therefore does not read performance;
- deterministic paired month-cluster bootstrap output;
- fixture-weighted bootstrap point estimates;
- a clear synthetic pass;
- a negative mean whose upper interval crosses zero and therefore fails;
- secondary-metric and moneyline-invariance failures;
- pre-readiness refusal to write a decision;
- write-once refusal to replace an existing decision;
- a full integration run with exactly 4,000 rows: 2,000 per horizon, six
  months, five competitions, both 2,000-replicate bootstraps, decision
  creation, and byte-identical second invocation;
- publication receipt validation, error sanitization, watchdog escalation, and
  collector-config path/hash validation.

Synthetic tests demonstrate that the implementation executes the frozen
logic. They are not evidence that the real challenger is good. Only the future
immutable ledger, evaluated once at the deterministic cutoff, can answer that
question.
