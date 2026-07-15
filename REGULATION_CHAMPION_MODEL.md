# Regulation Champion Model — Production Handoff

## 1. Status

`regulation_champion_v1` is the first production-refit regulation-moneyline
model. Its recipe earned promotion in the frozen chronological evaluation before
the production refit was performed. Production parameters may be refit as new
eligible history arrives; the feature definitions, formula, calibration method,
and applicability policy may not be changed under this version.

The local 2026-07-15 refit used 38,445 eligible completed fixtures and produced:

| Horizon | Rows | Home scale | Away scale | xG coefficient | Shots coefficient | Temperature |
|---|---:|---:|---:|---:|---:|---:|
| T−24h | 38,445 | 1.00590 | 0.98445 | 0.04536 | 0.36744 | 1.18068 |
| Clean T−72h | 34,813 | 1.00360 | 0.98352 | 0.03573 | 0.36566 | 1.17176 |

The logical model hash is
`8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a`.
Generated model and prediction artifacts remain ignored by Git; their manifests
contain source paths and hashes.

## 2. Frozen recipe

For each home and away team, the chronological team-state engine estimates
opponent-adjusted attacking and defensive strength using only results available
by the prediction cutoff. It also estimates competition goal level and home
advantage and reports rest, congestion, uncertainty, and history depth.

The base rates are:

```text
base_home_rate = team_state_home_rate × fitted_home_scale
base_away_rate = team_state_away_rate × fitted_away_scale
```

Chronological Understat xG and API-Football shots states produce two
coverage-weighted log signals for each team. The champion correction is:

```text
home_rate = base_home_rate × exp(beta_xg × home_xg_signal
                                 + beta_shots × home_shots_signal)
away_rate = base_away_rate × exp(beta_xg × away_xg_signal
                                 + beta_shots × away_shots_signal)
```

An independent home/away Poisson score distribution produces raw regulation
home/draw/away probabilities. Frozen temperature scaling is then applied to the
three-way probabilities.

The production refit recomputes the global rate scales and rich coefficients on
all eligible history. This increases the legitimate training sample without
changing the evaluated recipe.

## 3. Calibration decision

The production refit deliberately reuses the temperatures fitted in the
evaluation calibration period:

- T−24h: `1.1806793063486158`
- clean T−72h: `1.171755567418676`

They are not re-estimated from in-sample probabilities after the all-history
refit. Doing that would create an optimistic calibrator without a new
cross-fitted procedure. A future calibration redesign is a new challenger and
requires new forward or nested evaluation.

Temperature scaling currently calibrates regulation moneyline only. It is not
backpropagated into the Poisson score grid, so calibrated moneyline and raw
score-derived markets are not fully distribution-coherent. Until a
distribution-level calibration method passes evaluation, this artifact exposes
only regulation moneyline as a supported calibrated output. Do not use it to
publish calibrated exact-score, spread, or total probabilities.

## 4. Upcoming-fixture inference policy

Upcoming inference replays the same historical state machines used in training.
It never creates a fake target score for an unplayed match.

A horizon is emitted only when all of the following hold:

1. The fixture is currently canonical `scheduled` and its kickoff is after
   `as_of` and within the configured seven-day window.
2. The horizon's exact anchor is due: T−72h or T−24h is at or before `as_of`.
3. The latest schedule observation available at that exact historical anchor
   contains the same kickoff as the current canonical fixture and says the
   fixture was scheduled.
4. For clean T−72h, neither team has another scheduled or completed fixture
   between the anchor and kickoff.
5. Historical results and xG/shots observations enter state only after the
   configured 150-minute post-kickoff availability delay. Simultaneous matches
   update as one order-invariant batch.

If a schedule was first discovered after the anchor, or a later reschedule is
being applied retrospectively, that horizon fails closed with a typed reason.

The output includes calibrated probabilities, raw Poisson probabilities,
expected goal rates, history depths, feature/model versions, exact timestamps,
and warnings. Its public training-evidence block records the horizon-specific
fit size plus the frozen sufficiency thresholds: 1,000 minimum fit fixtures,
fewer than five team matches as cold start, and 20 observations as full
xG/shots signal history. Cold-start or prior-only xG/shots rows are returned
with warnings; they are not manually suppressed or shrunk because such a rule
was not part of the evaluated recipe.

The application must display global and fixture-specific sample sizes
separately. For example, a T−24 model fit on 38,445 fixtures can still be
prior-heavy for a matchup where each team has only one eligible prior match.

## 5. Evaluation evidence

The richer xG/shots recipe was selected using an internal development validation
period ending before the calibration and final-test periods. Coefficients were
then refit on all development data, temperature was fit on calibration only,
and the final test was scored once.

Against calibrated independent Poisson, final-test log loss improved by:

- `−0.00453` at T−24h;
- `−0.00434` at clean T−72h.

Both paired calendar-month bootstrap 95% intervals were entirely below zero.
These held-out results belong to the recipe, not to the all-history fitted
artifact, which is never scored on its own training rows.

The strict timestamped Polymarket benchmark still has zero complete eligible
three-way fixture histories. Untimestamped Football-Data closing consensus is
retrospective only and remains about `0.042` log-loss points better on its
covered final-test subset. It is a performance target, not an eligible feature.

## 6. Commands and artifacts

Refit the frozen champion using the local read-only warehouse:

```bash
.venv/bin/python scripts/fit_regulation_champion.py
```

Generated artifacts:

```text
data/models/regulation_champion_v1/model.json
data/models/regulation_champion_v1/manifest.json
```

Create an upcoming snapshot:

```bash
.venv/bin/python scripts/predict_upcoming_regulation.py
```

For a reproducible historical run, pass an explicit timezone-aware timestamp:

```bash
.venv/bin/python scripts/predict_upcoming_regulation.py \
  --as-of 2026-07-15T00:30:00+00:00
```

Generated snapshots:

```text
data/predictions/regulation_champion_v1/latest.json
data/predictions/regulation_champion_v1/YYYYMMDDTHHMMSSZ.json
```

The 2026-07-15 00:30 UTC local snapshot emitted 10 due horizon rows across six
fixtures. This proves the inference path, not forward profitability.

## 7. Rules for future agents

- Do not tune new features, thresholds, shrinkage rules, calibration methods, or
  model families against the opened final test.
- Do not replace missing xG, shots, minutes, or results with invented zeroes.
- Do not use Football-Data closing odds as T−72h or T−24h features because their
  quote timestamps are unknown.
- Do not describe the all-history artifact's training fit as held-out model
  performance; cite the frozen evaluation report.
- A recipe change creates a new challenger version and needs new forward or
  nested evaluation evidence.
- The immutable `latest.json` snapshot is loaded through a fail-closed FastAPI
  boundary into the custom Next.js fixture-selection UI. The Railway
  object-storage/API/web rollout completed on 2026-07-15; the public UI exposes
  stale artifacts explicitly while the API stays private. The guarded
  post-collection publisher is live and has completed its first verified
  Railway cycle. The next quantitative
  step is distribution-level calibration and lineup/player availability,
  researched under a new evaluation window.
