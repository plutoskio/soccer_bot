# API-Football Historical Backfill — Final Record

Completed: 2026-07-05 23:45:32 Europe/Luxembourg  
Verified against the live warehouse: 2026-07-06

## Scope

- Approved completed fixtures in the frozen manifest: **23,619**
- Historical execution batches: **1,181**
- Fixtures requiring detailed API retrieval: **22,753**
- Fixtures already complete before detailed execution: **866**
- Final batch checkpoint state: **1,181 succeeded, 0 pending, 0 failed**
- Requested fixture responses: **22,753**
- Returned fixture responses: **22,753**
- Relationally validated fixture responses: **22,753**
- API-Football fixture mappings now present in the warehouse: **23,726**

The 23,726 mappings include the 23,619 approved historical fixtures plus 107
additional watched-competition fixtures collected for audits, qualifiers,
current collection, and validation.

## Execution record

- First batch attempt: 2026-07-03 21:57:42 Europe/Luxembourg
- Final successful checkpoint: 2026-07-05 23:45:32 Europe/Luxembourg
- Successful executor runs: 21
- Failed executor runs during validation and retry work: 17

Failed attempts did not become successful checkpoints. The executor wrote a
batch checkpoint only after raw identity/structure validation, transactional
normalization, and post-load relational validation passed. Every final batch
checkpoint is successful.

## Frozen manifest identity

- Executor manifest SHA-256:
  `12028ad24de33e51e8a7a7b2a5a8030279b3278203e9907ccaa74ebd3989a27d`
- `data/staged/api_football_backfill_batches.json` SHA-256:
  `fd1b0f50abbfad4e585136e8b75a85f6802b4eab0b8956ccfd19b8f16e943fba`
- `data/staged/api_football_backfill_manifest.jsonl` SHA-256:
  `48993d164cc15c6b502378463250db78e75500e07857d59bf23ba48b2b385081`
- `data/staged/api_football_backfill_summary.json` SHA-256:
  `6b95e451e2e286b8e67da208a3d94a7afb3bf46ebe6cb26ce2fd15e70b43553c`

The executor manifest hash is derived from the batch and fixture-manifest files
using the executor's versioned hashing procedure; it is not the SHA-256 of one
individual file.

## Modeling eligibility for approved fixtures

| Eligibility state | Fixtures |
|---|---:|
| Pass result, team, and player eligibility | 23,526 |
| Pass result eligibility | 23,589 |
| Pass team eligibility | 23,535 |
| Pass player eligibility | 23,529 |
| Administrative result; match not played | 30 |
| Team and player data incomplete | 51 |
| Player data incomplete only | 9 |
| Team data incomplete only | 3 |

Eligibility is computed by `fixture_model_eligibility`. Feature construction
must additionally require each selected feature column to be non-null.

## Controlled quality warnings in the current warehouse

| Open warning | Fixtures |
|---|---:|
| Administrative result; match not played | 30 |
| Provider lineup duplicate normalized safely | 3 |
| Participating player not confidently linked to lineup identity | 501 |
| Player-stat section unavailable | 130 |
| Team-stat section unavailable | 2 |
| Player passing coverage below threshold | 8 |

These warnings document controlled provider limitations. They are not hidden
imputations and do not replace model eligibility or feature-specific null checks.

## Evidence and reproducibility

- Immutable API responses remain under `data/raw/api_football/`.
- The fixture manifest, batch definitions, and summary remain under
  `data/staged/`.
- Per-batch checkpoint, validation, raw-artifact, requested, returned, and
  validated counts remain in `historical_backfill_batch_checkpoint`.
- Run-level attempts remain in `historical_backfill_run`.
- Provider anomalies and resolved corrections remain in `data_quality_issue`.
- Historical coverage decisions remain in
  `reports/API_FOOTBALL_HISTORICAL_COVERAGE.json` and its Markdown report.

This report supersedes the pilot, 50-batch, 100-batch, 250-batch, and raw
remaining-run progress records. Those files described intermediate execution
states rather than the final warehouse.
