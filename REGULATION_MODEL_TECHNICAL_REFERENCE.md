# Soccer Bot Regulation Model — Quantitative Technical Reference

Status: implementation-grounded reference for production `regulation_champion_v1` and prospective `regulation_score_grid_v3_prospective_shadow`

Repository state reviewed: 2026-07-17

Production artifact described: local all-history refit created 2026-07-14/15

Primary supported public production output: regulation-time three-way moneyline
Private prospective shadow output: coherent regulation score grid and all deterministic score-settled CORE projections

## 1. Scope and the shortest correct description

The current production model is a **dynamic, online, opponent-adjusted soccer
goal model**. It estimates a home and away scoring intensity, corrects those
intensities using chronological xG and shot histories, turns the two intensities
into an independent-Poisson score distribution, aggregates that distribution
into home/draw/away probabilities, and finally temperature-calibrates the
three-way probabilities.

In compact notation, for fixture \(i\) at information cutoff \(t_i\):

\[
\begin{aligned}
\lambda^{\text{state}}_{H,i}
  &= \exp\{\mu_{c,t_i}+\gamma_{c,t_i}
      +\alpha_{H,t_i}-\delta_{A,t_i}\},\\
\lambda^{\text{state}}_{A,i}
  &= \exp\{\mu_{c,t_i}
      +\alpha_{A,t_i}-\delta_{H,t_i}\},\\
\lambda^{\text{base}}_{H,i}
  &= s_H\lambda^{\text{state}}_{H,i},\\
\lambda^{\text{base}}_{A,i}
  &= s_A\lambda^{\text{state}}_{A,i},\\
\lambda_{H,i}
  &= \lambda^{\text{base}}_{H,i}
     \exp\{\beta_x z^{xG}_{H,i}+\beta_s z^{shots}_{H,i}\},\\
\lambda_{A,i}
  &= \lambda^{\text{base}}_{A,i}
     \exp\{\beta_x z^{xG}_{A,i}+\beta_s z^{shots}_{A,i}\},\\
H_i&\sim\operatorname{Poisson}(\lambda_{H,i}),\\
A_i&\sim\operatorname{Poisson}(\lambda_{A,i}),\qquad H_i\perp A_i,\\
q_r&=\sum_{(h,a)\in\mathcal S_r}P(H_i=h,A_i=a),\\
p_r&=\frac{q_r^{1/T}}{\sum_k q_k^{1/T}},
\qquad r\in\{\text{home},\text{draw},\text{away}\}.
\end{aligned}
\]

Here:

- \(\alpha\) is latent attacking strength;
- \(\delta\) is latent defensive strength, subtracted in the log-rate;
- \(\mu_c\) is a competition-specific log goal level;
- \(\gamma_c\) is competition-specific home advantage and is zeroed at a
  neutral venue;
- \(s_H,s_A\) are global home/away rate corrections;
- \(z^{xG},z^{shots}\) are coverage-weighted matchup signals;
- \(T\) is a horizon-specific temperature.

The estimator is implemented directly in Python. It is not a scikit-learn,
PyTorch, XGBoost, or Stan model. The dynamic state updates are diagonal
Laplace/Fisher-scoring-style Bayesian updates; the rich-rate layer is a
two-coefficient ridge-penalized Poisson regression solved by Newton steps with
backtracking; calibration is a one-parameter bounded optimization.

The model is deliberately narrow. It currently does **not** use confirmed
lineups, player identities, injuries, market prices, rest, congestion, or the
stored posterior standard deviations in its final scoring formula. Some of
those fields are collected or emitted as diagnostics and some belong to future
model families, but they are not hidden inputs to `regulation_champion_v1`.

A second, strictly non-production layer is now frozen for prospective
evaluation. It accepts the champion rates and calibrated moneyline and returns
a coherent joint score law

\[
\pi_{h,a}=p_{R(h,a)}
\frac{q_{h,a}\exp\{\theta^\top f(h,a)\}}
{\sum_{(i,j):R(i,j)=R(h,a)}q_{i,j}\exp\{\theta^\top f(i,j)\}},
\]

where (R(h,a)) is home win, draw, or away win. The three result regions are
normalized separately. Consequently, summing the v3 grid over each result
region reproduces the incumbent calibrated moneyline exactly, while
\(\theta\) changes only the conditional distribution of exact scores inside
each region. This removes v1's cross-contract incoherence without asking the
score layer to relearn or approximate the promoted result probabilities.

V3 is an operational **shadow**, not a promoted model. Its historical rows are
fit inputs, its decision gate was frozen before the first eligible shadow
prediction, and only predictions created after the 2026-07-17 freeze and
strictly before kickoff may become evaluation evidence.

## 2. What is production, what is only infrastructure, and what is future work

It is important not to conflate three layers of the repository.

### 2.1 Production model

`regulation_champion_v1` supports:

- regulation time only, including stoppage time;
- no extra time and no penalty shootout;
- pre-lineup T−24h predictions;
- clean pre-lineup T−72h predictions;
- calibrated home/draw/away probabilities.

It is an independent-Poisson xG/shots-corrected rate model with three-way
temperature calibration.

### 2.2 Implemented probability, shadow, and settlement infrastructure

The repository also implements a validated joint score-grid abstraction and
deterministic pricing for:

- exact score;
- regulation moneyline;
- goal handicaps, including integer, half, and quarter lines;
- match totals;
- team totals;
- both teams to score.

Those transformations are mathematically implemented and tested. Public
production still exposes only the promoted calibrated moneyline. The v3 shadow
now supplies a coherent calibrated score grid privately by preserving those
three probabilities as hard marginals and distributing each marginal across
its compatible score cells. V3 therefore supports research prices for every
CORE score-settled family, but those prices remain shadow outputs until the
frozen prospective gate passes. They are not yet public recommendations or
evidence of betting edge.

### 2.3 Declared but not yet modeled

The contract and system designs reserve a `confirmed_lineup_v1` information
state, player goal/assist engines, corner engines, first-score timing, period
scores, qualification, and market-aware fusion. These are roadmap items, not
features silently present in the current champion.

## 3. End-to-end process

The implemented path is:

```text
immutable provider responses
        ↓
canonical DuckDB observations and canonical entity IDs
        ↓
fixture_model_eligibility
        ↓
one reviewed regulation-score target per fixture
        ↓
chronological T−72h-clean and T−24h team-state snapshots
        ↓
chronological Understat-xG and API-Football-shots snapshots
        ↓
expanding-window independent-Poisson and Dixon–Coles evaluation
        ↓
development-only rich-feature selection
        ↓
calibration-year temperature fit
        ↓
one frozen final-test evaluation
        ↓
all-eligible-history production refit
        ↓
read-only upcoming-fixture replay and schedule gates
        ↓
validated immutable JSON snapshot
        ↓
private API and public probability desk

post-freeze validated champion snapshot
        ↓
result-marginal-preserving conditional score tilt
        ↓
immutable private shadow score grid
        ↓
exact score / handicap / totals / team totals / BTTS projections
        ↓
prospective-only paired evaluation after the frozen evidence minimum
```

The key principle is that every historical row represents a forecast that
could have been made at an exact historical timestamp. A row is not merely a
completed match with present-day aggregates attached.

## 4. Evidence, canonicalization, and model eligibility

### 4.1 Data sources relevant to this model

The warehouse contains observations from API-Football, Football-Data.co.uk,
Understat, StatsBomb Open Data, and Polymarket. Their present roles differ:

| Source | Current role in the regulation model |
|---|---|
| API-Football | Results, schedules, team shots, current fixture discovery, and future lineup/player work |
| Football-Data.co.uk | Long team/result history and retrospective closing-odds benchmark |
| Understat | Chronological team xG enrichment for supported competitions |
| StatsBomb Open Data | Canonical historical evidence and future event/player research; not a distinctive current champion feature |
| Polymarket | Timestamped market audit; not a champion feature |

Provider observations are mapped to canonical fixture, competition, team, and
player IDs. Cross-provider joins are not performed by display name. Raw source
artifacts remain immutable; repairs occur in normalized data with provenance
and narrow guards.

### 4.2 The eligibility view

All target construction starts from `fixture_model_eligibility`. It exposes:

- `eligible_result_models`: a non-administrative played fixture has at least
  one valid final nonnegative regulation score;
- `eligible_team_models`: result eligibility plus a coherent two-team core
  statistics block with valid shots, shots on target, and corners;
- `eligible_player_models`: result eligibility plus two complete confirmed
  starting elevens and at least 22 valid positive-minute participants linked
  to those lineups from one provider artifact.

The current regulation target requires only `eligible_result_models`. This is
intentional: result modeling should not discard a valid score merely because
player or corner data are absent. Rich xG/shots observations are optional
point-in-time evidence; missing rich evidence leaves a prior-based signal
rather than fabricating a zero.

In the local warehouse snapshot reviewed for this document there are 38,625
fixtures, of which 38,449 are result eligible, 34,599 team eligible, and 23,592
player eligible. Four result-eligible fixtures have reviewed cross-provider
score conflicts, leaving 38,445 frozen regulation targets. These local counts
must not be treated as current Railway production counts; the production
collector continues to change the cloud warehouse.

### 4.3 Target construction

For each result-eligible fixture, the target builder reads all valid final
regulation score observations. It accepts the fixture only if all observed
valid provider scores agree:

\[
y_i=(y_{H,i},y_{A,i}).
\]

It derives:

\[
\begin{aligned}
R_i &\in \{H,D,A\},\\
G_i &= y_{H,i}+y_{A,i},\\
D_i &= y_{H,i}-y_{A,i},\\
BTTS_i &= \mathbf 1(y_{H,i}>0\land y_{A,i}>0).
\end{aligned}
\]

