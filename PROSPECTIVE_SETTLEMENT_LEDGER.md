# Regulation Score Grid v3 — Prospective Settlement Ledger

## Status and purpose

The prospective settlement ledger is the outcome-side audit system for
`regulation_score_grid_v3_prospective_shadow`. It is implemented, configured to
run after every successful private shadow publication, and intentionally does
not compute aggregate performance or make a promotion decision.

Its sole job is to join a completed regulation result to the exact immutable
forecast that existed before kickoff, score both the frozen v3 challenger and
its frozen control, price deterministic score-settled contracts, and append a
tamper-evident per-fixture record. Forecast artifacts and settlement artifacts
remain separate. A result can never modify the prediction it evaluates.

The principal files are:

```text
config/models/regulation_score_grid_v3_settlement.json
src/soccer_bot/prospective_evidence.py
src/soccer_bot/prospective_settlement.py
scripts/settle_score_grid_v3_prospective.py
```

Production state is persisted under:

```text
/app/data/predictions/regulation_score_grid_v3_shadow/latest.json
/app/data/predictions/regulation_score_grid_v3_shadow/evidence/<evidence-key>.json
/app/data/predictions/regulation_score_grid_v3_shadow/receipts/<as-of>.json
/app/data/predictions/regulation_score_grid_v3_settlement/ledger.jsonl
/app/data/predictions/regulation_score_grid_v3_settlement/manifest.json
```

The public application continues to expose only the promoted regulation
champion. Neither v3 forecasts nor settlement results are public betting
recommendations.

## 1. Separation of information and outcomes

There are three logically distinct objects:

1. The mutable operational view, `latest.json`, answers “what is the most
   recent shadow forecast set?” It may be replaced by a later cycle.
2. An immutable forecast-evidence record answers “what was the first valid v3
   forecast for fixture (i) and information state (s)?” It is written once.
3. An append-only settlement record answers “what result was later observed,
   and how did that frozen forecast score?” It never edits either of the first
   two objects.

This separation prevents three common forms of leakage:

- repeatedly selecting whichever pre-match snapshot later looks best;
- regenerating a historical forecast after the result is known;
- silently changing a settled row when the warehouse or model artifact changes.

The prospective pairing unit is

\[
K_{i,s}=(\text{fixture\_id}_i,\text{information\_state}_s).
\]

The canonical forecast policy is the first valid immutable evidence record for
that pair. Later five-minute refreshes do not create additional evaluation
rows. They cannot overweight a fixture or create snapshot-selection degrees of
freedom.

## 2. Storage architecture and volume safety

The first implementation wrote a complete approximately 1.1 MB snapshot every
five minutes. At 288 cycles per day, that would consume approximately

\[
1.1\text{ MB}\times 288\approx316.8\text{ MB/day},
\]

before raw provider growth. It would be incompatible with a 10 GB volume and a
six-month prospective gate.

The corrected design stores each full score grid only once per fixture/horizon
pair. A receipt is written only when a cycle contributes at least one new pair.
`latest.json` remains a single replaceable operational file. Storage growth is
therefore proportional to new forecasts, not cron frequency.

Existing timestamped full snapshots are not discarded. On first execution of
the new evidence writer, they are sorted oldest-first and materialized into the
same per-pair evidence format. A durable
`evidence/legacy_import_v1.json` completion marker makes that migration
restart-safe and one-time. If a process stops midway, already-created evidence
is byte-checked and the remaining snapshots can be resumed.

## 3. Forecast-evidence identity

For fixture (i), horizon (s), and frozen logical model hash (M), the
evidence key is

\[
E_{i,s}=\operatorname{SHA256}
\left(\operatorname{JSON}([i,s,M])\right),
\]

where JSON uses compact deterministic serialization. Including (M) makes a
hash collision between different model versions operationally detectable;
the ledger additionally rejects more than one evidence file for the same
((i,s)) pairing.

Every evidence record contains:

- fixture ID and information state;
- first snapshot `as_of` and physical creation time;
- parent prediction cutoff and scheduled kickoff;
- expected home and away goals;
- parent and grid-implied three-way probabilities;
- the complete finite joint regulation-score grid;
- per-grid SHA-256;
- parent snapshot, shadow-model artifact, and prospective-gate source hashes;
- model version and logical model hash;
- prospective gate version and holdout start;
- logical hash of the full originating snapshot;
- logical hash of the evidence record itself.

The evidence writer validates before the first immutable write. It requires:

- timezone-aware timestamps;
- prediction cutoff no later than snapshot `as_of`;
- creation time no earlier than `as_of` and strictly before kickoff;
- `as_of` and kickoff at or after the prospective holdout start;
- positive finite expected-goal rates;
- unique nonnegative integer score cells;
- strictly positive finite cell probabilities summing to one;
- exact score-grid hash agreement;
- positive normalized home/draw/away triplets;
- grid-implied and stored implied moneyline equality to the parent within
  (10^{-10});
- valid lowercase SHA-256 values for all three required source artifacts.

This validation occurs inside the shadow-generation subprocess, before an
invalid candidate could become the canonical first evidence. If the path
already exists, different bytes are rejected rather than overwritten.

## 4. Frozen candidate and control

Let the champion provide expected goals \(\lambda_H,\lambda_A\) and calibrated
moneyline probabilities

\[
m=(m_H,m_D,m_A),\qquad m_H+m_D+m_A=1.
\]

On finite score support \(\mathcal S\), define the independent-Poisson base
measure

\[
q(h,a)=e^{-(\lambda_H+\lambda_A)}
\frac{\lambda_H^h}{h!}\frac{\lambda_A^a}{a!}.
\]

For result region \(R(h,a)\in\{H,D,A\}\), frozen v3 is

\[
p_\theta(h,a)=m_{R(h,a)}
\frac{q(h,a)e^{\theta^\top f(h,a)}}
{\sum_{(u,v):R(u,v)=R(h,a)}q(u,v)e^{\theta^\top f(u,v)}}.
\]

The frozen baseline uses exactly the same expected goals, finite support, and
parent moneyline, with \(\theta=0\):

\[
p_0(h,a)=m_{R(h,a)}
\frac{q(h,a)}
{\sum_{(u,v):R(u,v)=R(h,a)}q(u,v)}.
\]

Both distributions therefore satisfy

\[
\sum_{(h,a):R(h,a)=r}p_\theta(h,a)
=\sum_{(h,a):R(h,a)=r}p_0(h,a)=m_r.
\]

The prospective comparison isolates conditional score shape. A moneyline
improvement cannot drive the result because candidate and baseline inherit the
same parent three-way probabilities by construction.

At settlement time, the candidate is recomputed from the frozen model,
expected-goal rates, parent moneyline, and horizon. Its grid hash must exactly
match the stored evidence hash. A mismatched artifact, changed implementation,
or corrupted evidence file fails the cycle before a record is appended.

## 5. Frozen artifact registry

The settlement recipe was frozen before the first predicted fixture could
finish. It pins:

- logical v3 model SHA-256;
- byte hash of the deployed model artifact;
- prospective gate version and byte hash;
- regulation contract-registry byte hash;
- reviewed result-exclusion byte hash;
- pairing and first-evidence policy;
- scoring probability floor;
- total and handicap reference-line ranges;
- integrity and reporting policy.

The byte hashes prevent a file from retaining the same path or superficial
version label while its contents change. A legitimate future change requires
an explicit new reviewed configuration/version. It cannot silently alter
historical or future records in this ledger.

## 6. Outcome source and eligibility

Settlement opens DuckDB with `read_only=True`. It starts from the
`fixture_model_eligibility` view and joins canonical fixtures to
`fixture_result_observation`. Regulation score models require
`eligible_result_models = TRUE`.

Only observations with:

```text
result_status = final
home_score_regulation is a nonnegative integer
away_score_regulation is a nonnegative integer
```

are valid settlement outcomes. Extra-time and shootout scores are never used.

If several providers report the same regulation score, all agreeing source,
observation, raw-artifact, and retrieval provenance is retained. If providers
report different final scores:

- an exact match to an already reviewed frozen exclusion is skipped;
- a changed reviewed conflict fails closed;
- any new unreviewed conflict fails closed.

Invalid final scores fail closed. A forecast fixture missing from the
eligibility view also fails rather than remaining silently pending. A fixture
with no final result remains pending and is reconsidered in the next cycle.
An ineligible fixture is not appended and may be reconsidered if its canonical
eligibility is later corrected.

## 7. Temporal integrity vector

For each settled row, the ledger stores each condition separately and the
conjunction

\[
G_{i,s}=\bigwedge_j C_{i,s,j}.
\]

`eligible_for_prospective_gate` is true only when every condition is true:

