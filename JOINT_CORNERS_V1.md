# Joint Corners V1

Status: trained candidate; selected for forward shadow testing; not
production-approved
Recipe frozen: 2026-07-19
Prospective holdout starts: 2026-07-21

## Data

- 34,459 fixtures had a safe joint home/away regulation-corner target.
- 140 fixtures with provider disagreement were excluded and audited.
- The chronological feature artifact contains 34,459 T−24 rows and 31,152
  clean T−72 rows.
- Missing corners were never replaced with zero.

## Candidate result

The negative-binomial marginal model was selected for forward shadow testing.
In plain language, it allows much wider match-to-match variation than ordinary
Poisson. The dependence-only bivariate model learned essentially zero shared
intensity and did not improve joint score quality.

On the already-visible historical audit, negative-binomial minus Poisson joint
log-loss deltas were:

| Horizon | Mean delta | Paired month-block 95% interval |
|---|---:|---:|
| T−24 | −0.18011 | [−0.19752, −0.16119] |
| Clean T−72 | −0.18456 | [−0.20194, −0.16701] |

Total-corner ranked probability score also improved at both horizons. These
results justify selecting the forward challenger; they do not approve it,
because the audit period was known before the recipe was frozen.

The logical model hash is
`313501f07aea937f573544b648f5d3381dcb3fbd50a0a31f8c14d1edda7606d9`.
