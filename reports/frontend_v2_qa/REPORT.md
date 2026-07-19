# Frontend V2 QA Report

Date: 2026-07-19
URL: `http://127.0.0.1:3000`
Scope: fixture selection, horizon switching, market-family navigation, unavailable states, desktop/mobile layout, console errors, and keyboard-accessible controls

## Summary

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 1 |
| Total | 2 |

## Issues

Both issues were fixed after reproduction and rechecked in the automated test/build pass.

### ISSUE-001: Unavailable reason combines two different conditions

- Severity: low
- Category: content / UX
- URL: `http://127.0.0.1:3000`
- Evidence: [desktop-unavailable-stable.png](screenshots/desktop-unavailable-stable.png)

The selected fixture had not started, but the message said “Prospective holdout not eligible or fixture already started.” The interface should report the exact condition so a user knows whether to wait for the holdout boundary or whether the match is already in play.

Reproduction:

1. Open the app and select “Scores & goals”.
2. Observe the unavailable-state heading for the upcoming fixture.

Resolution: the backend now emits the exact reason `prospective_holdout_not_started_for_this_fixture`; already-started fixtures are excluded before snapshot composition.

### ISSUE-002: Expandable market rows lose their button role

- Severity: medium
- Category: accessibility
- URL: `http://127.0.0.1:3000`
- Evidence: [mobile-expanded.png](screenshots/mobile-expanded.png)

The market row expands correctly with a pointer, but the accessibility tree exposes only table cells because the native button is overridden with `role="row"`. Keyboard and screen-reader users are not told that the row is an expandable button. It should keep a button semantic while the surrounding container supplies table structure.

Reproduction:

1. Filter the 1X2 market list to “Draw”.
2. Inspect the interactive accessibility tree: no button is exposed for the Draw row.
3. Click the Draw cell and observe that detail content does expand, confirming the hidden interaction.

Resolution: visual table roles were removed from the interactive control and each market row now retains its native button role with a descriptive accessible label and expansion state.