- result-model eligibility is true;
- forecast kickoff is at or after the frozen holdout start;
- current canonical kickoff equals forecast kickoff;
- prediction cutoff is no later than first snapshot `as_of`;
- physical snapshot creation is no earlier than snapshot `as_of`;
- the cutoff is exactly 24 hours for `pre_lineup_24h_v1` or exactly 72 hours
  for `pre_lineup_72h_clean_v1`;
- first snapshot `as_of` and physical creation are both strictly pre-kickoff;
- earliest valid result retrieval is after forecast creation and kickoff;
- settlement execution is at or after every result retrieval recorded;
- model and gate identities match;
- stored and recomputed grid hashes match.

A temporal anomaly does not cause the system to rewrite or erase factual
history. The row is retained with its failed condition and excluded from the
future gate. This makes anomalies auditable without contaminating evidence.

## 8. Per-row proper scoring rules

Let the realized regulation score be \(y=(h^*,a^*)\), and let \(p\) denote
either candidate or baseline. The frozen probability floor is
\(\varepsilon=10^{-15}\). It avoids infinite serialized values when the
realized score lies beyond finite numerical support; it does not redistribute
probability mass.

### 8.1 Exact-score log loss

\[
L_{\text{score}}(p,y)
=-\log\max\{p(h^*,a^*),\varepsilon\}.
\]

This is the primary prospective-gate metric.

### 8.2 Marginal log losses

Define

\[
p_H(k)=\sum_a p(k,a),\quad
p_A(k)=\sum_h p(h,k),
\]

\[
p_T(t)=\sum_{h+a=t}p(h,a),\quad
p_D(d)=\sum_{h-a=d}p(h,a).
\]

The ledger records

\[
-\log\max\{p_H(h^*),\varepsilon\},\quad
-\log\max\{p_A(a^*),\varepsilon\},
\]

\[
-\log\max\{p_T(h^*+a^*),\varepsilon\},\quad
-\log\max\{p_D(h^*-a^*),\varepsilon\}.
\]

It also stores the corresponding realized-category probabilities.

### 8.3 Moneyline and BTTS

For categorical moneyline probabilities \(m_r\), the ledger records log loss
and multiclass Brier score

\[
L_{1X2}=-\log m_{r^*},\qquad
B_{1X2}=\sum_{r\in\{H,D,A\}}
(m_r-\mathbf1\{r=r^*\})^2.
\]

For BTTS probability \(b=P(H>0,A>0)\), it records binary log loss and

\[
B_{\text{BTTS}}=(b-z)^2,
\]

where \(z=1\) exactly when both realized scores are positive.

### 8.4 Ranked probability score

For an ordered discrete distribution with CDF \(F(k)\) and realized category
\(y\), the ledger uses

\[
\operatorname{RPS}(F,y)=
\sum_{k=k_{\min}}^{k_{\max}-1}
\left(F(k)-\mathbf1\{y\le k\}\right)^2.
\]

It records RPS for total goals and goal difference. Candidate and baseline use
the same finite support, so their paired differences are directly comparable.

For every metric whose name ends in `_log_loss`, `_brier`, or `_rps`, the
stored delta is

\[
\Delta=L_{\text{candidate}}-L_{\text{baseline}}.
\]

Negative values favor v3. The sign convention is fixed and never inferred at
report time.

## 9. Deterministic contract settlement

The candidate and baseline grids are separately projected through the shared
`ScoreGrid` contract engine. The frozen reference ranges are:

- total goals from 0.5 through 6.5 in 0.25 increments;
- selected-team goal handicap from -2.5 through +2.5 in 0.25 increments.

For each line and selection, the ledger stores the forecast probability over

```text
win, half_win, push, half_loss, loss
```

and the deterministic realized outcome. Total-goal sides are over and under;
handicap selections are home and away.

Quarter lines are settled by splitting the stake equally across the adjacent
half-step lines. For example, over 2.25 is half over 2.0 and half over 2.5.
The two leg outcomes combine into the five-state settlement distribution. The
realized distribution is generated by passing a unit-mass score grid at
\((h^*,a^*)\) through the same code, avoiding a second handwritten settlement
implementation.

These are probability settlements, not P&L. No bookmaker price, fee, slippage,
liquidity, stake, or executable market timestamp is implied.

## 10. Append-only hash chain

Each JSONL record stores `previous_record_sha256` and `record_sha256`. If
\(J_n\) is deterministic compact JSON of record (n) excluding its own record
hash, then

