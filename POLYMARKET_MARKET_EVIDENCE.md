# Polymarket Market Evidence — Frozen Collection and Execution Specification

## 1. Purpose and status

This document specifies `polymarket_regulation_market_evidence_v1`, the
outcome-blind Polymarket evidence layer that accompanies the independent soccer
forecast. It is not a new predictive model, not a market-aware model, and not a
trading system. It exists to answer a narrower scientific question correctly:

> What executable Polymarket information was actually visible before the exact
> timestamp of a frozen soccer prediction, under a semantically identical
> regulation-time contract?

The production collector now preserves the inputs needed to answer that
question:

- public Gamma event, market, rules, outcome, token, liquidity, and fee-enabled
  metadata;
- public CLOB order books, including every returned bid and ask level;
- the provider book hash, last trade, tick size, minimum order size, negative
  risk flag, raw artifact identity, and immutable retrieval timestamp;
- the exact intended prediction cutoff, capture-window start, deadline,
  schedule identity known at retrieval, and a fail-closed timing-valid flag;
- a versioned accepted or rejected semantic mapping for every linked market;
- immutable prediction/book evidence for complete regulation moneylines;
- count-only coverage reports and operational alerts.

The frozen policy is
`config/contracts/polymarket_regulation_v1.json`. Its canonical JSON SHA-256 is
`e11ebe375845ec8293249730b889478a907e102abf401f87cfbcc96b8f4f900b`.
Changing a phrase, threshold, fee rule, horizon, or execution size changes that
identity and requires a new policy version.

