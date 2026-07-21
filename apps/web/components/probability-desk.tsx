"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useMemo, useState } from "react";
import type {
  FamilyStatus,
  InformationState,
  MarketQuote,
  ModelFamily,
  PlatformSnapshot,
  PlatformState,
} from "@/lib/types";

type FixtureGroup = {
  id: string;
  fixture: PlatformState["fixture"];
  kickoff: string;
  states: Partial<Record<InformationState, PlatformState>>;
};

const HORIZONS: { key: InformationState; short: string; label: string }[] = [
  { key: "pre_lineup_72h_clean_v1", short: "T−72", label: "Clean 72-hour view" },
  { key: "pre_lineup_24h_v1", short: "T−24", label: "24-hour view" },
];

const FAMILY_SHORT: Record<string, string> = {
  regulation_moneyline: "1X2",
  regulation_score: "Scores & goals",
  corners: "Corners",
  first_score_timing: "First team",
  player_events: "Players",
};

const STATUS_COPY: Record<FamilyStatus, string> = {
  validated: "Passed its frozen approval test",
  experimental: "Forward testing · excluded from ranking",
  unavailable: "No safe forecast at this information state",
  unsupported: "No approved model contract",
};

type UnavailableCopy = {
  marker: string;
  eyebrow: string;
  title: string;
  body: string;
};

