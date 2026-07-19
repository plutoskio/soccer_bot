# Regulation Score Specialist V1

Status: trained candidate; not production-approved
Recipe frozen: 2026-07-19
Prospective holdout starts: 2026-07-21

## What it estimates

The model estimates one regulation score distribution. Exact score, goal
marginals, totals, team totals, handicaps, and BTTS must all be calculated from
that same distribution. Its implied 1X2 probabilities may disagree with the
separate validated 1X2 champion; that difference is recorded, not forced away.

## Fit

| Horizon | Training fixtures | Iterations | Converged |
|---|---:|---:|---|
| T−24 | 15,458 | 4 | yes |
| Clean T−72 | 14,139 | 4 | yes |

The logical model hash is
`a8579d765fe0afca789403ceb1b22a82c7779ff0aef57ee76feca609ffee0d4e`.

## Approval boundary

Training success is not approval. The model was designed after the earlier
score-grid results were visible, so those results cannot provide an unbiased
promotion decision. The candidate must accumulate immutable forecasts for
eligible fixtures beginning 2026-07-21 and pass the frozen two-horizon gate in
`config/models/regulation_score_specialist_v1.json`.

The existing score-grid v3 track continues unchanged in parallel.