Provider precedence is not used to make a conflict disappear. Four reviewed
conflicts are excluded only while the exact configured score sets still match
the warehouse. A new conflict, a changed conflict, or an exclusion whose
evidence no longer matches fails the build.

Administrative results are excluded. Negative scores, incomplete final
scores, and non-final observations cannot become targets. Regulation semantics
include stoppage time and exclude extra time and shootouts.

## 5. Information states and point-in-time semantics

### 5.1 T−24h

For kickoff \(k_i\):

\[
t_i^{24}=k_i-24\text{ hours}.
\]

Every target has one T−24h row. It is a standardized, comparable pre-lineup
anchor—not a claim that a distinct model should be fit for each clock hour.

### 5.2 Clean T−72h

For kickoff \(k_i\):

\[
t_i^{72}=k_i-72\text{ hours}.
\]

The row is retained only if neither participating team has another fixture
with kickoff in:

\[
[t_i^{72},k_i).
\]

A fixture kicking off exactly at the T−72h anchor blocks the row because its
result is not available at that instant. This rule prevents the meaning of the
"T−72h" feature state from changing because a team plays in the intervening
three days.

The all-history feature artifact contains:

| Information state | Rows |
|---|---:|
| T−24h | 38,445 |
| Clean T−72h | 34,813 |
| Total | 73,258 |

### 5.3 Result availability embargo

A result from a match at kickoff \(k_j\) becomes model-available at:

\[
a_j=k_j+150\text{ minutes}.
\]

This is a conservative operational availability rule. Feature and evaluation
event loops process predictions before results at the same timestamp. Thus a
result whose availability time equals a prediction cutoff is still not visible
to that prediction.

Simultaneous results are accumulated and applied as a batch. No fixture in the
batch sees an update from another fixture in the same batch, and reversing
input order produces the same logical artifact hash.

### 5.4 Schedule knowledge during upcoming inference

An upcoming row must pass a stronger gate than simply having a current kickoff:

1. The fixture is currently `scheduled`.
2. Its current kickoff is after `as_of` and no more than seven days ahead.
3. The horizon is due: its exact anchor is at or before `as_of`.
4. A schedule observation existed at or before that exact anchor.
5. The latest such observation said `scheduled` and contained the same kickoff
   as the current canonical fixture.
6. The clean T−72h intervening-fixture rule passes.

Consequently, a fixture discovered after its nominal T−72h anchor does not
receive a retrospectively manufactured T−72h prediction. A rescheduled fixture
whose old cutoff did not know the current kickoff also fails closed.

## 6. Dynamic team and competition state model

### 6.1 State variables

Each canonical team \(j\) has two scalar latent states:

\[
\alpha_j(t)=\text{attack strength},\qquad
\delta_j(t)=\text{defense strength}.
\]

Each competition \(c\) has:

\[
\mu_c(t)=\text{log goal level},\qquad
\gamma_c(t)=\text{home advantage}.
\]

The states are keyed by team and competition, not by season. Team state is
global across the covered competitions in which that canonical team appears;
competition state is shared across seasons of that competition. Time decay is
the mechanism that reduces stale influence. There is no explicit promotion,
transfer-window, season-reset, or cross-competition translation parameter in
v1.

### 6.2 Priors

Team attack and defense begin at:

\[
\alpha_j,\delta_j\sim N(0,0.45^2).
\]

Competition goal level begins at:

\[
\mu_c\sim N(\log 1.25,0.30^2).
\]

Competition home advantage begins at:

\[
\gamma_c\sim N(\log 1.15,0.25^2).
\]

For a first non-neutral match between unseen teams in an unseen competition,
the uncorrected prior means are therefore:

\[
\lambda_H=1.25\times1.15=1.4375,\qquad
\lambda_A=1.25.
\]

At a neutral venue, the home-advantage term is exactly zero, giving equal prior
rates when team states are equal.

### 6.3 Continuous-time mean reversion

Before a state is read or updated at time \(t\), it is projected from its last
state time \(t_0\). For half-life \(h\) days:

\[
\phi=\exp\left(-\log 2\frac{t-t_0}{h}\right)
     =2^{-(t-t_0)/h}.
\]

For prior mean \(m_0\), prior variance \(v_0\), current mean \(m\), and
variance \(v\):

\[
\begin{aligned}
m(t)&=m_0+\phi[m(t_0)-m_0],\\
v(t)&=\phi^2v(t_0)+(1-\phi^2)v_0.
\end{aligned}
\]

Team attack/defense use a 180-day half-life. Competition goal level and home
advantage use a 730-day half-life. The variance equation restores uncertainty
toward the prior as old evidence decays; it does not merely shrink the mean.

This is OU/state-space-like propagation, but the implementation is a set of
independent scalar filters, not a sampled multivariate state-space posterior.

### 6.4 Match intensities

At prediction time, for home team \(H\), away team \(A\), competition \(c\),
and neutral indicator \(n\):

\[
\tilde\gamma_c=(1-n)\gamma_c.
\]

The structural log intensities are:

\[
\begin{aligned}
\eta_H&=\mu_c+\tilde\gamma_c+\alpha_H-\delta_A,\\
\eta_A&=\mu_c+\alpha_A-\delta_H.
\end{aligned}
\]

The snapshot rates are:

\[
\lambda_H^{state}=\operatorname{clip}(e^{\eta_H},0.05,6),\qquad
\lambda_A^{state}=\operatorname{clip}(e^{\eta_A},0.05,6).
\]

The subtraction of defense means that a positive defense state represents
stronger goal suppression. For example, if an opponent concedes fewer goals
than expected, the defensive state receives a positive update, reducing future
opponents' log rates.

### 6.5 Online state update and its derivation

For a Poisson observation \(y\) with log intensity \(\eta\),

\[
\ell(\eta)=y\eta-e^\eta-\log(y!),
\]

so:

\[
\frac{\partial\ell}{\partial\eta}=y-\lambda,qquad
-E\left[\frac{\partial^2\ell}{\partial\eta^2}\right]=\lambda.
\]

The implementation uses these score and Fisher-information terms. For one
fixture define residuals:

\[
r_H=y_H-\lambda_H,qquad r_A=y_A-\lambda_A.
\]

The state contributions are:

| State | Gradient contribution | Information contribution |
|---|---:|---:|
| competition goal level \(\mu_c\) | \(r_H+r_A\) | \(\lambda_H+\lambda_A\) |
| home attack \(\alpha_H\) | \(r_H\) | \(\lambda_H\) |
| away defense \(\delta_A\) | \(-r_H\) | \(\lambda_H\) |
| away attack \(\alpha_A\) | \(r_A\) | \(\lambda_A\) |
| home defense \(\delta_H\) | \(-r_A\) | \(\lambda_A\) |
| home advantage \(\gamma_c\), non-neutral only | \(r_H\) | \(\lambda_H\) |

For each scalar state with current mean \(m\), variance \(v\), accumulated
batch gradient \(g\), and information \(I\):

\[
v'=(v^{-1}+I)^{-1},\qquad m'=m+v'g.
\]

This is a one-step Gaussian/Laplace assumed-density update using a diagonal
information approximation. It is computationally simple and chronological,
but it ignores posterior covariances among team attack, team defense,
competition level, and home advantage. There is no joint identifiability
constraint such as sum-to-zero attack effects; shrinkage to explicit zero
priors and the shared competition level provide practical anchoring.

### 6.6 Snapshot diagnostics versus scoring inputs

The feature artifact also records:

- posterior standard deviations for all state variables;
- team and competition history counts;
- rest days;
- match counts in the previous 7, 14, and 30 days;
- a cold-start flag below five prior team matches;
- neutral venue and matchup-strength components.

Only the structural expected-goal rates and team IDs/timestamps feed the
champion's next stages. Rest, congestion, cold-start, and posterior standard
deviations are currently diagnostics/warnings, not regression covariates or
uncertainty integration terms. This distinction prevents the feature schema
from being mistaken for the fitted formula.

## 7. Global home/away rate-scale correction

The online structural model can have small aggregate rate bias. A separate
home and away scale correct it.

With prior observed and expected totals both equal to 125:

\[
s_H=\frac{125+\sum_i y_{H,i}}
          {125+\sum_i \lambda^{state}_{H,i}},\qquad
s_A=\frac{125+\sum_i y_{A,i}}
          {125+\sum_i \lambda^{state}_{A,i}}.
\]

The equal pseudo-totals shrink each ratio toward one. They are goal-total
pseudocounts, not literally 125 pseudo-fixtures.

In walk-forward evaluation, these totals contain only results available before
the forecast. Each horizon owns an independent estimator and independent
totals. In the production refit, the totals are recomputed on all eligible
historical rows for that horizon.

After scaling:

\[
\lambda_H^{base}=\operatorname{clip}(s_H\lambda_H^{state},0.05,8),
\quad
\lambda_A^{base}=\operatorname{clip}(s_A\lambda_A^{state},0.05,8).
\]

The 2026-07-15 production scales are:

| Horizon | Home scale | Away scale |
|---|---:|---:|
| T−24h | 1.0058974442 | 0.9844531339 |
| Clean T−72h | 1.0035993349 | 0.9835248593 |

## 8. Chronological xG and shots state

### 8.1 Provider-specific metrics

The rich correction uses:

- Understat team xG, prior mean 1.25;
- API-Football team shots, prior mean 12.0.

Provider identity is part of the feature definition. xG from different
providers is not silently pooled as if the scales were identical.

For a metric to update from a fixture, both teams must have exactly one valid
provider row for that metric. Duplicate rows fail the load; negative values
fail the load. If only one side is present, neither side updates for that
metric. Missing observations do not become zero.

### 8.2 Decayed empirical-Bayes means