export function ProbabilityDesk({ snapshot: initialSnapshot }: { snapshot: PlatformSnapshot }) {
  const reducedMotion = useReducedMotion();
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const fixtures = useMemo(() => groupFixtures(snapshot.states), [snapshot.states]);
  const [selectedId, setSelectedId] = useState(fixtures[0]?.id ?? "");
  const [requestedHorizon, setRequestedHorizon] = useState<InformationState>(
    snapshot.available_information_states.includes("pre_lineup_24h_v1")
      ? "pre_lineup_24h_v1"
      : "pre_lineup_72h_clean_v1",
  );
  const [selectedFamilyKey, setSelectedFamilyKey] = useState("regulation_moneyline");
  const [requestedGroup, setRequestedGroup] = useState("");
  const [query, setQuery] = useState("");
  const [expandedMarket, setExpandedMarket] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function refreshSnapshot() {
      try {
        const response = await fetch("/api/platform-snapshot", {
          cache: "no-store",
          headers: { accept: "application/json" },
        });
        if (!response.ok) return;
        const next = (await response.json()) as PlatformSnapshot;
        if (active && next.snapshot_version === "specialized_bet_platform_snapshot_v1") {
          setSnapshot(next);
        }
      } catch {
        // Preserve the last validated snapshot through transient refresh failures.
      }
    }
    const interval = window.setInterval(refreshSnapshot, 60_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const selected = fixtures.find((fixture) => fixture.id === selectedId) ?? fixtures[0];
  if (!selected) return <EmptyDesk snapshot={snapshot} />;
  const horizon = selected.states[requestedHorizon]
    ? requestedHorizon
    : selected.states.pre_lineup_24h_v1
      ? "pre_lineup_24h_v1"
      : "pre_lineup_72h_clean_v1";
  const state = selected.states[horizon];
  if (!state) return <EmptyDesk snapshot={snapshot} />;
  const family =
    state.families.find((item) => item.family_key === selectedFamilyKey) ??
    state.families[0];
  const groups = Array.from(new Set(family.markets.map((market) => market.group)));
  const activeGroup = groups.includes(requestedGroup) ? requestedGroup : groups[0] ?? "";
  const visibleMarkets = family.markets.filter((market) => {
    const matchesGroup = !activeGroup || market.group === activeGroup;
    const needle = query.trim().toLocaleLowerCase();
    return matchesGroup && (!needle || `${market.label} ${market.group}`.toLocaleLowerCase().includes(needle));
  });

  function chooseFixture(id: string) {
    setSelectedId(id);
    setExpandedMarket(null);
    setRequestedGroup("");
    setQuery("");
  }

  function chooseFamily(key: string) {
    setSelectedFamilyKey(key);
    setExpandedMarket(null);
    setRequestedGroup("");
    setQuery("");
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#workspace" aria-label="Soccer Bot bet research desk">
          <span className="brand-mark" aria-hidden="true">SB</span>
          <span>Soccer Bot</span>
        </a>
        <div className="topbar-context">
          <span>Bet research desk</span>
          <i aria-hidden="true" />
          <span>Regulation</span>
        </div>
        <div className="snapshot-status">
          <span className="status-dot" data-stale={snapshot.is_stale} aria-hidden="true" />
          <span>{snapshot.is_stale ? "Snapshot stale" : "Snapshot current"}</span>
          <time>{formatAsOf(snapshot.as_of)}</time>
        </div>
      </header>

      <div className="desk-grid" id="workspace">
        <nav className="fixture-rail" aria-label="Upcoming fixtures">
          <div className="rail-heading">
            <p className="eyebrow">Fixtures</p>
            <span>{snapshot.fixture_count.toString().padStart(2, "0")}</span>
          </div>
          <div className="fixture-list">
            {fixtures.map((fixture, index) => {
              const active = fixture.id === selected.id;
              return (
                <button
                  key={fixture.id}
                  type="button"
                  className="fixture-button"
                  style={{ animationDelay: reducedMotion ? "0ms" : `${index * 32}ms` }}
                  data-active={active}
                  aria-current={active ? "true" : undefined}
                  onClick={() => chooseFixture(fixture.id)}
                >
                  <span className="fixture-time">{formatKickoffShort(fixture.kickoff)}</span>
                  <span className="fixture-competition">{fixture.fixture.competition_name}</span>
                  <span className="fixture-teams">
                    <span>{fixture.fixture.home_team_name}</span>
                    <span>{fixture.fixture.away_team_name}</span>
                  </span>
                  <span className="fixture-horizons" aria-label="Available horizons">
                    {fixture.states.pre_lineup_72h_clean_v1 && <i>T−72</i>}
                    {fixture.states.pre_lineup_24h_v1 && <i>T−24</i>}
                  </span>
                </button>
              );
            })}
          </div>
        </nav>

        <section className="research-workspace" aria-labelledby="fixture-title">
          <motion.div
            className="workspace-frame"
            key={`${selected.id}-${horizon}`}
            initial={reducedMotion ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
          >
            <div className="fixture-header">
              <div>
                <p className="eyebrow">{selected.fixture.competition_name}</p>
                <h1 id="fixture-title">
                  <span>{selected.fixture.home_team_name}</span>
                  <em>vs</em>
                  <span>{selected.fixture.away_team_name}</span>
                </h1>
                <p className="kickoff-line">{formatKickoffLong(state.kickoff)}</p>
              </div>
              <div className="horizon-switch" aria-label="Prediction horizon">
                {HORIZONS.map((item) => {
                  const available = Boolean(selected.states[item.key]);
                  return (
                    <button
                      type="button"
                      key={item.key}
                      className={horizon === item.key ? "active" : ""}
                      disabled={!available}
                      aria-pressed={horizon === item.key}
                      title={available ? item.label : `${item.label} not available`}
                      onClick={() => {
                        setRequestedHorizon(item.key);
                        setExpandedMarket(null);
                      }}
                    >
                      {item.short}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="family-nav" role="tablist" aria-label="Bet families">
              {state.families.map((item) => {
                const active = family.family_key === item.family_key;
                return (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={active}
                    key={item.family_key}
                    data-status={item.status}
                    onClick={() => chooseFamily(item.family_key)}
                  >
                    {active && <motion.span className="family-active" layoutId="family-active" />}
                    <span>{FAMILY_SHORT[item.family_key] ?? item.display_name}</span>
                    <i aria-label={item.status} />
                  </button>
                );
              })}
            </div>

            <div className="family-intro">
              <div>
                <div className="status-line">
                  <StatusLabel status={family.status} />
                  <span>{familyStatusCopy(family)}</span>
                </div>
                <h2>{family.display_name}</h2>
              </div>
              <div className="family-count">
                <strong>{family.markets.length}</strong>
                <span>priced selections</span>
              </div>
            </div>

            {family.markets.length > 0 ? (
              <>
                <div className="market-controls">
                  <div className="group-switch" aria-label="Market group">
                    {groups.map((group) => (
                      <button
                        type="button"
                        key={group}
                        data-active={activeGroup === group}
                        onClick={() => {
                          setRequestedGroup(group);
                          setExpandedMarket(null);
                        }}
                      >
                        {group}
                      </button>
                    ))}
                  </div>
                  <div className="market-tools">
                    <label className="market-search">
                      <span className="sr-only">Search bets</span>
                      <input
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder="Filter bets"
                      />
                      <kbd>/</kbd>
                    </label>
                  </div>
                </div>

                <div className="market-table" aria-label={`${activeGroup} probabilities`}>
                  <div className="market-table-head" aria-hidden="true">
                    <span>Selection</span>
                    <span>Probability</span>
                    <span>Fair multiplier</span>
                    <span>Bookmaker consensus</span>
                  </div>
                  <AnimatePresence initial={false}>
                    {visibleMarkets.map((market) => (
                      <MarketRow
                        key={market.market_id}
                        market={market}
                        expanded={expandedMarket === market.market_id}
                        onToggle={() => setExpandedMarket(
                          expandedMarket === market.market_id ? null : market.market_id,
                        )}
                        reducedMotion={Boolean(reducedMotion)}
                      />
                    ))}
                  </AnimatePresence>
                  {visibleMarkets.length === 0 && (
                    <p className="no-markets">No selections match this filter.</p>
                  )}
                </div>
              </>
            ) : (
              <UnavailableFamily family={family} />
            )}
          </motion.div>
        </section>

        <ModelInspector family={family} state={state} snapshot={snapshot} />
      </div>
    </main>
  );
}

function MarketRow({
  market,
  expanded,
  onToggle,
  reducedMotion,
}: {
  market: MarketQuote;
  expanded: boolean;
  onToggle: () => void;
  reducedMotion: boolean;
}) {
  const quote = market.market_comparison;
  return (
    <motion.div layout={!reducedMotion} className="market-row-wrap">
      <button
        type="button"
        className="market-row"
        aria-expanded={expanded}
        aria-label={`${market.label}, ${market.probability === null ? "settlement-priced" : formatPercent(market.probability)}, fair multiplier ${market.fair_decimal_multiplier === null ? "infinite" : market.fair_decimal_multiplier.toFixed(2)}`}
        onClick={onToggle}
      >
        <span className="market-name">
          <i aria-hidden="true">{expanded ? "−" : "+"}</i>
          <strong>{market.label}</strong>
        </span>
        <span className="market-probability">
          {market.probability === null ? <small>settled price</small> : formatPercent(market.probability)}
        </span>
        <span className="market-fair">
          {market.fair_decimal_multiplier === null ? "∞" : market.fair_decimal_multiplier.toFixed(2)}
        </span>
        <motion.span
          className="market-external"
          key={quote?.retrieved_at ?? "consensus-missing"}
          initial={reducedMotion ? false : { opacity: 0.35 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.28 }}
        >
          {quote ? quote.market_decimal_multiplier.toFixed(2) : "—"}
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            className="market-detail"
            initial={reducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <div>
              <p className="eyebrow">Selection contract</p>
              <code>{humanizeSelection(market.selection)}</code>
            </div>
            {market.settlement_probabilities && (
              <div>
                <p className="eyebrow">Settlement probability</p>
                <p className="settlement-line">
                  {Object.entries(market.settlement_probabilities)
                    .filter(([, value]) => value > 0.00005)
                    .map(([key, value]) => `${humanize(key)} ${formatPercent(value)}`)
                    .join(" · ")}
                </p>
              </div>
            )}
            <div>
              <p className="eyebrow">Fair multiplier</p>
              <p className="settlement-line">
                {market.fair_decimal_multiplier === null ? "∞" : market.fair_decimal_multiplier.toFixed(2)}
              </p>
            </div>
            <div>
              <p className="eyebrow">Bookmaker benchmark</p>
              <p>{quoteSummary(market.market_comparison)}</p>
              <p className="settlement-line">Median of complete 1X2 books after proportional margin removal. Benchmark only; never a model feature.</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function ModelInspector({
  family,
  state,
  snapshot,
}: {
  family: ModelFamily;
  state: PlatformState;
  snapshot: PlatformSnapshot;
}) {
  const evidence = evidenceRows(family.evidence);
  const warnings = Array.isArray(family.evidence.warnings) ? family.evidence.warnings : [];
  const cutoffQuotes = family.markets.filter((market) => market.market_comparison).length;
  const latestConsensusRetrievedAt = family.markets
    .map((market) => market.market_comparison?.retrieved_at)
    .filter((value): value is string => Boolean(value))
    .sort()
    .at(-1);
  return (
    <aside className="model-inspector" aria-label="Model and data evidence">
      <div className="inspector-heading">
        <p className="eyebrow">Estimate source</p>
        <StatusLabel status={family.status} />
      </div>
      <div className="inspector-block model-identity">
        <strong>{family.model_version}</strong>
        {family.logical_model_sha256 && <code>{family.logical_model_sha256.slice(0, 18)}…</code>}
        <p>{familyStatusCopy(family)}.</p>
      </div>
      <div className="inspector-block">
        <p className="eyebrow">Information cutoff</p>
        <strong>{state.information_state === "pre_lineup_24h_v1" ? "T−24 hours" : "Clean T−72 hours"}</strong>
        <p>{formatTimestamp(state.prediction_at)}</p>
      </div>
      <div className="inspector-block evidence-list">
        <p className="eyebrow">Data depth</p>
        {evidence.length ? evidence.map(([label, value]) => (
          <div key={label}><span>{label}</span><strong>{value}</strong></div>
        )) : <p>No family-specific depth is available.</p>}
      </div>
      <div className="inspector-block">
        <p className="eyebrow">Market evidence</p>
        <strong>{cutoffQuotes ? `${cutoffQuotes} cutoff ${cutoffQuotes === 1 ? "benchmark" : "benchmarks"}` : "No cutoff benchmark"}</strong>
        <p>
          {cutoffQuotes
            ? `API-Football 1X2 consensus retrieved before the model cutoff at ${formatTimestamp(latestConsensusRetrievedAt ?? snapshot.as_of)}.`
            : "No complete three-way bookmaker consensus was captured safely before this prediction cutoff."}
        </p>
      </div>
      {warnings.length > 0 && (
        <div className="inspector-block warning-block">
          <p className="eyebrow">Read before use</p>
          <ul>{warnings.map((warning) => <li key={warning}>{warningCopy(warning)}</li>)}</ul>
        </div>
      )}
      <div className="inspector-footer">
        <span>{snapshot.ranking_policy === "validated_families_only" ? "Validated only" : "Unsafe policy"}</span>
        <p>Automatic ranking excludes every experimental estimate.</p>
      </div>
    </aside>
  );
}

function UnavailableFamily({ family }: { family: ModelFamily }) {
  const copy = unavailableFamilyCopy(family);
  return (
    <div className="unavailable-family">
      <span aria-hidden="true">{copy.marker}</span>
      <div>
        <p className="eyebrow">{copy.eyebrow}</p>
        <h3>{copy.title}</h3>
        <p>{copy.body}</p>
      </div>
    </div>
  );
}

function familyStatusCopy(family: ModelFamily) {
  if (family.status !== "unavailable") return STATUS_COPY[family.status];
  switch (family.unavailable_reason) {
    case "prospective_holdout_not_started_for_this_fixture":
      return "Model trained · forward test scheduled";
    case "requires_two_timestamp_safe_confirmed_lineups":
      return "Waiting for both confirmed lineups";
    case "corner_feature_not_available_at_horizon":
      return "Fixture data requirement not met";
    default:
      return STATUS_COPY.unavailable;
  }
}

function unavailableFamilyCopy(family: ModelFamily): UnavailableCopy {
  if (family.unavailable_reason === "prospective_holdout_not_started_for_this_fixture") {
    const holdoutStart = family.evidence.prospective_holdout_start;
    return {
      marker: "→",
      eyebrow: "Forward test scheduled",
      title: typeof holdoutStart === "string"
        ? `Predictions start ${formatAvailabilityStart(holdoutStart)}`
        : "Predictions start when the forward test opens",
      body: "This model is trained. After that time, the next five-minute publication cycle will show eligible future fixtures here. Until then, its estimates are intentionally hidden from betting use.",
    };
  }
  if (family.unavailable_reason === "requires_two_timestamp_safe_confirmed_lineups") {
    return {
      marker: "XI",
      eyebrow: "Confirmed lineups required",
      title: "Waiting for both confirmed lineups",
      body: "Player goal and assist forecasts are published only when both teams’ confirmed lineups were captured safely before kickoff. This is a separate data requirement, not a failed score model.",
    };
  }
  if (family.unavailable_reason === "corner_feature_not_available_at_horizon") {
    return {
      marker: "…",
      eyebrow: "Match data incomplete",
      title: "Corner history is not ready at this horizon",
      body: "This fixture does not have enough safely timed corner information for a forecast. Other fixtures can still become available when their data requirements are met.",
    };
  }
  return {
    marker: "×",
    eyebrow: "Forecast unavailable",
    title: humanize(family.unavailable_reason ?? "No safe prediction"),
    body: "The desk does not fill missing evidence with assumptions. This family will appear when its exact data and timing rules are satisfied.",
  };
}

function warningCopy(warning: string) {
  switch (warning) {
    case "prospective_holdout_not_started_for_this_fixture":
      return "No estimate is published before the frozen forward-test start.";
    case "requires_two_timestamp_safe_confirmed_lineups":
      return "Both confirmed lineups must be captured safely before kickoff.";
    case "corner_feature_not_available_at_horizon":
      return "Safely timed corner history is incomplete for this fixture.";
    default:
      return humanize(warning);
  }
}

function quoteSummary(quote: MarketQuote["market_comparison"]) {
  if (!quote) return "Consensus: unavailable";
  return `Consensus: ${formatPercent(quote.market_probability)} · fair ${quote.market_decimal_multiplier.toFixed(2)} · ${quote.bookmaker_count} books · ${formatTimestamp(quote.retrieved_at)}`;
}

function StatusLabel({ status }: { status: FamilyStatus }) {
  return <span className="status-label" data-status={status}><i aria-hidden="true" />{status}</span>;
}

function EmptyDesk({ snapshot }: { snapshot: PlatformSnapshot }) {
  return (
    <main className="system-state">
      <div className="wordmark"><span aria-hidden="true">SB</span> Soccer Bot</div>
      <p className="eyebrow">Snapshot {formatAsOf(snapshot.as_of)}</p>
      <h1>No prediction horizon is ready.</h1>
      <p className="state-copy">Fixtures appear only after a valid pre-match cutoff is available.</p>
    </main>
  );
}

function groupFixtures(states: PlatformState[]): FixtureGroup[] {
  const groups = new Map<string, FixtureGroup>();
  states.forEach((state) => {
    const existing = groups.get(state.fixture_id) ?? {
      id: state.fixture_id,
      fixture: state.fixture,
      kickoff: state.kickoff,
      states: {},
    };
    existing.states[state.information_state] = state;
    groups.set(state.fixture_id, existing);
  });
  return Array.from(groups.values()).sort(
    (a, b) => new Date(a.kickoff).getTime() - new Date(b.kickoff).getTime(),
  );
}

function evidenceRows(evidence: Record<string, unknown>): [string, string][] {
  const labels: Record<string, string> = {
    training_fixtures: "Training matches",
    home_history_matches: "Home history",
    away_history_matches: "Away history",
    home_xg_history: "Home xG history",
    away_xg_history: "Away xG history",
    home_shots_history: "Home shot history",
    away_shots_history: "Away shot history",
    expected_home_corners: "Expected home corners",
    expected_away_corners: "Expected away corners",
    competition_history_matches: "Competition history",
    selected_candidate: "Model shape",
  };
  return Object.entries(labels).flatMap(([key, label]) => {
    const value = evidence[key];
    if (typeof value === "number") {
      return [[label, Number.isInteger(value) ? formatInteger(value) : value.toFixed(2)] as [string, string]];
    }
    if (typeof value === "string") return [[label, humanize(value)] as [string, string]];
    return [];
  });
}

function humanizeSelection(value: Record<string, string | number>) {
  return Object.entries(value).map(([key, item]) => `${humanize(key)}: ${item}`).join(" · ");
}

const formatter = (options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Luxembourg", ...options });
function formatKickoffShort(value: string) { return formatter({ weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatKickoffLong(value: string) { return formatter({ weekday: "long", day: "2-digit", month: "long", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatTimestamp(value: string) { return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatAvailabilityStart(value: string) { return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatAsOf(value: string) { return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatPercent(value: number) { return `${(value * 100).toFixed(1)}%`; }
function formatInteger(value: number) { return new Intl.NumberFormat("en-GB").format(value); }
function humanize(value: string) { return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()); }
