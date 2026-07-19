# First Score Timing V1

Status: trained candidate; mixed historical evidence; not production-approved
Recipe frozen: 2026-07-19
Prospective holdout starts: 2026-07-21

## Target and model

The safe target has three outcomes: home team scores first, away team scores
first, or no goal. A match enters training only when its detailed goal events
reconcile exactly to the canonical regulation score.

- 23,740 fixtures produced a safe team-level target.
- 14,694 result-eligible fixtures had no stored event artifact.
- 11 had an event artifact that did not completely reconcile to the score.
- Player first-scorer remains disabled because own-goal and on-pitch semantics
  are not yet safe enough.

The model begins with the teams' expected goal rates and the explicit no-goal
probability, then learns a small three-way correction.

## Historical audit

| Horizon | Fixtures | Log-loss delta | Paired month-block 95% interval | Brier direction |
|---|---:|---:|---:|---|
| T−24 | 9,683 | −0.00115 | [−0.00183, −0.00037] | worse |
| Clean T−72 | 8,882 | −0.00067 | [−0.00151, 0.00026] | worse |

The log loss is slightly better, but the clean T−72 interval crosses zero and
Brier score is slightly worse at both horizons. The result is not strong enough
for promotion. The raw goal-race baseline and corrected candidate should both
be recorded prospectively without changing the gate.

The all-history fit uses 14,315 T−24 rows and 13,072 clean T−72 rows. Its logical
hash is
`63e5e96732ba84b96ffb0a3827b9f181260dfdf1d7044ae1540094f59c07b457`.