For each team and each metric there is an attacking state (metric produced) and
a defensive state (metric conceded). A state stores evidence sum \(E\),
evidence weight \(W\), prior mean \(m_0\), and fixed prior strength \(\kappa=5\).

After \(d\) days, observed evidence decays with 180-day half-life:

\[
q=2^{-d/180},\qquad E\leftarrow qE,\qquad W\leftarrow qW.
\]

The state mean is:

\[
\bar x=\frac{\kappa m_0+E}{\kappa+W}.
\]

An observed value \(x\) updates \(E\leftarrow E+x\) and
\(W\leftarrow W+1\). Thus the prior always retains weight five while match
evidence has an exponentially decayed effective weight.

The separate `history` count is an undiscounted count of complete historical
metric observations. It is used for signal-coverage gating and UI evidence,
not as the decayed denominator.

### 8.3 Matchup signal

For a home-side metric, let \(a_H\) be the home team's attacking state and
\(d_A\) the away team's defensive/conceded state. Let \(n_H,n_A\) be the two
undiscounted history counts, \(m_0\) the metric prior, and \(N=20\) the full
signal threshold.

\[
c_H=\min\left(\frac{\min(n_H,n_A)}{20},1\right),
\]

\[
z_H=c_H\log\left(
\frac{\max((a_H+d_A)/2,10^{-6})}{m_0}
\right).
\]

The away signal swaps sides:

\[
z_A=c_A\log\left(
\frac{\max((a_A+d_H)/2,10^{-6})}{m_0}
\right).
\]

This is computed independently for xG and shots. Using the minimum of the two
teams' history counts prevents a matchup from receiving full signal strength
when only one side is well observed. If either side has zero history, coverage
is zero and the signal is exactly zero even though prior means exist.

### 8.4 Rich-rate correction

For side \(j\in\{H,A\}\):

\[
\lambda_j=\operatorname{clip}\left[
\lambda_j^{base}\exp(\beta_xz_j^{xG}+\beta_sz_j^{shots}),
0.05,8
\right].
\]

The same two coefficients apply to home and away instances within a horizon;
the two horizons have separate fits.

### 8.5 Fitting objective

Each fixture produces two training instances: one home-goal observation and one
away-goal observation. For instance \(r\), let \(\lambda_{0r}\) be its base
rate, \(x_r=(z_r^{xG},z_r^{shots})\), and \(y_r\) its observed goals. The fit
minimizes Poisson negative log likelihood up to constants plus ridge penalty:

\[
Q(\beta)=\sum_r\left[
\lambda_{0r}e^{\beta^Tx_r}
-y_r(\log\lambda_{0r}+\beta^Tx_r)
\right]+\frac{10}{2}\lVert\beta\rVert_2^2.
\]

The score used by the solver is:

\[
g=-10\beta+\sum_rx_r(y_r-\lambda_r),
\]

and the positive information matrix is:

\[
I=10I_2+\sum_r\lambda_rx_rx_r^T.
\]

The Newton/Fisher-scoring step solves \(I\Delta=g\). A backtracking line search
halves step size until the objective decreases, stopping below \(10^{-6}\)
step scale if no decrease is found. The coefficient convergence tolerance is
\(10^{-8}\), with at most 40 iterations. The development-selection and
promoted-evaluation fits converged in four iterations. Production refitting
requires convergence, but the serialized production artifact does not retain
its iteration count.

The final production coefficients are:

| Horizon | xG coefficient | Shots coefficient |
|---|---:|---:|
| T−24h | 0.0453592387 | 0.3674365080 |
| Clean T−72h | 0.0357326972 | 0.3656627524 |

These magnitudes should be interpreted on the coverage-weighted **log-ratio
signal**, not on raw xG or raw shot counts. The comparatively large shots
coefficient does not mean one extra shot multiplies goals by \(e^{0.367}\).

## 9. From goal intensities to a particular exact score

### 9.1 Independent-Poisson score probability

Given final rates \(\lambda_H,\lambda_A\):

\[
P(H=h)=e^{-\lambda_H}\frac{\lambda_H^h}{h!},\qquad
P(A=a)=e^{-\lambda_A}\frac{\lambda_A^a}{a!}.
\]

The champion assumes conditional independence:

\[
P(H=h,A=a\mid I_t)=
e^{-(\lambda_H+\lambda_A)}
\frac{\lambda_H^h}{h!}
\frac{\lambda_A^a}{a!}.
\]

That is exactly how it assigns a probability to "a certain score." The modal
score is the \((h,a)\) cell with the greatest product, but the model retains
the full distribution; it does not output the modal score as a deterministic
pick.

### 9.2 Finite support and numerical tail policy

For moneyline aggregation, each Poisson marginal is generated recursively:

\[
p_0=e^{-\lambda},\qquad p_k=p_{k-1}\frac{\lambda}{k},
\]

until cumulative mass is at least \(1-10^{-12}\). The retained marginal is
then normalized to sum to one. The loop fails if it has not converged by 100
goals.

Technically, the implementation does not expose a terminal `K+` cell. It
renormalizes after dropping less than \(10^{-12}\) marginal tail mass. This is
negligible for the configured rate cap but is worth distinguishing from the
contract design's ideal of an explicit tail bucket. Baseline exact-score log
loss uses the analytic Poisson PMF directly rather than the truncated marginal.

### 9.3 Derived raw moneyline

Define:

\[
\begin{aligned}
q_H&=\sum_{h>a}P(h,a),\\
q_D&=\sum_{h=a}P(h,a),\\
q_A&=\sum_{h<a}P(h,a).
\end{aligned}
\]

The same raw grid can derive totals, team totals, BTTS, and handicaps. The
current production endpoint stops at moneyline because calibration breaks
grid-level coherence, not because those sums are impossible.

## 10. Dixon–Coles challenger and why it is not the champion

The evaluation also implemented a Dixon–Coles low-score dependence correction.
For the independent cell mass \(P_0(h,a)\):

\[
P_{DC}(h,a)=P_0(h,a)\tau(h,a),
\]

where:

\[
\tau(h,a)=
\begin{cases}
1-\lambda_H\lambda_A\rho,&(h,a)=(0,0),\\
1+\lambda_H\rho,&(h,a)=(0,1),\\
1+\lambda_A\rho,&(h,a)=(1,0),\\
1-\rho,&(h,a)=(1,1),\\
1,&\text{otherwise}.
\end{cases}
\]

The online \(\rho\) state begins at zero with prior variance \(0.1^2\). For a
low-score cell with coefficient \(c\) such that \(\tau=1+c\rho\), the update
accumulates:

\[
g_\rho=\frac{c}{1+c\rho},\qquad
I_\rho=\left(\frac{c}{1+c\rho}\right)^2,
\]

then applies the same scalar Gaussian update. \(\rho\) is hard-clipped to
[-0.25,0.25] and additionally clipped to a rate-dependent interval that keeps
all four low-score multipliers strictly positive.

The final-test point estimates slightly favored Dixon–Coles, but every paired
calendar-month bootstrap 95% interval for its final-test deltas crossed zero.
For example:

| Horizon | Metric | DC minus independent | 95% month-block interval |
|---|---|---:|---:|
| T−24h | exact-score log loss | −0.000895 | [−0.002074, 0.000440] |
| T−24h | moneyline log loss | −0.000388 | [−0.000952, 0.000151] |
| Clean T−72h | exact-score log loss | −0.000823 | [−0.001936, 0.000368] |
| Clean T−72h | moneyline log loss | −0.000281 | [−0.000753, 0.000244] |

The added dependence parameter therefore did not earn promotion. The champion
sets \(\rho=0\) and uses independent Poisson.

## 11. Three-way temperature calibration

### 11.1 Transformation

For raw moneyline probabilities \(q_k>0\) and temperature \(T>0\):

\[
p_k=\frac{\exp(\log q_k/T)}{\sum_j\exp(\log q_j/T)}
   =\frac{q_k^{1/T}}{\sum_jq_j^{1/T}}.
\]

When \(T>1\), the distribution becomes less sharp; large probabilities move
toward the center and small probabilities increase. Both production
temperatures exceed one, indicating that raw champion moneyline probabilities
were overconfident on the calibration period.

### 11.2 Fitting

Temperature minimizes mean three-class log loss on the calibration fold:

\[
\hat T=\arg\min_{T\in[0.5,2]}
-\frac1N\sum_i\log p_{i,y_i}(T).
\]

The optimizer performs golden-section search over \(\log T\) with tolerance
\(10^{-8}\). A separate temperature is fit for each model family and horizon.
At least 1,000 calibration fixtures are required.

The promoted rich model temperatures are:

| Horizon | Calibration fixtures | Temperature | Calibration log loss before | after |
|---|---:|---:|---:|---:|
| T−24h | 5,227 | 1.1806793063 | 1.0026624 | 0.9999235 |
| Clean T−72h | 4,795 | 1.1717555674 | 1.0033968 | 1.0009369 |

### 11.3 Coherence limitation

The temperature transformation operates on \((q_H,q_D,q_A)\), not on every
score cell. There is no unique calibrated score grid implied by those three
numbers. The production response therefore contains:

- final goal intensities;
- raw score-derived moneyline;
- calibrated moneyline;
- a warning: `moneyline_calibration_not_score_grid_coherent`.

The supported public production output is calibrated regulation moneyline
only. Mathematically, three calibrated result probabilities do not identify a
unique score grid: infinitely many joint score laws have the same three region
sums. Any coherent back-projection therefore needs an additional within-region
modeling assumption.

V3 makes that assumption explicit rather than pretending uniqueness. It uses
the rate model's Poisson cell weights as a base measure, applies a fitted
low-dimensional exponential tilt, and normalizes within home-win, draw, and
away-win regions separately. The calibrated moneyline supplies the three
region masses; the conditional tilt supplies the relative cell weights inside
each region. Section 18 derives this construction and its prospective
governance in full.

