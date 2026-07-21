import type { InformationState, PlatformSnapshot, PlatformState } from "./types";

export type FixtureGroup = {
  id: string;
  fixture: PlatformState["fixture"];
  kickoff: string;
  states: Partial<Record<InformationState, PlatformState>>;
};

export function groupFixtures(states: PlatformState[]): FixtureGroup[] {
  const fixtures = new Map<string, FixtureGroup>();
  for (const state of states) {
    const fixture = fixtures.get(state.fixture_id) ?? {
      id: state.fixture_id,
      fixture: state.fixture,
      kickoff: state.kickoff,
      states: {},
    };
    fixture.states[state.information_state] = state;
    fixtures.set(state.fixture_id, fixture);
  }
  return Array.from(fixtures.values()).sort(
    (left, right) => new Date(left.kickoff).getTime() - new Date(right.kickoff).getTime(),
  );
}

export function preferredState(fixture: FixtureGroup): PlatformState | undefined {
  return fixture.states.pre_lineup_24h_v1 ?? fixture.states.pre_lineup_72h_clean_v1;
}

export function moneylineProbabilities(state: PlatformState | undefined) {
  const family = state?.families.find((item) => item.family_key === "regulation_moneyline");
  const values: Record<string, number | null> = {
    home_win: null,
    draw: null,
    away_win: null,
  };
  for (const market of family?.markets ?? []) {
    const outcome = market.selection.outcome;
    if (typeof outcome === "string" && outcome in values) values[outcome] = market.probability;
  }
  return values;
}

export function groupFixturesByDay(fixtures: FixtureGroup[]) {
  const groups = new Map<string, FixtureGroup[]>();
  for (const fixture of fixtures) {
    const key = dayKey(fixture.kickoff);
    groups.set(key, [...(groups.get(key) ?? []), fixture]);
  }
  return Array.from(groups.entries());
}

export function snapshotLabel(snapshot: PlatformSnapshot) {
  return snapshot.is_stale ? "Snapshot delayed" : "Forecasts current";
}

const luxembourgDay = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Luxembourg",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function dayKey(value: string) {
  return luxembourgDay.format(new Date(value));
}