Polymarket's market-data endpoints are public. This implementation needs no
Polymarket API key, wallet, private key, signature, trading credential, user
position, or order permission. It never calls an order endpoint. The relevant
provider references are Polymarket's [market-data
overview](https://docs.polymarket.com/market-data/overview), [CLOB market-info
schema](https://docs.polymarket.com/api-reference/markets/get-clob-market-info),
and [fee specification](https://docs.polymarket.com/trading/fees).

## 2. Separation from the forecasting model

The production champion remains `regulation_champion_v1`. Its inputs are
soccer results, chronological team state, Understat xG, API-Football shots, and
frozen calibration. Polymarket prices are not features in this model. The
boundary is deliberate:

```text
soccer evidence ──> independent champion ──> frozen probabilities
                                                │
public Polymarket books ──> immutable evidence ─┤ comparison only
                                                │
realized result ──────────> separate settlement ledger later
```

The market-evidence artifact contains the model probabilities because a
comparison requires them, but the dependency is one-way: the prediction is
already frozen before the book is paired. No market field can change the
champion probability, expected-goal rate, model hash, or training state.

This distinction supports three different future claims, which must never be
collapsed:

1. **Predictive accuracy:** whether probabilities forecast realized outcomes
   under proper scoring rules.
2. **Calibration:** whether events assigned probability \(p\) occur at rate
   approximately \(p\).
3. **Executable market value:** whether a model-market disagreement remains
   after spread, visible depth, fees, slippage, liquidity, and selection
   effects.

A favorable model score alone does not prove an executable betting edge. A
positive theoretical book-walk value is also not proof of a realizable strategy:
the visible book can change before execution, fills can be partial, and the
observed set of listed fixtures is selected rather than random.

## 3. Exact timing contract

### 3.1 Prediction anchors

Let:

- \(K\) be the current canonical kickoff;
- \(h\) be a horizon in minutes;
- \(C_h = K-h\) be the exact prediction cutoff;
- \(W=16\) minutes be the frozen market-capture window.

For T−72h, \(h=4320\). For T−24h, \(h=1440\). A timed book is eligible only if

\[
C_h-W \le t_{retrieve} < C_h.
\]

The right boundary is strict. A response retrieved exactly at \(C_h\), one
microsecond after it, or after kickoff is ineligible. This is stronger than
merely checking that a book was pre-kickoff.

The old schedule opened its window at \(C_h\), which meant a stage labeled
T−24h was normally collected after the T−24h model cutoff. Version 1 corrects
that chronology: the job opens 16 minutes before the cutoff and closes at the
cutoff.

### 3.2 Retry geometry

Railway invokes the run-once collector every five minutes. A failed or
incomplete capture is retried after five minutes, with at most three attempts.
The configuration invariant is

\[
W > r(A-1),
\]

where \(r=5\) minutes and \(A=3\). Thus all configured attempts can occur
strictly before the deadline when the first attempt starts at the window's
left boundary. Startup validation rejects a configuration that cannot satisfy
this inequality.

An unsuccessful response outside the interval is still retained as raw
evidence. Its normalized snapshot records
`capture_timing_valid=false` and
`retrieval_outside_frozen_capture_window`; it cannot satisfy an evidence query.
No later response is relabeled as an earlier stage.

### 3.3 Schedule identity

Timing eligibility also requires

\[
K_{known\ at\ retrieval}=K_{prediction}.
\]

This prevents a book captured for an obsolete kickoff from being paired with a
prediction for a rescheduled fixture. Market checkpoint keys include the
schedule version, so a material kickoff revision produces new jobs rather than
silently reusing the old stage.

### 3.4 Snapshot comparability

The three binary Yes tokens used for a regulation moneyline need not fall in
the same HTTP batch when a request contains hundreds of tokens. They must,
however, be individually valid and have retrieval timestamps whose maximum
pairwise span is no more than 15 seconds:

\[
\max_i t_i-\min_i t_i \le 15\text{ seconds}.
\]

This bound is frozen in policy and recorded in each evidence artifact.

## 4. Semantic contract mapping

### 4.1 Why titles are insufficient

“Team A to win” can mean regulation only, qualification, extra time included,
or a voidable event under different postponement rules. Similar display text
is not semantic equivalence. A market enters the accepted mapping table only
when all of the following agree:

- its linked canonical fixture;
- provider `sportsMarketType`;
- exact question grammar;
- home/away team identity after conservative club-prefix/suffix normalization;
- line value and the line embedded in the question;
- complete expected outcome set;
- explicit regulation-plus-stoppage-time language in preserved rules.

Anything else gets an immutable rejected decision with a typed reason. The
system does not use fuzzy probability thresholds to turn ambiguity into a
mapping.

### 4.2 Supported canonical contracts

| Provider type | Canonical contract | Required interpretation |
|---|---|---|
| `moneyline` | `regulation_moneyline` | home win, draw, or away win in regulation |
| `totals` | `regulation_total_goals` | over/under the exact match-total line |
| `spreads` | `regulation_goal_handicap` | named-team line converted to a home handicap |
| `soccer_team_totals` | `regulation_team_total_goals` | home/away team and exact total line |
| `both_teams_to_score` | `regulation_both_teams_to_score` | both score at least once in regulation |
| `soccer_exact_score` | `regulation_exact_score` | explicit \((H,A)\) or provider “other score” bucket |

First-half, second-half, qualification, extra-time, shootout, player, corner,
and first-to-score types are deliberately rejected by this version even if the
collector stores their books. Supporting one requires a matching canonical
prediction contract and a new reviewed mapping policy.

### 4.3 Handicap sign convention

The canonical parameter is always the home-team handicap. If the question is
`Spread: Home (-1.5)`, then \(h_{home}=-1.5\). If the question is
`Spread: Away (-1.5)`, the equivalent home parameter is
\(h_{home}=+1.5\). Both provider outcomes must independently match the
fixture's home and away teams. A line or name disagreement rejects the
contract.

### 4.4 Binary polarity

Polymarket commonly represents each moneyline selection or exact score as a
separate Yes/No binary market. The mapping stores both outcomes:

```text
Yes -> canonical selection, polarity +1
No  -> same canonical selection, polarity -1
```

The complete three-way consensus uses only the positive-polarity Yes token for
`home_win`, `draw`, and `away_win`. It requires exactly one accepted market for
each. Missing or duplicate selections fail closed.

### 4.5 Mapping immutability

`polymarket_contract_mapping` has a uniqueness constraint on
`(prediction_market_id, mapping_version)`. Once a decision is written, later
provider text cannot rewrite it. A changed interpretation needs a new mapping
version. Each decision stores the policy hash, rules-text hash, fixture,
provider type, canonical parameters, decision time, and rejection reason.

## 5. Order-book normalization

For token \(j\), a snapshot stores ordered price-size pairs

\[
B_j=\{(p^b_{j\ell},q^b_{j\ell})\}, \qquad
A_j=\{(p^a_{j\ell},q^a_{j\ell})\}.
\]

Prices must be finite and strictly inside \((0,1)\); sizes must be finite and
positive. The normalized top of book must equal

\[
b_j=\max_\ell p^b_{j\ell}, \qquad
a_j=\min_\ell p^a_{j\ell}.
\]

An evidence-eligible book is two-sided, non-crossed \(b_j\le a_j\), and has
spread \(s_j=a_j-b_j\le 0.20\). The wide 20-cent ceiling is a data-quality
gate, not a declaration that such a book is attractive to trade. Exact spread
and depth remain in the artifact.

One normalized snapshot is keyed by token plus raw-artifact identity. Repeated
identical book contents therefore remain distinct retrievals; a content hash
cannot collapse their different timing provenance.

## 6. Market consensus calculation

For each of the three positive moneyline tokens, define the midpoint

\[
m_i=\frac{b_i+a_i}{2}.
\]

Because independent binary markets can sum to more or less than one, the
diagnostic no-vig consensus is

\[
\tilde p_i=\frac{m_i}{\sum_{k\in\{H,D,A\}}m_k}.
\]

The recorded disagreement is
\(\Delta_i=p_i^{model}-\tilde p_i\). This normalized midpoint is suitable for a
predictive benchmark. It is not an execution price: a buyer pays asks and
moves through the visible ladder.

## 7. Depth-aware execution mathematics

### 7.1 Ask-ladder walk

The frozen diagnostic considers an immediate taker purchase of the selected
Yes token for target share quantities \(Q\in\{10,50,100,250\}\).

Sort asks by ascending price. At level \(\ell\), fill

\[
x_\ell=\min\left(q^a_\ell,
Q-\sum_{r<\ell}x_r\right).
\]

Visible gross cost and volume-weighted average price are

\[
C_{gross}=\sum_\ell x_\ell p^a_\ell,
\qquad
VWAP=\frac{C_{gross}}{\sum_\ell x_\ell}.
\]

Displayed slippage is \(VWAP-a_1\). A quote is not fully executable when
\(\sum x_\ell<Q\), or when \(Q\) is below the provider minimum order size.

### 7.2 Fees

Polymarket documents that makers are not charged and that enabled sports-market
taker fees use rate \(r=0.03\) with a price-dependent curve. At each fill this
implementation calculates

\[
F_\ell=x_\ell r p^a_\ell(1-p^a_\ell),
\qquad F=\sum_\ell F_\ell.
\]

The fee is zero only when point-in-time Gamma metadata explicitly says fees are
disabled. If the field is unknown, the artifact may preserve the book and
gross VWAP, but net cost and expected profit are `NULL` and the quote is not
economically eligible. It never assumes that an unknown fee is zero.

Maker rebates are excluded because the diagnostic is a taker buy. Deposit,
withdrawal, intermediary, network, opportunity, and latency costs are not
included and are explicitly marked as limitations.

### 7.3 Model-implied expected value

For a fully filled binary share paying one unit if the selection wins, model
expected payout is \(E[Payout]=Qp_i^{model}\). The frozen theoretical expected
profit is

\[
E[\Pi]=Qp_i^{model}-C_{gross}-F.
\]

This is an audit statistic, not an instruction or realized return. It omits
future book movement, queue position, rejected orders, adverse selection,
capital constraints, and all non-platform costs noted above.

## 8. Immutable evidence protocol

For each champion row, the writer attempts to build one evidence record keyed
by evidence version, fixture ID, information-state/horizon, exact prediction
timestamp, champion logical-model hash, and market-policy hash.

The first valid record is written using a temporary file, `fsync`, and an
atomic no-replace link. Later runs validate the existing identity and do not
overwrite it, even if a later database query would select a different book.
This prevents hindsight from improving a historical forecast's market pairing.

Every selection contains:

- prediction, model, snapshot, mapping, rules, raw-artifact, and book hashes;
- token, outcome, mapping, market, and snapshot IDs;
- observed, retrieved, target, window, deadline, kickoff, and capture-skew
  timestamps;
- full bids and asks plus top-of-book metadata;
- fee status and depth-walk outputs at every frozen size;
- model probability, no-vig market probability, and their difference.

It contains no realized score, winner, settlement status, scoring rule,
aggregate accuracy, ROI, order, position, wallet, or account identifier.

Persistent paths are:

```text
data/predictions/polymarket_market_evidence_v1/
├── evidence/<fixture_id>/<evidence_id>.json   # immutable
├── coverage.json                              # atomic current count view
└── receipts.jsonl                             # append-only cycle receipts
```

## 9. Count-only coverage and anti-peeking policy

For each horizon, `coverage.json` reports only a funnel:

1. champion prediction rows;
2. rows with exactly three complete accepted moneyline mappings;
3. rows with complete timing-safe pre-cutoff books;
4. rows passing two-sided spread/depth validation;
5. immutable evidence records;
6. records executable at every frozen share size with known fee status.

Typed exclusion counts explain attrition. The automatic cycle does not compute
realized accuracy, profit, calibration, model ranking, or a promotion decision.
Those belong to a separately frozen prospective evaluation after enough
evidence accumulates. This prevents repeatedly inspecting early results and
tuning the model or selection rules to the same prospective sample.

Zero Polymarket listings are not an incident. A prediction row with no mapped
market simply records incomplete coverage. A warning is raised when a complete
mapping exists but its required pre-cutoff books are missing; that condition
means collection failed to capture an available comparison opportunity.

## 10. Operational failure behavior

The market-evidence step runs after the independent champion snapshot has been
validated and uploaded. It is failure-isolated so it cannot replace or corrupt
the champion object. Its failure is nevertheless operationally critical: the
watchdog exits the cycle with code 3 after all valid collection writes are
committed.

Critical conditions include:

- evidence subprocess or receipt failure;
- configured and observed policy-hash mismatch;
- negative, impossible, or internally inconsistent coverage counts;
- any claim that result/performance fields were written;
- any claim that an order or trading action occurred.

The capture-gap condition is a warning. It should be investigated before the
next affected cutoff, but it does not imply corrupt soccer predictions.

## 11. Verification commands

Run the focused tests:

```bash
.venv/bin/python -m unittest \
  tests.test_polymarket_collector \
  tests.test_polymarket_evidence \
  tests.test_prediction_publication \
  tests.test_operational_alerts -v
```

Run the complete project validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

The standalone pairing command is read-only with respect to DuckDB:

```bash
.venv/bin/python scripts/capture_polymarket_market_evidence.py \
  --warehouse data/warehouse/soccer.duckdb \
  --snapshot data/predictions/regulation_champion_v1/latest.json \
  --policy config/contracts/polymarket_regulation_v1.json \
  --expected-policy-sha256 \
    e11ebe375845ec8293249730b889478a907e102abf401f87cfbcc96b8f4f900b \
  --output-dir data/predictions/polymarket_market_evidence_v1
```

## 12. Controlled limitations and next research step

- Immutable champion pairing currently requires a complete regulation
  moneyline because that is the champion's supported calibrated output.
- The collector and semantic layer already preserve regulation totals, spreads,
  team totals, BTTS, and exact scores. Pairing those contracts to the coherent
  score-grid shadow is a separate versioned step; it must preserve the shadow's
  prospective anti-peeking gate.
- Confirmed-lineup market evidence is not claimed by this version. A future
  lineup model needs a precisely ordered protocol: lineup becomes known, model
  prediction is frozen, and a separately timestamped comparison book is
  captured without pretending it preceded the prediction.
- No current evidence artifact proves profitability. A later prospective
  settlement/evaluation program must be frozen before inspecting its metrics
  and must address missing-market selection, spread, depth, fees, slippage,
  latency, and liquidity jointly.

The immediate operational task is therefore accumulation, not tuning: keep the
collector healthy through real T−72h and T−24h windows, audit capture-gap
warnings, and allow immutable evidence to build before opening any performance
analysis.