## 12. Fully worked prediction example

This example reconstructs the frozen 2026-07-15 snapshot for Bodø/Glimt vs
Fredrikstad at the clean T−72h anchor. It is an implementation audit example,
not a current betting recommendation.

### 12.1 Team-state snapshot

At the cutoff, the relevant latent means were:

| Quantity | Value |
|---|---:|
| competition log goal level \(\mu_c\) | 0.261749692 |
| competition home advantage \(\gamma_c\) | 0.259262397 |
| home attack \(\alpha_H\) | 0.354537078 |
| away defense \(\delta_A\) | 0.073101289 |
| away attack \(\alpha_A\) | −0.091363063 |
| home defense \(\delta_H\) | 0.228180082 |

Therefore:

\[
\eta_H=0.261749692+0.259262397+0.354537078-0.073101289,
\]

\[
\lambda_H^{state}=e^{\eta_H}=2.230995453.
\]

And:

\[
\eta_A=0.261749692-0.091363063-0.228180082,
\]

\[
\lambda_A^{state}=e^{\eta_A}=0.943844875.
\]

### 12.2 Global scale

Using clean-T−72h production scales:

\[
\lambda_H^{base}=2.230995453\times1.003599335=2.239025553,
\]

\[
\lambda_A^{base}=0.943844875\times0.983524859=0.928294898.
\]

### 12.3 Rich signals

Both teams had zero Understat-xG history in this matchup, so both xG signals
were zero. The coverage-saturated shot signals were:

\[
z_H^{shots}=0.149015452,qquad z_A^{shots}=0.017227417.
\]

With \(\beta_s=0.365662752\):

\[
\lambda_H=2.239025553\exp(0.365662752\times0.149015452)
=2.364413856,
\]

\[
\lambda_A=0.928294898\exp(0.365662752\times0.017227417)
=0.934161080.
\]

### 12.4 Probability of exactly 2–0

\[
P(H=2)=e^{-2.364413856}\frac{2.364413856^2}{2!}
=0.262763541,
\]

\[
P(A=0)=e^{-0.934161080}=0.392915352.
\]

Hence:

\[
P(2\text{–}0)=0.262763541\times0.392915352
=0.103243829.
\]

The leading raw score cells were:

| Score | Raw probability |
|---|---:|
| 2–0 | 10.3244% |
| 2–1 | 9.6446% |
| 1–0 | 8.7331% |
| 1–1 | 8.1582% |
| 3–0 | 8.1370% |
| 3–1 | 7.6013% |
| 4–0 | 4.8098% |
| 2–2 | 4.5048% |

"2–0" is the modal cell, but there is only a 10.3% raw probability of that
exact outcome. This illustrates why the model should be read as a distribution,
not as a confident exact-score pick.

### 12.5 Raw and calibrated moneyline

Summing the raw grid gives:

| Outcome | Raw | After \(T=1.171755567\) |
|---|---:|---:|
| Home win | 69.1751% | 64.3258% |
| Draw | 17.6291% | 20.0305% |
| Away win | 13.1958% | 15.6437% |

The calibrated home-win probability is lower than the raw score-grid value
because \(T>1\) softens the distribution. The 10.3244% exact 2–0 cell remains
a **raw** Poisson-grid quantity; v1 does not publish a calibrated 2–0 value.

## 13. Evaluation design: there is a train/test split, but it is temporal

### 13.1 Why there is no random split

A random fixture split would interleave later and earlier soccer regimes and
would not reproduce live deployment. It could also allow state or aggregate
construction to use information that would not have existed at the target
cutoff. The primary evaluation is chronological and prequential.

### 13.2 Expanding-window/prequential evaluation

Within each horizon:

1. Sort prediction and result-availability events by timestamp.
2. At a prediction event, forecast with the estimator state available
   immediately before that timestamp.
3. Score the forecast only after the first 1,000 training fixtures have been
   observed for that horizon.
4. At a result event, update global scales and Dixon–Coles state.
5. Apply simultaneous result events as one batch.
6. Continue expanding; old training observations are not removed from the
   global scale totals, while the underlying team states themselves mean-revert.

The team-state and rich-state feature builders have already enforced their own
chronological cutoffs. The walk-forward evaluator adds an independent online
rate-scale/dependence layer. T−24h and clean T−72h never share online estimator
state.

The 1,000-fixture warmup is not a fixed training set followed by a static test.
Warmup rows update the estimator but are not emitted as scored predictions.
Every later forecast uses a larger legitimate history than the previous one.

### 13.3 Named chronological folds

The emitted predictions are labeled by kickoff:

| Fold | Kickoff interval |
|---|---|
| Development | before 2024-07-01 00:00 UTC |
| Calibration | 2024-07-01 through before 2025-07-01 00:00 UTC |
| Test | 2025-07-01 onward in the frozen artifact |

The folds label a continuously expanding evaluation. The estimator is not
reset at fold boundaries. This is the correct deployment analogue: a 2025
forecast can learn from eligible pre-2025 history, but never from its own or
future results.

Scored independent-Poisson fixture counts are:

| Horizon | Development | Calibration | Test |
|---|---:|---:|---:|
| T−24h | 27,023 | 5,227 | 5,159 |
| Clean T−72h | 24,245 | 4,795 | 4,743 |

The first scored development kickoffs occur in February 2015 because of the
per-horizon warmup, although the frozen feature history begins in August 2014.

### 13.4 Nested selection path for the rich correction

The xG/shots recipe was selected without opening the calibration or test folds:

1. **Initial research fit:** kickoff before 2023-07-01, still within
   development.
2. **Internal development validation:** 2023-07-01 through before 2024-07-01.
3. **Promotion gate:** for both horizons and both moneyline log loss and Brier,
   the paired month-block 95% upper bound had to be below zero.
4. **Development refit:** after passing, coefficients were refit on all
   development predictions through before 2024-07-01.
5. **Calibration fit:** temperature was fit only on the 2024-07-01 to
   2025-07-01 calibration fold.
6. **Final test:** the frozen recipe was scored once on the later test fold.

The initial development-only validation produced:

| Horizon | Fixtures | Log-loss delta vs independent | 95% interval | Brier delta | 95% interval |
|---|---:|---:|---:|---:|---:|
| T−24h | 5,072 | −0.004283 | [−0.005513, −0.003009] | −0.003107 | [−0.003889, −0.002288] |
| Clean T−72h | 4,601 | −0.004050 | [−0.005106, −0.002946] | −0.002958 | [−0.003560, −0.002299] |

Only after that gate passed were coefficients refit on all development data,
temperatures fit on calibration, and final test evaluated.

## 14. Scoring rules and uncertainty calculations

### 14.1 Exact-score log loss

For observed score \((y_H,y_A)\):

\[
L_{score}=-\log P(H=y_H,A=y_A).
\]

The probability is floored at \(10^{-15}\) before taking the logarithm in the
baseline evaluator.

### 14.2 Moneyline log loss

For realized class \(r_i\):

\[
L_{ML,i}=-\log p_{i,r_i}.
\]

Log loss is the primary selection criterion because it rewards assigning
probability to the realized outcome and heavily penalizes unjustified
certainty. The baseline evaluator floors the realized moneyline probability at
\(10^{-15}\) before taking its logarithm; raw Poisson moneyline probabilities
are strictly positive under the configured rate bounds.

### 14.3 Three-class Brier score

The implementation uses the sum across the three classes, not the average per
class:

\[
B_i=\sum_{k\in\{H,D,A\}}(p_{ik}-\mathbf1[r_i=k])^2.
\]

This convention explains values near 0.60; dividing by three would produce a
different scale.

### 14.4 Calibration error

The reported calibration error is a ten-bin, equal-width, one-vs-all ECE-like
summary. Each fixture contributes three probability/outcome pairs. For bin
\(b\):

\[
ECE=\sum_b\frac{n_b}{3N}
\left|\overline p_b-\overline y_b\right|.
\]

It is a useful compact diagnostic but not a complete calibration analysis; it
uses fixed-width bins and mixes the three outcome classes.

### 14.5 Paired calendar-month block bootstrap

Model comparisons are paired fixture by fixture. For each fixture, compute the
challenger-minus-baseline loss delta and group deltas by kickoff calendar month.
With \(M\) observed months, one bootstrap replicate samples \(M\) entire month
blocks with replacement and computes the fixture-weighted mean of all sampled
deltas.

The evaluator uses:

- 2,000 replicates;
- deterministic base seed 20260714 plus a SHA-256-derived comparison seed;
- the empirical 2.5% and 97.5% ordered replicate indices;
- the fraction of replicate means below zero as the reported probability that
  the lower-is-better challenger is better.

Month blocking acknowledges serial dependence and regime clustering more
honestly than an IID fixture bootstrap. It does not, however, simultaneously
cluster by competition/team, and with only 13 final-test month blocks its tail
precision remains limited.

## 15. Held-out evidence for the promoted recipe

The final test compares the calibrated rich model against calibrated
independent Poisson on identical fixtures.

| Horizon | Test fixtures | Champion log loss | Champion Brier | Log-loss delta vs calibrated independent | 95% interval | Brier delta | 95% interval |
|---|---:|---:|---:|---:|---:|---:|---:|
| T−24h | 5,159 | 1.0143715 | 0.6070100 | −0.0045293 | [−0.0055777, −0.0035300] | −0.0031480 | [−0.0038167, −0.0025196] |
| Clean T−72h | 4,743 | 1.0167267 | 0.6088214 | −0.0043393 | [−0.0053215, −0.0034037] | −0.0029985 | [−0.0036961, −0.0023479] |

