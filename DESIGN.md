# Soccer Bot Interface Design Contract

This file is the durable frontend handoff for the custom forecasting platform.
It follows the installed OpenAI `frontend-skill`; future agents should preserve
the thesis and tokens unless the user explicitly approves a redesign.

## Visual thesis

A restrained football trading desk: midnight ink, floodlit ivory typography,
and one sharp chartreuse signal color. It should feel analytical and editorial,
not like a sportsbook promotion or a generic SaaS dashboard.

The dominant visual is the selected fixture's three-way probability field. It
must be understandable before secondary metrics, model metadata, or warnings.

## Content plan

1. **Navigation:** brand, snapshot freshness, and fixtures grouped by kickoff.
2. **Primary workspace:** selected teams, competition, kickoff, horizon control,
   home/draw/away probability field, and fair odds.
3. **Evidence:** expected goals, team histories, xG/shots coverage, global
   horizon training size, explicit sufficiency thresholds, and typed
   applicability warnings.
4. **Inspector:** model version, prediction cutoff, raw versus calibrated
   probabilities, and distribution limitation.

This is an operational product surface, so there is no marketing hero. Headings
and labels must orient the user without aspirational copy.

## Interaction thesis

- The workspace enters in one short stagger: fixture context, probability field,
  then evidence. Motion should make hierarchy legible, not theatrical.
- Changing T−72h/T−24h smoothly reallocates the probability field and updates
  evidence without a page reload.
- Fixture selection uses a precise active rail and shared layout transition;
  mobile navigation becomes a compact horizontal fixture strip.

All motion respects `prefers-reduced-motion`.

## Design tokens

```text
ink-950       #080b0d   page and deepest surface
ink-900       #101518   primary workspace
ink-800       #1a2225   selected/interactive surface
bone-100      #f2f0e8   primary text
bone-300      #c7c5bb   secondary text
signal        #c7ff32   one action/state accent
warning       #ffb45b   applicability warning only
danger        #ff6f61   stale/error only
divider       rgba(242, 240, 232, 0.12)
```

Typography uses at most two families:

- Space Grotesk for product name, fixture names, probabilities, and headings.
- IBM Plex Mono for timestamps, model identifiers, odds, and diagnostic labels.

Spacing is based on a 4px unit. Major regions use whitespace and dividers rather
than nested cards or thick borders.

## Layout

Desktop uses three functional regions:

```text
fixture rail (280px) | primary workspace (fluid) | evidence inspector (320px)
```

The probability field is a single horizontal composition, not three separate
cards. Evidence is shown as divided rows and columns. Cards are allowed only
where the whole surface is an interaction, such as a selectable fixture.

On mobile, the fixture rail becomes a horizontal strip, the workspace comes
first, and the inspector follows as a plain divided section.

## Product-language rules

- Say `Snapshot 4 min ago`, not `Real-time intelligence`.
- Say `Limited history`, not `Low-confidence AI prediction`.
- Say `Unavailable at this cutoff`, not `Coming soon`.
- Distinguish global training size from fixture-specific team history. Never
  imply that a large global sample compensates for a cold-start team.
- Base sufficiency labels on frozen recipe thresholds, and display the threshold
  beside the observed count instead of inventing an unexplained confidence
  score.
- Always show whether a probability is calibrated, its horizon, and its cutoff.
- Never imply guaranteed edge, accuracy, or profitability.

## Hard constraints

- No dashboard-card mosaic, decorative gradient wallpaper, pill soup, or fake
  live indicators.
- No more than one accent color in routine UI.
- No market odds in the independent-model area unless their timestamp and
  semantic mapping are valid.
- Cold-start and missing-stat warnings must remain visible near the probability.
- The app reads immutable snapshots and never trains or writes the warehouse.
- Unsupported contracts fail closed; regulation moneyline is the only supported
  calibrated output in `regulation_champion_v1`.

## Review checklist

- Can a user select a fixture and read the three outcomes in two interactions or
  fewer?
- Is the probability field the unmistakable visual anchor?
- Can headings, labels, and numbers alone explain the screen?
- Are warnings specific and actionable?
- Can the user see both the horizon training size and each team's available
  pre-cutoff history, with an honest pass/below-threshold interpretation?
- Is every card genuinely interactive?
- Does the layout work at 390px, 768px, 1440px, and 1728px widths?
- Are keyboard focus, contrast, reduced motion, and loading/error states present?