\[
H_n=\operatorname{SHA256}(J_n),\qquad
J_n.\text{previous}=H_{n-1}.
\]

The first previous hash is null. Before every update, the complete chain is
validated for:

- parseable object records;
- frozen ledger version;
- previous-hash continuity;
- exact logical record hash;
- unique evidence key;
- unique fixture/information-state pair.

New records are sorted deterministically by kickoff, fixture ID, and horizon.
The writer preserves the existing bytes, adds complete newline-terminated
records in a temporary file, flushes and `fsync`s, and atomically replaces the
ledger. The file is then read and verified again. A partial record cannot be
accepted as a valid append.

The chain is tamper-evident, not a digital signature. An adversary with write
access could recompute an unkeyed chain; production filesystem access and
backups remain part of the trust boundary. The chain's purpose is to detect
accidental edits, truncation, reordering, duplication, and ordinary corruption.

## 11. No-rewrite behavior

Once a pairing key is in the ledger, future cycles do not query or rescore it.
Even if a warehouse result is later changed, the ledger bytes remain unchanged.
Any correction must be represented by a separately designed correction/audit
artifact; it cannot rewrite prospective history.

This policy trades automatic correction for evidentiary clarity. The result
provenance stored in the original row identifies exactly which observations
were available when settlement occurred.

## 12. Manifest and premature-analysis guard

`manifest.json` is a mutable operational summary of ledger identity and chain
head. It contains no mean score, confidence interval, bootstrap, or gate
decision. Every settlement receipt explicitly reports:

```text
performance_aggregates_written: false
gate_decision_written: false
```

The operational watchdog treats a settlement failure as critical and also
raises `premature_prospective_evaluation_output` if either flag is not exactly
false. This is an intentional anti-peeking control. Per-fixture scores are
needed to build future evidence, but aggregate inference is deferred until the
frozen minimum of six complete calendar months, 2,000 fixtures per horizon,
and five competitions per horizon is satisfied.

## 13. Collector execution order and failure isolation

After DuckDB is closed, one successful production cycle performs:

1. champion generation and validation;
2. public object upload and read-back verification;
3. private v3 generation;
4. first-evidence materialization and `latest.json` update;
5. read-only prospective settlement;
6. append-only publication receipt;
7. operational watchdog evaluation.

Settlement uses its actual invocation time, not the champion's earlier
information cutoff, because provider observations may be written during the
collection phase of that same cycle.

A shadow or settlement failure cannot undo a verified public champion upload.
It is nevertheless operationally critical: the watchdog exits the cron with
code 3 so missing evidence cannot pass silently. No stderr, environment value,
credential, or provider response body is copied into public or operational
receipts.

## 14. Testing and adversarial cases

The automated suite covers:

- first valid evidence wins and later refreshes are ignored;
- oldest-first migration of legacy snapshots;
- corrupt evidence-record hashes;
- invalid grids rejected before their first write;
- exact candidate reconstruction and parent-moneyline preservation;
- baseline construction with zero tilt;
- finite and arithmetically correct per-row metrics;
- deterministic total and handicap outcomes;
- repeated execution without duplicate append;
- later warehouse changes without ledger rewrite;
- ledger hash tampering;
- duplicate evidence for one pairing key;
- frozen artifact hash mismatch;
- nonfinal pending fixtures;
- ineligible results;
- unreviewed provider conflicts;
- result retrieval before forecast or kickoff;
- settlement timestamp before result retrieval;
- kickoff revision after prediction;
- settlement subprocess failure isolation;
- critical operational alerting and premature-analysis detection.

## 15. Commands

Run the settlement updater directly:

```bash
.venv/bin/python scripts/settle_score_grid_v3_prospective.py
```

Run focused verification:

```bash
.venv/bin/python -m unittest \
  tests.test_prospective_evidence \
  tests.test_prospective_settlement \
  tests.test_prediction_publication \
  tests.test_operational_alerts -v
```

Run the complete repository validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/run_collector.py --dry-run
git diff --check
```

## 16. What remains deliberately deferred

This ledger does not yet:

- aggregate rows by horizon or calendar month;
- run the paired month-block bootstrap;
- declare pass, fail, or promotion;
- compare against timestamped market prices;
- estimate executable betting P&L;
- settle player, scorer-order, or corner contracts;
- expose v3 through the public web service.

Those are separate stages with separate leakage risks. The next prospective
evaluation program should read only hash-verified, gate-eligible ledger rows
after the frozen evidence minimum is actually reached.