All four intervals are below zero in this frozen comparison. That is the
promotion evidence for the **recipe**.

It is not evidence that the all-history production coefficients themselves
were evaluated out of sample. Once the recipe was selected, those coefficients
were refit on all eligible history to maximize legitimate information for live
inference. Held-out quality displayed for production must remain the frozen
recipe-level evidence above.

The final test has now been inspected. Further tuning against it would turn it
into development data. A changed feature, threshold, prior, model family,
calibrator, or suppression rule needs a new version and a new forward or nested
evaluation window.

## 16. Production refit

### 16.1 What is refit

For each horizon, production refitting recomputes on all eligible historical
rows:

- home rate scale;
- away rate scale;
- xG coefficient;
- shots coefficient.

### 16.2 What is frozen

The following remain part of the selected recipe:

- eligibility and target semantics;
- information-state definitions;
- priors, half-lives, bounds, and 150-minute delay;
- rich-signal construction and 20-match coverage ramp;
- independent-Poisson family;
- ridge strength and optimizer policy;
- three-way temperature-scaling method;
- calibration temperatures from the leakage-safe calibration fold.

The temperatures are intentionally **not** re-estimated on in-sample
all-history predictions after the refit. Doing so would create an optimistic
calibrator without a new out-of-fold procedure.

### 16.3 Artifact parameters

| Horizon | Training rows | Home scale | Away scale | xG beta | Shots beta | Frozen T |
|---|---:|---:|---:|---:|---:|---:|
| T−24h | 38,445 | 1.0058974442 | 0.9844531339 | 0.0453592387 | 0.3674365080 | 1.1806793063 |
| Clean T−72h | 34,813 | 1.0035993349 | 0.9835248593 | 0.0357326972 | 0.3656627524 | 1.1717555674 |

The logical model SHA-256 is:

```text
8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a
```

The manifest separately hashes the warehouse snapshot, feature rows, rich
rows, source configuration, evaluation report, and serialized artifact.

## 17. Upcoming inference and publication

### 17.1 Replay, do not synthesize

Upcoming inference rebuilds the same chronological state machines from
historical targets and optional rich observations. Upcoming fixtures are
separate objects without `home_goals` or `away_goals`; the code cannot make a
scheduled fixture look like a zero-zero historical target.

Only historical results whose 150-minute availability time is at or before a
snapshot's prediction timestamp can update that snapshot. Rich observations
obey the same delay and batching policy.

### 17.2 Output fields

For each due fixture/horizon, the snapshot exposes:

- exact `prediction_at` and kickoff;
- model, feature, and rich-feature identity;
- final expected home and away goals;
- raw home/draw/away probabilities;
- calibrated home/draw/away probabilities;
- team, xG, and shot history counts;
- typed warnings.

Warnings include team cold start, prior-only/unavailable rich signals, and the
moneyline/score-grid coherence limitation. Cold-start rows are not suppressed:
that would be a new applicability rule that was not evaluated in v1.

### 17.3 Fail-closed publication boundary

Before the API serves a snapshot, validation requires:

- the supported output to be regulation moneyline;
- timezone-aware timestamps;
- prediction cutoff before kickoff;
- supported horizon labels;
- unique fixture/horizon keys;
- raw and calibrated probability triplets each summing to one within
  \(10^{-9}\);
- every probability strictly between zero and one;
- nonnegative finite expected goals and history counts;
- valid model/training evidence fields.

The publisher additionally checks model version, logical model hash, exact
`as_of`, minimum row count, no started fixtures, no not-yet-due horizons, and a
64-character prediction-row hash. Object storage upload is read back
byte-for-byte and revalidated. The API reports staleness separately rather
than quietly presenting an old snapshot as fresh.

## 18. Score-grid contract pricing and settlement math

Given a normalized finite score grid \(\pi_{h,a}\), the deterministic pricer
computes:

\[
P(H)=\sum_{h>a}\pi_{h,a},\quad
P(D)=\sum_{h=a}\pi_{h,a},\quad
P(A)=\sum_{h<a}\pi_{h,a},
\]

\[
P(BTTS)=\sum_{h>0,a>0}\pi_{h,a},
\]

and corresponding mass sums for match totals, team totals, and selected-team
goal difference.

Integer and half lines settle directly. A quarter line is split equally into
the adjacent half-step legs. For example, a 2.25 total becomes legs 2.0 and
2.5. Each score cell maps to one of:

```text
win, half_win, push, half_loss, loss
```

The settlement distribution sums grid mass by that mapping. Fair decimal odds
for a settlement distribution are:

\[
O_{fair}=1+
\frac{P(loss)+0.5P(half\_loss)}
     {P(win)+0.5P(half\_win)}.
\]

For a non-quarter contract with pushes, conditional win probability is:

\[
P(win\mid decisive)=\frac{P(win)}{P(win)+P(loss)}.
\]

This contract layer is deterministic and tested independently of model
training. It must not be confused with evidence that the current moneyline-only
calibrator validates those other contracts.

### 18.1 Why a calibrated joint distribution is the correct next object

Every score-settled regulation contract is a measurable partition or function
of the same pair of integer-valued random variables \((H,A)\). Training a
separate classifier for exact score, another for over/under 2.5, another for
BTTS, and another for each handicap can produce individually reasonable but
mutually impossible prices. For example, separately fitted heads need not
satisfy

\[
P(BTTS)=1-P(H=0)-P(A=0)+P(H=0,A=0),
\]

or

\[
P(H>A)+P(H=A)+P(H<A)=1.
\]

The primary modeling object must therefore be the normalized joint measure

\[
\Pi=\{\pi_{h,a}:h,a\in\mathbb N_0\},
\qquad \pi_{h,a}\ge0,\quad\sum_{h,a}\pi_{h,a}=1.
\]

Contract probabilities and settlement distributions are downstream sums over
\(\Pi\). This makes cross-market identities implementation invariants rather
than hoped-for empirical relationships.

### 18.2 V2 research: unconstrained joint exponential tilt

The first distribution-level challenger started from the champion's corrected
rates and independent-Poisson grid \(q_{h,a}\). It tested joint temperature
scaling and a six-feature exponential tilt:

\[
\pi_\theta(h,a)=
\frac{q_{h,a}\exp\{\theta^\top f_2(h,a)\}}
{\sum_{i,j}q_{i,j}\exp\{\theta^\top f_2(i,j)\}},
\]

\[
f_2(h,a)=
\begin{bmatrix}
h/3\\a/3\\(\log h!+\log a!)/5\\
\mathbf1(h=a)\\\mathbf1(h=a=0)\\\mathbf1(h>0,a>0)
\end{bmatrix}.
\]

It minimized penalized exact-score negative log likelihood with ridge penalty
25. Its score and Fisher information were

\[
g(\theta)=\sum_n[f(y_n)-E_{\pi_{\theta,n}}f]-25\theta,
\]

\[
I(\theta)=\sum_n\operatorname{Cov}_{\pi_{\theta,n}}(f)+25I.
\]

Newton/Fisher steps used backtracking and log-sum-exp normalization. Candidate
selection used 2023-H2 for fitting and 2024-H1 for validation. The selected
family was frozen, refit on 2024-H2, and scored on 2025-H1. No kickoff at or
after the already-opened 2025-07-01 final-test boundary was admitted to v2
research.

V2 improved confirmation-period exact-score log loss relative to the raw score
grid:

| Horizon | Fixtures | Exact-score delta | Paired month-block 95% interval |
|---|---:|---:|---:|
| T−24h | 2,671 | −0.005302 | [−0.008773, −0.002633] |
| Clean T−72h | 2,446 | −0.005995 | [−0.010486, −0.002398] |

It also improved mean total-goal, goal-difference, BTTS, and home/away marginal
log loss. If the benchmark had been only the uncalibrated Poisson grid, the
candidate would have looked promotable.

That was not the correct primary-market comparison. The incumbent moneyline
is temperature-calibrated. A fair confirmation control fitted three-way
temperature on each fit half and applied it only to the later validation half.
Against that stronger control, v2 moneyline log loss changed by:

| Horizon | V2 minus calibrated moneyline control |
|---|---:|
| T−24h | +0.002184 |
| Clean T−72h | +0.001666 |

Both violated the predeclared maximum degradation of +0.001. V2 was therefore
rejected with status `research_candidate_failed_confirmation_gate`. The gate
was not weakened after seeing the result. This failure identified a precise
design requirement: score-shape correction must not be allowed to spend the
already-promoted moneyline calibration.

### 18.3 V3 construction: calibrated result masses plus conditional score law

Let

\[
q_{h,a}=e^{-(\lambda_H+\lambda_A)}
\frac{\lambda_H^h}{h!}\frac{\lambda_A^a}{a!}
\]

be the rate model's raw score measure, and let the promoted champion return

