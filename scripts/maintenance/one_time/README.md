# One-Time Maintenance Scripts

These scripts are retained as guarded audit and disaster-reconstruction tools.
They have already been executed against the historical warehouse and are not
part of normal collection, deployment, or modeling workflows.

- `repair_known_swapped_player_blocks.py` reproduces seven fixture-specific,
  evidence-backed player-stat block corrections while preserving the original
  raw responses.
- `remove_out_of_scope_discovery_fixtures.py` removes shallow fixtures that can
  appear if old unfiltered daily-discovery artifacts are replayed without the
  configured competition boundary.

Do not run either script against the local or Railway production warehouse
without first reviewing its guards and current assumptions. Stop production
collection, create and verify a database backup, test against a copy, and
compare protected invariants before and after execution.
