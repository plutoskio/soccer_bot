# Frontend QA Report — Probability Desk

| Field | Value |
|---|---|
| Date | 2026-07-15 |
| Application | Local production build plus live Railway deployment |
| Production URL | `https://soccer-bot-web-production.up.railway.app` |
| Desktop viewport | 1440 × 1000 |
| Mobile device | iPhone 14, 390 CSS pixels |
| Open critical/high/medium/low issues | 0 / 0 / 0 / 0 |

## Verified behavior

- The real champion snapshot renders six fixtures and ten horizon-specific
  prediction rows.
- Fixture selection updates names, kickoff, probabilities, coverage, warnings,
  and the model inspector.
- T−72 and T−24 selection updates the active information state. A fixture whose
  T−24 cutoff is not yet due disables that option and falls back to clean T−72.
- Calibrated home/draw/away probabilities, fair decimal odds, raw-to-calibrated
  movement, model version, logical hash prefix, cutoff, and coverage are visible.
- Snapshots older than six hours are labeled stale in amber; the application
  does not imply that an old artifact is live.
- The iPhone layout uses a horizontal fixture tape and has no document-level
  horizontal overflow (`scrollWidth == innerWidth == 390`).
- Controls are represented as accessible buttons with pressed, disabled, and
  current states. Reduced-motion mode preserves the entire information layout.
- Stopping the API and reloading produces the intentional fail-closed state;
  the page does not retain or invent probabilities.
- No application JavaScript errors were reported during the final production
  build pass.
- The live Railway page returned HTTP 200, loaded through the private API, and
  passed fixture/horizon interaction checks with no console or page errors.
- The data-sufficiency extension distinguishes fixture-specific team history,
  horizon-wide training size, and least-covered xG/shots depth. Changing from
  T−24 to T−72 updates the training set from 38,445 to 34,813 fixtures.
- The observed counts are interpreted only against thresholds stored in the
  immutable snapshot: 1,000 fit fixtures, five matches to exit team cold start,
  and 20 observations for full rich-signal depth.

## Resolved during QA

The first pass used JavaScript entrance animations whose initial `opacity: 0`
state remained in automated Chromium. That could hide the fixture tape and the
primary probability workspace. The dependency on JavaScript opacity state was
removed. Content is now visible by default; CSS entrance effects and probability
reflow are progressive enhancement, and reduced-motion mode is explicitly
supported.

## Evidence

- `screenshots/desktop-reduced-motion.png` — complete desktop workspace
- `screenshots/desktop-interaction.png` — post-interaction desktop state
- `screenshots/mobile-full.png` — complete responsive page
- `screenshots/mobile-api-unavailable.png` — fail-closed API error state
- `railway-production.png` — live Railway desktop workspace
- `railway-production-mobile.png` — live Railway mobile workspace
- `data-sufficiency-desktop.png` — local pre-deployment evidence extension
- `data-sufficiency-mobile.png` — local responsive evidence extension
- `data-sufficiency-production.png` — live production evidence extension
- `data-sufficiency-production-mobile.png` — live production mobile evidence

The red-box annotated first-pass image is retained as evidence of the resolved
entrance-state problem. It is not the release state.