\[
m_H=P_{champ}(H>A),\quad
m_D=P_{champ}(H=A),\quad
m_A=P_{champ}(H<A).
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

For each result region \(r\), define

\[
Z_r(\theta;\lambda_H,\lambda_A)=
\sum_{(i,j):R(i,j)=r}
q_{i,j}\exp\{\theta^\top f_3(i,j)\}.
\]

The v3 joint law is

\[
\boxed{
\pi_\theta(h,a)=m_{R(h,a)}
\frac{q_{h,a}\exp\{\theta^\top f_3(h,a)\}}
{Z_{R(h,a)}(\theta;\lambda_H,\lambda_A)}
}.
\]

Summing over a result region gives

\[
\sum_{(h,a):R(h,a)=r}\pi_\theta(h,a)
=m_r\frac{Z_r}{Z_r}=m_r.
\]

Thus the grid moneyline equals the parent moneyline for every possible
coefficient vector, rate pair, and fixture, modulo floating-point arithmetic.
This is stronger than a penalty, a calibration target, or a validation
observation: it is a structural identity.

At \(\theta=0\), v3 becomes the coherent parent-preserving control

\[
\pi_0(h,a)=m_{R(h,a)}
\frac{q_{h,a}}
{\sum_{(i,j):R(i,j)=R(h,a)}q_{i,j}}.
\]

The control keeps the Poisson relative score shape inside each outcome region
while replacing the three raw region masses with the champion-calibrated
masses. Prospective v3 evaluation compares the fitted conditional tilt against
this control. Both have exactly the same moneyline; the comparison isolates
whether the learned within-result score shape adds information.

### 18.4 V3 feature map and identifiability

The frozen conditional feature vector is

\[
f_3(h,a)=
\begin{bmatrix}
h/3\\
a/3\\
(\log h!+\log a!)/5\\
\mathbf1(h=a=0)\\
\mathbf1(h>0,a>0)
\end{bmatrix}.
\]

The divisors 3, 3, and 5 are conditioning scales. They are fixed, not learned.
The home/away terms allow smooth movement among score magnitudes inside each
result; the log-factorial term changes conditional tail concentration; the
0–0 term distinguishes scoreless draws from positive draws; and BTTS adjusts
positive-positive cells.

V2's generic draw indicator is intentionally absent. Within the draw region it
is identically one; within the home- and away-win regions it is identically
zero. It therefore cancels between numerator and conditional partition
function and has no identifiable effect. Removing it prevents a formally
redundant coefficient from contaminating the information matrix.

There is also no result-region intercept. Any constant added to all cells in a
region cancels in that region's conditional normalization. The three region
masses belong exclusively to the parent champion.

### 18.5 Conditional likelihood, derivatives, and optimizer

For historical fixture \(n\), write \(r_n=R(h_n,a_n)\). Since the parent
probability \(m_{r_n}\) is constant with respect to \(\theta\), fitting the
within-result shape minimizes

\[
\mathcal L(\theta)=
\sum_n\left[
-\log q_n(h_n,a_n)
-\theta^\top f_3(h_n,a_n)
+\log Z_{r_n,n}(\theta)
\right]
+\frac{25}{2}\|\theta\|_2^2.
\]

The penalized log-likelihood score is

\[
g(\theta)=\sum_n\left[
f_3(h_n,a_n)-E_\theta(f_3(H,A)\mid R=r_n)
\right]-25\theta,
\]

and negative Hessian/Fisher information is

\[
I(\theta)=\sum_n
\operatorname{Cov}_\theta(f_3(H,A)\mid R=r_n)+25I.
\]

Each iteration solves

\[
I(\theta)\Delta=g(\theta)
\]

by pivoted Gaussian elimination and proposes \(\theta+\Delta\). Backtracking
halves the step until the penalized objective decreases. A fit converges when
the maximum coefficient step is below \(10^{-8}\) or the average score norm is
below \(10^{-8}\). If line search stalls at floating-point resolution, it is
accepted only when the average score norm is below \(10^{-7}\); otherwise the
artifact is rejected. The maximum is 30 iterations and each horizon needs at
least 5,000 fit fixtures.

Fitting stores two truncated Poisson marginal vectors per fixture, not a dense
dictionary for every score cell. Conditional moments still iterate over the
exact same finite Cartesian support; the representation changes memory use,
not the objective.

### 18.6 V3 finite support and numerical invariants

For each team, Poisson probabilities are generated recursively from

\[
P(X=0)=e^{-\lambda},\qquad
P(X=k)=P(X=k-1)\lambda/k.
\]

Support includes at least goals 0 through 12 and extends until omitted
marginal tail mass is no larger than \(10^{-12}\), with a safety maximum of 60
goals per team. The truncated marginal is renormalized, then v3 performs
log-weight stabilization and separate result-region normalization.

Inference rejects nonpositive/nonfinite rates, a parent triplet with missing or
nonpositive outcomes, a parent sum outside tolerance, unsupported horizons,
nonpositive score cells, a joint sum outside (10^{-10}), or a result marginal
different from its parent by more than (10^{-10}). The first live eligible
shadow snapshot achieved:

| Invariant | Observed maximum/minimum |
|---|---:|
| Absolute parent-versus-grid moneyline difference | (8.88\times10^{-16}) |
| Absolute grid normalization error | (1.11\times10^{-16}) |
| Minimum finite score-cell probability | (3.25\times10^{-27}) |

### 18.7 Frozen v3 artifact

The v3 fit consumes chronological rich-rate predictions through but excluding
2026-07-11. These historical outcomes are parameter-estimation inputs, not
held-out performance evidence.

| Horizon | Training fixtures | Newton iterations | Penalized objective |
|---|---:|---:|---:|
| T−24h | 15,458 | 4 | 30,892.3677 |
| Clean T−72h | 14,139 | 4 | 28,142.4840 |

The coefficients, on the scaled feature vector, are:

| Horizon | home goals | away goals | log-factorial | 0–0 | BTTS |
|---|---:|---:|---:|---:|---:|
| T−24h | 0.297579 | 0.305641 | −0.711568 | 0.004884 | −0.040752 |
| Clean T−72h | 0.262488 | 0.274630 | −0.666558 | 0.009040 | −0.021508 |

The logical model SHA-256 is
`d17aa0334ad85914a396089430ad588ef8ca9381227de044106c1c777cbe00c7`.
The tracked deployment artifact and generated fit artifact must deserialize to
that same logical hash.

The negative log-factorial coefficients imply additional conditional
concentration relative to Poisson within a result region, while the positive
goal-count coefficients partially reposition mass across compatible score
magnitudes. Coefficients act jointly through the region-specific partition
functions; none is an additive probability effect. These in-sample fitted
parameters cannot be cited as proof that v3 forecasts better.

### 18.8 First eligible prospective snapshot

The production collector published a verified champion snapshot with
`as_of=2026-07-17T17:40:45.165106Z`, 25 horizon rows, and 15 fixtures. The exact
validated object was downloaded read-only from the production snapshot bucket;
the live warehouse was neither stopped nor opened for this operation.

V3 transformed those parent rows at
`2026-07-17T17:45:23.810020Z`, before every included kickoff. The preserved
first source snapshot is
`data/predictions/regulation_score_grid_v3_shadow/20260717T174045Z.json`, with
file SHA-256
`824ecc70570417a351be8d1d428aad3a696172b2b2438fa32e5f5924496f2ce4`.
All 25 parent rows became shadow rows; none was excluded.

Each row contains parent and implied moneyline, full joint score grid, top 15
exact scores, home/away/total/difference marginals, BTTS, and a grid hash. It is
explicitly marked `prospective_shadow_not_for_production_betting` and
`retrospective_performance_not_estimated`.

Production evidence is now normalized to one immutable file per
`(fixture_id, information_state)` under the shadow `evidence/` directory. The
first valid row is canonical; later five-minute refreshes cannot create more
evaluation weight or replace it. The original timestamped source snapshot is
imported oldest-first and retained, while future cycles replace only
`latest.json` and write compact receipts when genuinely new pairs appear. This
changes storage representation, not the forecast values or frozen gate.

### 18.9 Frozen prospective gate

The decision policy is stored separately from the fitted artifact in
`regulation_score_grid_v3_prospective_gate_v1`. Freezing it before the first
eligible shadow prediction prevents evaluation thresholds from drifting after
outcomes arrive.

Each horizon requires at least six complete calendar-month blocks, 2,000 paired
fixtures, and five competitions. The primary exact-score log-loss delta must be
negative in mean and its paired calendar-month bootstrap 95% upper endpoint
must be below zero at both horizons. The bootstrap uses 2,000 replicates and
seed 20260717.

Mean total-goal and goal-difference log-loss deltas must be nonpositive.
Home-goal, away-goal, BTTS log loss, total RPS, and goal-difference RPS may each
degrade by at most 0.001. Moneyline equality is not a statistical gate because
it is structural; the numerical absolute difference may never exceed
\(10^{-10}\).

Only immutable predictions whose parent `as_of` is at or after recipe freeze
and whose shadow creation is strictly before kickoff are eligible. Outcomes are
joined only after prediction hashes are fixed. Any coefficient, feature,
penalty, support, threshold, or decision-rule change creates a new challenger
version and requires a new untouched forward holdout.

### 18.10 Operational accumulation and failure isolation

The champion collector remains the public production path. After it generates,
independently validates, uploads, reads back, and revalidates the moneyline
snapshot, it invokes the v3 transformation against the exact local parent
bytes. The shadow artifact is written only to the persistent private volume
under `data/predictions/regulation_score_grid_v3_shadow`; it is not uploaded to
the public application snapshot key.

The collector independently verifies the tracked v3 model version/hash, frozen
gate identity, parent `as_of`, unique fixture/horizon keys, pre-kickoff creation,
positive normalized grid cells, grid-implied moneyline, and parent equality.
A shadow failure is recorded in the append-only prediction publication report
but cannot undo or falsely mark as failed an already verified champion upload.
This isolates experimental evidence collection from the public production
service while ensuring failures remain observable.

### 18.11 Prospective outcome settlement

After a valid shadow cycle, a separate process opens the warehouse read-only
and joins final regulation results to the first immutable forecast for each
fixture/horizon pair. It starts from `fixture_model_eligibility`, requires
`eligible_result_models`, excludes reviewed conflicts, and fails closed on any
new provider disagreement or malformed final score.

The ledger recomputes both frozen distributions: the fitted conditional tilt
and the parent-moneyline-preserving zero-tilt Poisson control. The recomputed
candidate grid hash must equal the stored evidence hash. Each append records
exact-score, home-goal, away-goal, total-goal, goal-difference, moneyline, and
BTTS log losses; moneyline and BTTS Brier scores; total and goal-difference
RPS; candidate-minus-baseline deltas; and deterministic total/Asian-handicap
settlements at frozen reference lines.

Every row contains separate temporal and identity checks. Only their
conjunction is eligible for the future gate. Existing rows are never rescored
or rewritten when the warehouse changes. Records form an append-only SHA-256
chain, while the original forecasts remain in a separate evidence directory.
No aggregate mean, bootstrap interval, or gate decision is produced by the
settlement stage. See `PROSPECTIVE_SETTLEMENT_LEDGER.md` for the complete schema,
equations, sign conventions, and failure policy.

### 18.12 Frozen one-shot prospective evaluation

The evaluator is a third artifact boundary after immutable forecast evidence
and append-only settlement. Its config and implementation module are
byte-hash-pinned before performance inspection. The holdout begins on July 17,
2026, so August 2026 is the first full UTC calendar block. Each month matures
seven days after month end. The deterministic cutoff is the first mature month
where both horizons independently have at least six nonempty blocks, 2,000
eligible settled fixture/horizon rows, and five competitions.

Routine collection performs count-only readiness. It verifies the ledger hash
chain, frozen identities, timestamps, competition IDs, integrity Booleans, and
eligibility conjunction, but deliberately does not access per-row metric
fields. It cannot compute an aggregate or make a decision. Readiness requires
an explicit one-shot command.

For metric \(k\), horizon \(s\), and selected fixture \(i\), define the paired
loss difference

\[
d_{i,s}^{(k)}=L_k(p_{\theta,i,s},Y_i)-L_k(p_{0,i,s},Y_i),
\]

so negative is favorable. Point estimates are fixture-weighted means. Exact
score log loss additionally uses a paired calendar-month cluster percentile
bootstrap: sample the observed month labels with replacement, concatenate all
fixture deltas from each sampled month, and take the mean. The frozen program
uses 2,000 replicates, seed 20260717, separate deterministic generators per
horizon, and linear Type-7 2.5% and 97.5% quantiles.

At both horizons, exact-score mean delta and its 97.5% bootstrap endpoint must
be strictly negative. Total-goals and goal-difference log-loss mean deltas must
be nonpositive. Home-goals, away-goals, BTTS log loss, total-goals RPS, and
goal-difference RPS may each degrade by at most 0.001 in mean. The maximum
candidate or baseline deviation from parent moneyline must remain at most
\(10^{-10}\). Every Boolean must pass; there is no compensation across metrics
or horizons.

The decision is created once using create-if-absent and durable filesystem
operations. It binds the frozen identities, ledger boundary and head, selected
record-hash digest, cutoff, metrics, interval, and gate Booleans. Later ledger
appends are permitted, but the original ledger prefix and selected evidence are
revalidated. Pass means human promotion review only, never automatic
publication or betting. See `PROSPECTIVE_EVALUATION_PROGRAM.md` for the full
mathematical and operational specification.

## 19. Market benchmark and what it says about edge

### 19.1 Strict timestamped Polymarket benchmark

A valid Polymarket comparison requires all three semantically mapped home,
draw, and away `Yes` order books at or before the model cutoff; a known kickoff;
both bid and ask; valid \(0<bid\le ask<1\); and spread no wider than 0.20.
Prices are midpoints and then normalized across the three outcomes.

The frozen audit has zero complete eligible fixtures. No claim of edge versus
timestamped Polymarket is currently supported.

### 19.2 Retrospective Football-Data benchmark

Football-Data closing consensus uses inverse decimal odds normalized to remove
the three-way overround:

\[
q_k=1/O_k,qquad p_k=q_k/\sum_jq_j.
\]

It covers 12,458 local fixtures, but `quoted_at` is null. These odds are a
retrospective performance yardstick and are prohibited as T−72h/T−24h model
features.

On the covered final-test subset, closing consensus beat the champion by about
0.0424 log-loss points at both horizons, with month-block intervals fully below
zero. That is a meaningful benchmark gap. It is not evidence that the model
could have observed those closing prices at its historical anchors, and it is
not proof of a tradeable edge in either direction after spreads, liquidity,
fees, and selection effects.

## 20. What the model learns—and what it does not

### 20.1 Learned or updated from data

- chronological team attack and defense states;
- competition goal level and home advantage;
- their diagonal posterior variances;
- global home/away rate bias;
- decayed xG and shots attacking/conceding states;
- horizon-specific xG and shots correction coefficients;
- horizon-specific moneyline temperature;
- the rejected Dixon–Coles low-score dependence state during evaluation.
- v2's rejected unconstrained joint score-tilt coefficients during research;
- v3's horizon-specific result-conditional score-shape coefficients for the
  prospective shadow.

### 20.2 Fixed hyperparameters

- team prior SD 0.45;
- team half-life 180 days;
- competition goal-level prior \(\log 1.25\), SD 0.30;
- competition home-advantage prior \(\log 1.15\), SD 0.25;
- competition half-life 730 days;
- state-rate bounds [0.05, 6];
- final rate bounds [0.05, 8];
- rich prior strength five matches;
- rich half-life 180 days;
- full rich signal at 20 observations;
- rich ridge penalty 10;
- rate-scale pseudo totals 125/125;
- 150-minute result delay;
- 1,000-fixture evaluation warmup;
- Poisson tail tolerance \(10^{-12}\);
- probability floor \(10^{-15}\);
- bootstrap design and seed;
- temperature range [0.5, 2].
- v2/v3 score-grid minimum support of 0–12 goals per team and hard maximum 60;
- v2/v3 score-grid ridge penalty 25;
- v3 five-feature conditional map and scaling constants 3, 3, 5, 1, 1;
- v3 minimum 5,000 fit fixtures per horizon and 30 Newton iterations;
- v3 normalization and parent-moneyline tolerance (10^{-10});
- v3 prospective evidence minimum and paired month-block decision gate.

These are part of the model version. Changing one after reading the frozen test
creates a new recipe, not a harmless production refit.

### 20.3 Explicitly absent from the champion formula

- confirmed lineup or player availability;
- player quality, minutes, goals, or assists;
- injuries and suspensions;
- goalkeeper effects;
- rest and congestion covariates, despite being emitted;
- travel distance;
- formation or tactical style;
- season-specific intercepts;
- promotion/regime-change indicators;
- posterior predictive integration over parameter uncertainty;
- overdispersion or shared scoring shocks;
- market prices;
- competition-specific rich coefficients;
- direct machine-learning residual trees or neural networks.

## 21. Statistical interpretation and limitations

### 21.1 Conditional independence and dispersion

Independent Poisson imposes:

\[
\operatorname{Var}(H\mid I)=E(H\mid I)=\lambda_H,
\]

and similarly for away goals, with zero conditional covariance. Real soccer can
exhibit low-score dependence, tactical mixtures, red-card regimes, and
overdispersion. Dixon–Coles did not produce sufficiently certain incremental
value in this evaluation, but that does not prove independence is literally
true. It means the tested correction did not clear the promotion standard.

### 21.2 Diagonal state approximation

The state filter ignores covariance. A high-scoring match could be explained
jointly by team attack, opponent defense, competition level, and home
advantage, but the implementation updates scalar marginals independently from
the same residual. This is fast and regularized, yet can underrepresent joint
parameter uncertainty and confounding.

### 21.3 Parameter uncertainty is not propagated into scores

The artifact stores state standard deviations but plugs posterior means into
\(\lambda\). It does not integrate:

\[
P(h,a\mid\text{data})=
\int P(h,a\mid\theta)P(\theta\mid\text{data})d\theta.
\]

Consequently, sparse-team predictive distributions do not automatically widen
because parameter uncertainty is large. Cold-start warnings expose some of
this limitation, but warnings are not a mathematical correction.

### 21.4 Partial coverage and transportability

Understat xG is concentrated in supported leagues. API-Football shots are
broader but still provider- and competition-dependent. Coverage-weighting
shrinks sparse rich signals to zero, but the base team state may still transfer
across competitions without an explicit learned translation layer. The project
is not globally representative; MLS is not currently in collection scope.

### 21.5 Calibration is contract-specific

Three-way temperature calibration improved moneyline reliability but makes the
published moneyline inconsistent with the raw exact-score grid. V2 demonstrated
that unconstrained distribution calibration could improve exact-score loss
while degrading that promoted result calibration. V3 removes this tradeoff by
holding the calibrated result marginals fixed and modeling only conditional
score shape. It is structurally coherent, but its exact-score/total/handicap
quality remains unproven until prospective evaluation completes.

### 21.6 Final-test governance

The current test period supplied valid one-time evidence for v1, but it is no
longer untouched. Continuing to tune against the same report would overfit the
research process even if each individual fit remains chronological.

### 21.7 No guaranteed betting edge

Good probabilistic scores are necessary, not sufficient, for a tradable edge.
The system lacks complete timestamped Polymarket comparison history, execution
cost modeling, liquidity/depth constraints, and forward paper-trading evidence.
The project is a research system, not a guarantee of profit.

## 22. Leakage, invariance, and reproducibility guardrails

The implementation and tests enforce the following properties:

1. A target result cannot change its own pre-match features.
2. A target result cannot change its own walk-forward prediction.
3. A future result cannot change an earlier feature or prediction.
4. A prior result is unavailable until 150 minutes after kickoff.
5. A result becoming available exactly at a prediction timestamp is processed
   after the prediction.
6. Simultaneous results are batched and input-order invariant.
7. T−24h and clean T−72h maintain independent online estimators.
8. Clean T−72h rows fail when either team has an intervening fixture.
9. Upcoming fixtures have no fake outcome fields.
10. Schedule knowledge must exist at the exact historical anchor and match the
    current kickoff.
11. Rich observations cannot change their own pre-match snapshot.
12. Calibration fits only the calibration fold and outputs only test-fold
    predictions during evaluation.
13. Mutating a test outcome cannot change a fitted temperature or other test
    predictions.
14. Development promotion evidence must state that the test fold was not
    accessed and must pass all configured interval gates.
15. Dataset, prediction, rich-feature, model, source-file, and snapshot hashes
    make logical artifacts reproducible and tamper-evident.
16. Raw evidence is immutable; canonical conflicts fail closed rather than
    using silent provider precedence.
17. Missing values remain missing; no invented scores, minutes, xG, or shots
    are inserted.
18. The warehouse is opened read-only by model-building and inference scripts.
19. V2 rejects every kickoff at or after its opened final-test boundary.
20. V3 fit rejects every kickoff at or after its frozen training cutoff.
21. V3's full score grid must reproduce each parent moneyline probability to
    absolute tolerance (10^{-10}).
22. The v3 parent snapshot must be post-freeze and shadow creation must be
    strictly before kickoff.
23. Pre-freeze local champion snapshots are rejected rather than relabeled as
    prospective evidence.
24. The v3 model and prospective gate have independent versioned hashes and
    identities.
25. Shadow-generation failure is isolated from the already-validated public
    champion publication and is recorded without secrets.
26. V3 outcomes may be joined only after immutable pre-kickoff artifacts exist.

## 23. Reproducible command sequence

The implemented research and production sequence is:

```bash
# 1. Build frozen point-in-time targets/features and manifest
.venv/bin/python scripts/build_regulation_modeling_dataset.py

# 2. Run expanding-window independent-Poisson and Dixon–Coles baselines,
#    then fit baseline temperatures on calibration and score test
.venv/bin/python scripts/evaluate_regulation_baselines.py

# 3. Research xG/shots only inside development
.venv/bin/python scripts/research_rich_rate_features.py

# 4. Enforce the promotion gate, refit on development, calibrate on the next
#    year, and evaluate the frozen test once
.venv/bin/python scripts/evaluate_promoted_rich_rate_model.py

# 5. Audit timestamped and retrospective markets
.venv/bin/python scripts/evaluate_market_benchmarks.py

# 6. Refit the frozen recipe on all eligible local history
.venv/bin/python scripts/fit_regulation_champion.py

# 7. Replay history and create a read-only upcoming snapshot
.venv/bin/python scripts/predict_upcoming_regulation.py \
  --as-of 2026-07-15T00:30:00+00:00

# 8. Reproduce the v2 coherent-grid research and its failed confirmation gate
.venv/bin/python scripts/research_score_grid_v2.py

# 9. Fit the frozen prospective v3 conditional score-shape artifact
.venv/bin/python scripts/fit_score_grid_v3_shadow.py

# 10. Read the already-published production champion object without touching
#     the live warehouse (Railway injects storage credentials without printing)
railway run --service soccer_bot .venv/bin/python \
  scripts/download_prediction_snapshot.py \
  --output data/predictions/regulation_champion_v1/production_latest.json

# 11. Generate a private immutable v3 score-grid snapshot
.venv/bin/python scripts/predict_score_grid_v3_shadow.py \
  --parent-snapshot \
  data/predictions/regulation_champion_v1/production_latest.json
```

These commands write generated Parquet/JSON artifacts but do not mutate the
warehouse. Current production facts should be queried from Railway only under
the repository's stopped-scheduler, backup, temporary-inspection, and explicit
read-only procedure; the local DuckDB snapshot is not automatically synced
from production.

## 24. Implementation source map

| Concern | Source of truth |
|---|---|
| Contract semantics | `config/contracts/regulation_v1.json`, `src/soccer_bot/contracts.py` |
| Target definition and reviewed conflicts | `config/models/regulation_score_v1.json`, `config/models/regulation_score_exclusions_v1.json`, `src/soccer_bot/datasets/targets.py` |
| Eligibility | `migrations/006_fixture_model_eligibility.sql` |
| Dynamic team/competition state | `config/features/regulation_team_state_v1.json`, `src/soccer_bot/datasets/features.py` |
| Walk-forward rate scaling, Poisson, Dixon–Coles, metrics, bootstrap | `config/models/regulation_walk_forward_v1.json`, `src/soccer_bot/modeling/walk_forward.py` |
| xG/shots state and correction fit | `config/features/regulation_rich_rate_v1.json`, `src/soccer_bot/modeling/rich_rates.py` |
| Temperature calibration | `src/soccer_bot/modeling/calibration.py` |
| Champion refit and inference | `config/models/regulation_champion_v1.json`, `src/soccer_bot/modeling/production.py` |
| Upcoming schedule gates | `src/soccer_bot/datasets/upcoming.py` |
| Production artifact | `artifacts/production/regulation_champion_v1/model.json` |
| Frozen evaluation evidence | `data/features/regulation_team_state_v1/regulation_walk_forward_v1/rich_rate_v1/promoted_evaluation/report.json` |
| Market audit | `config/models/regulation_market_benchmark_v1.json`, `src/soccer_bot/modeling/markets.py` |
| Snapshot validation | `apps/api/snapshot_store.py`, `src/soccer_bot/prediction_publication.py` |
| V2 score-grid research | `config/models/regulation_score_grid_v2.json`, `src/soccer_bot/modeling/score_grid.py`, `scripts/research_score_grid_v2.py` |
| V3 conditional score model | `config/models/regulation_score_grid_v3_shadow.json`, `src/soccer_bot/modeling/score_grid_shadow.py` |
| V3 prospective gate | `config/models/regulation_score_grid_v3_prospective_gate.json` |
| V3 tracked artifact | `artifacts/production/regulation_score_grid_v3_shadow/model.json` |
| V3 refit and inference | `scripts/fit_score_grid_v3_shadow.py`, `scripts/predict_score_grid_v3_shadow.py` |
| Read-only production snapshot retrieval | `scripts/download_prediction_snapshot.py` |
| Prediction operational alerts | `src/soccer_bot/operational_alerts.py`, `scripts/check_public_prediction_health.py`, `OPERATIONAL_ALERTING.md` |

## 25. Quant-scientist audit checklist

Before interpreting or changing this model, answer these questions explicitly:

1. Is the claim about the **raw score distribution** or the **calibrated
   moneyline**? They are not the same distribution.
2. Is the evidence from expanding-window evaluation, the frozen final test, or
   an in-sample all-history refit? Only the first two support performance
   claims.
3. Does every proposed feature exist at the exact prediction cutoff, using its
   retrieval/observation timestamp rather than its eventual canonical value?
4. Is missingness preserved, or is a missing provider field being converted
   into a sporting zero?
5. Is the target fixture excluded from all of its own rolling states?
6. Are simultaneous fixtures batched so ordering cannot leak information?
7. Is a new rule being tuned against the already-opened final test?
8. Does a proposed market comparison use a timestamped, executable bid/ask and
   semantically identical settlement rules?
9. Is a probability claim conditional on a lineup/player appearing, or
   unconditional over participation uncertainty?
10. Does a new exact-score/total/spread calibration preserve a normalized
    coherent joint grid?
11. Are uncertainty intervals clustered at a level appropriate for repeated
    teams, competitions, and calendar regimes?
12. Can the result be reproduced from a versioned config, frozen dataset,
    warehouse identity, source hashes, artifact hash, and code revision?
13. Does a proposed score model compare moneyline against the promoted
    calibrated control rather than only the weaker raw Poisson grid?
14. If result marginals are preserved, is the within-result conditional law
    identified and normalized separately in all three regions?
15. Was the prospective decision gate frozen before the first eligible
    prediction, and are all scored rows provably pre-kickoff and post-freeze?
16. Is a shadow artifact being mistaken for a public production output or a
    retrospective training fit being mistaken for held-out evidence?

## 26. Bottom line

The current model is best understood as a disciplined statistical baseline
with a legitimately promoted enrichment layer:

- a chronological approximate-Bayesian attack/defense state model produces
  opponent-adjusted goal rates;
- global rate scales remove aggregate home/away bias;
- decayed, partial-pooled xG and shot matchup signals multiplicatively correct
  the rates through a regularized Poisson likelihood;
- independent Poisson converts rates to an auditable score distribution;
- three-way temperature scaling improves moneyline reliability;
- temporal development, calibration, and final-test governance prevent the
  usual random-split and in-sample-calibration errors;
- all-history refitting maximizes production information without relabeling
  training fit as held-out evidence;
- schedule, availability, conflict, and artifact checks fail closed.

The system now also has a disciplined route from calibrated result
probabilities to a coherent joint score distribution. The first unconstrained
attempt was rejected despite improving exact-score loss because it spent too
much moneyline calibration. V3 fixes the architecture rather than relaxing the
gate: it preserves the champion moneyline algebraically and learns only
within-result score shape. Its first 25 post-freeze horizon rows are immutable
prospective evidence, not a performance conclusion.

Its strongest virtue is not exotic model complexity. It is the explicit
information-time contract and the ability to explain every probability from
canonical evidence through a small number of equations. Its most important
remaining weaknesses are lineup/player absence, diagonal and plug-in parameter
uncertainty, conditional Poisson independence, incomplete rich-data coverage,
the unproven prospective quality of v3's conditional score shape, and the lack
of a complete timestamped market benchmark. Those limitations define the
correct next research program rather than reasons to overstate what either v1
or v3 already knows.

Operational continuity now has a separate two-plane watchdog. Each collector
run validates champion/shadow status, frozen identities, row counts, freshness,
receipt durability, and volume pressure; an independent scheduled public
heartbeat detects a Railway cron that no longer starts. These controls monitor
artifact production only. They do not inspect prospective outcomes and cannot
become a tuning channel around the frozen v3 decision gate.
