"use client";

import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useState } from "react";
import {
  formatInteger,
  formatKickoffLong,
  formatMatchDate,
  formatPercent,
  formatRate,
  formatTimestamp,
  humanize,
} from "@/lib/format";
import { moneylineProbabilities, type FixtureGroup } from "@/lib/platform";
import type {
  FamilyStatus,
  InformationState,
  MatchContext,
  MarketQuote,
  ModelFamily,
  PlatformSnapshot,
  PlatformState,
  RecentMatch,
  TeamMatchContext,
  TeamTrend,
} from "@/lib/types";

const HORIZONS: { key: InformationState; short: string; label: string }[] = [
  { key: "pre_lineup_72h_clean_v1", short: "T−72", label: "Clean 72-hour forecast" },
  { key: "pre_lineup_24h_v1", short: "T−24", label: "24-hour forecast" },
];

const FAMILY_SHORT: Record<string, string> = {
  regulation_moneyline: "Match result",
  regulation_score: "Scores & goals",
  corners: "Corners",
  first_score_timing: "First team",
  player_events: "Players",
};

export function MatchDetail({
  fixture,
  snapshot,
}: {
  fixture: FixtureGroup;
  snapshot: PlatformSnapshot;
}) {
  const reducedMotion = useReducedMotion();
  const [requestedHorizon, setRequestedHorizon] = useState<InformationState>(
    fixture.states.pre_lineup_24h_v1 ? "pre_lineup_24h_v1" : "pre_lineup_72h_clean_v1",
  );
  const [selectedFamilyKey, setSelectedFamilyKey] = useState("regulation_moneyline");
  const [requestedGroup, setRequestedGroup] = useState("");
  const [trendWindow, setTrendWindow] = useState<"last_5" | "last_10">("last_5");

  const horizon = fixture.states[requestedHorizon]
    ? requestedHorizon
    : fixture.states.pre_lineup_24h_v1
      ? "pre_lineup_24h_v1"
      : "pre_lineup_72h_clean_v1";
  const state = fixture.states[horizon];
  if (!state) return null;
  const family =
    state.families.find((item) => item.family_key === selectedFamilyKey) ?? state.families[0];
  const groups = Array.from(new Set(family.markets.map((market) => market.group)));
  const activeGroup = groups.includes(requestedGroup) ? requestedGroup : groups[0] ?? "";
  const markets = family.markets.filter((market) => !activeGroup || market.group === activeGroup);
  const probabilities = moneylineProbabilities(state);
  const warnings = Array.isArray(family.evidence.warnings) ? family.evidence.warnings : [];
  const evidence = evidenceRows(family.evidence);

  function chooseFamily(key: string) {
    setSelectedFamilyKey(key);
    setRequestedGroup("");
  }

  return (
    <main className="page-shell match-page">
      <Link className="back-link" href="/">← All matches</Link>

      <motion.section
        className="match-heading"
        initial={reducedMotion ? false : { opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.42, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="match-heading-copy">
          <p className="section-label">{fixture.fixture.competition_name}</p>
          <h1>
            <span>{fixture.fixture.home_team_name}</span>
            <small>vs</small>
            <span>{fixture.fixture.away_team_name}</span>
          </h1>
          <p>{formatKickoffLong(state.kickoff)}</p>
        </div>
        <div className="horizon-control" aria-label="Prediction horizon">
          {HORIZONS.map((item) => {
            const available = Boolean(fixture.states[item.key]);
            return (
              <button
                type="button"
                key={item.key}
                disabled={!available}
                data-active={horizon === item.key}
                aria-pressed={horizon === item.key}
                title={available ? item.label : `${item.label} unavailable`}
                onClick={() => setRequestedHorizon(item.key)}
              >
                {item.short}
              </button>
            );
          })}
        </div>
      </motion.section>

      <motion.section
        className="primary-probability-section"
        key={horizon}
        initial={reducedMotion ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.3 }}
        aria-labelledby="result-probability-heading"
      >
        <div className="section-heading-row">
          <div>
            <p className="section-label">Regulation time</p>
            <h2 id="result-probability-heading">Match result</h2>
          </div>
          <StatusBadge status="validated" />
        </div>
        <div className="result-probabilities">
          <ResultProbability label={fixture.fixture.home_team_name} value={probabilities.home_win} />
          <ResultProbability label="Draw" value={probabilities.draw} />
          <ResultProbability label={fixture.fixture.away_team_name} value={probabilities.away_win} />
        </div>
      </motion.section>

      {state.match_context && (
        <MatchContextSection
          context={state.match_context}
          homeName={fixture.fixture.home_team_name}
          awayName={fixture.fixture.away_team_name}
          trendWindow={trendWindow}
          onTrendWindowChange={setTrendWindow}
        />
      )}

      {state.model_expectation && (
        <ModelView state={state} fixture={fixture} />
      )}

      <section className="match-section market-section" aria-labelledby="markets-heading">
        <div className="section-heading-row">
          <div>
            <p className="section-label">Forecasts</p>
            <h2 id="markets-heading">Available markets</h2>
          </div>
          <span className="section-meta">{family.markets.length} selections</span>
        </div>

        <div className="family-tabs" role="tablist" aria-label="Forecast families">
          {state.families.map((item) => (
            <button
              type="button"
              role="tab"
              aria-selected={family.family_key === item.family_key}
              data-active={family.family_key === item.family_key}
              data-status={item.status}
              key={item.family_key}
              onClick={() => chooseFamily(item.family_key)}
            >
              {FAMILY_SHORT[item.family_key] ?? item.display_name}
              <i aria-hidden="true" />
            </button>
          ))}
        </div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={`${horizon}-${family.family_key}`}
            initial={reducedMotion ? false : { opacity: 0, y: 5 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -3 }}
            transition={{ duration: 0.2 }}
          >
            <div className="family-summary">
              <div>
                <StatusBadge status={family.status} />
                <h3>{family.display_name}</h3>
                <p>{familyStatusCopy(family)}</p>
              </div>
              {groups.length > 1 && (
                <div className="group-control" aria-label="Market group">
                  {groups.map((group) => (
                    <button
                      type="button"
                      key={group}
                      data-active={activeGroup === group}
                      onClick={() => setRequestedGroup(group)}
                    >
                      {group}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {family.markets.length ? (
              <div className="market-list-clean">
                <div className="market-list-head" aria-hidden="true">
                  <span>Selection</span>
                  <span>Probability</span>
                  <span>Fair odds</span>
                  <span>Bookmakers</span>
                </div>
                {markets.map((market) => <MarketLine market={market} key={market.market_id} />)}
              </div>
            ) : (
              <UnavailableFamily family={family} />
            )}
          </motion.div>
        </AnimatePresence>
      </section>

      <section className="match-section detail-section" aria-labelledby="evidence-heading">
        <div className="section-heading-row">
          <div>
            <p className="section-label">Supporting evidence</p>
            <h2 id="evidence-heading">Model details</h2>
          </div>
        </div>
        <div className="detail-columns">
          <div className="detail-column">
            <h3>Data coverage</h3>
            <div className="evidence-rows">
              {evidence.length ? evidence.map(([label, value]) => (
                <div key={label}><span>{label}</span><strong>{value}</strong></div>
              )) : <p>No family-specific coverage metrics are available.</p>}
            </div>
          </div>
          <div className="detail-column">
            <h3>Forecast identity</h3>
            <dl className="definition-list">
              <div><dt>Model</dt><dd>{family.model_version}</dd></div>
              <div><dt>Information cutoff</dt><dd>{horizon === "pre_lineup_24h_v1" ? "T−24 hours" : "Clean T−72 hours"}</dd></div>
              <div><dt>Prediction time</dt><dd>{formatTimestamp(state.prediction_at)}</dd></div>
              <div><dt>Ranking</dt><dd>{family.eligible_for_ranking ? "Eligible" : "Excluded"}</dd></div>
            </dl>
          </div>
          <div className="detail-column">
            <h3>Bookmaker benchmark</h3>
            <BenchmarkSummary family={family} snapshot={snapshot} />
          </div>
        </div>
        {warnings.length > 0 && (
          <div className="forecast-warnings">
            <h3>Important notes</h3>
            <ul>{warnings.map((warning) => <li key={warning}>{warningCopy(warning)}</li>)}</ul>
          </div>
        )}
      </section>
    </main>
  );
}

function MatchContextSection({
  context,
  homeName,
  awayName,
  trendWindow,
  onTrendWindowChange,
}: {
  context: MatchContext;
  homeName: string;
  awayName: string;
  trendWindow: "last_5" | "last_10";
  onTrendWindowChange: (window: "last_5" | "last_10") => void;
}) {
  const reducedMotion = useReducedMotion();
  return (
    <section className="match-section context-section" aria-labelledby="context-heading">
      <div className="section-heading-row">
        <div>
          <p className="section-label">Before the cutoff</p>
          <h2 id="context-heading">How they arrive</h2>
        </div>
        <span className="section-meta">Only results available at forecast time</span>
      </div>

      <div className="preparation-grid">
        <PreparationTeam name={homeName} context={context.home} />
        <PreparationTeam name={awayName} context={context.away} />
      </div>

      <div className="recent-form-grid">
        <RecentForm name={homeName} matches={context.home.recent_matches} />
        <RecentForm name={awayName} matches={context.away.recent_matches} />
      </div>

      <div className="trend-heading">
        <div>
          <h3>Trend comparison</h3>
          <p>Regulation results available before this forecast.</p>
        </div>
        <div className="trend-control" aria-label="Trend window">
          {(["last_5", "last_10"] as const).map((window) => (
            <button
              type="button"
              key={window}
              data-active={trendWindow === window}
              aria-pressed={trendWindow === window}
              onClick={() => onTrendWindowChange(window)}
            >
              {window === "last_5" ? "Last 5" : "Last 10"}
            </button>
          ))}
        </div>
      </div>
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          className="trend-comparison"
          key={trendWindow}
          initial={reducedMotion ? false : { opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -3 }}
          transition={{ duration: 0.18 }}
        >
          <div className="trend-table-head">
            <strong>{homeName}</strong>
            <span>Metric</span>
            <strong>{awayName}</strong>
          </div>
          <TrendRow
            label="Record"
            home={recordLabel(context.home.trends[trendWindow])}
            away={recordLabel(context.away.trends[trendWindow])}
          />
          <TrendRow
            label="Goals scored / match"
            home={formatRate(context.home.trends[trendWindow].goals_for_per_match)}
            away={formatRate(context.away.trends[trendWindow].goals_for_per_match)}
          />
          <TrendRow
            label="Goals conceded / match"
            home={formatRate(context.home.trends[trendWindow].goals_against_per_match)}
            away={formatRate(context.away.trends[trendWindow].goals_against_per_match)}
          />
          <TrendRow
            label="Clean sheets"
            home={formatPercent(context.home.trends[trendWindow].clean_sheet_rate)}
            away={formatPercent(context.away.trends[trendWindow].clean_sheet_rate)}
          />
          <TrendRow
            label="Both teams scored"
            home={formatPercent(context.home.trends[trendWindow].both_teams_scored_rate)}
            away={formatPercent(context.away.trends[trendWindow].both_teams_scored_rate)}
          />
        </motion.div>
      </AnimatePresence>
    </section>
  );
}

function PreparationTeam({ name, context }: { name: string; context: TeamMatchContext }) {
  const lastMatch = context.recent_matches[0];
  return (
    <div className="preparation-team">
      <span>{name}</span>
      <strong>{context.rest_days === null ? "No prior match" : `${Math.round(context.rest_days)} days rest`}</strong>
      <small>
        {context.matches_last_14d} {context.matches_last_14d === 1 ? "match" : "matches"} in 14 days
        {lastMatch ? ` · Last played ${formatMatchDate(lastMatch.kickoff)}` : ""}
      </small>
    </div>
  );
}

function RecentForm({ name, matches }: { name: string; matches: RecentMatch[] }) {
  return (
    <div className="recent-form">
      <h3>{name}</h3>
      {matches.length ? (
        <div className="recent-match-list">
          {matches.map((match) => (
            <div className="recent-match" key={match.fixture_id}>
              <span className="form-result" data-outcome={match.outcome}>{match.outcome[0].toUpperCase()}</span>
              <time>{formatMatchDate(match.kickoff)}</time>
              <div>
                <strong>{match.opponent_name}</strong>
                <small>{match.competition_name} · {match.neutral_venue ? "Neutral" : match.venue === "home" ? "Home" : "Away"}</small>
              </div>
              <b>{match.team_score}–{match.opponent_score}</b>
            </div>
          ))}
        </div>
      ) : <p className="context-empty">No prior eligible results at this cutoff.</p>}
    </div>
  );
}

function TrendRow({ label, home, away }: { label: string; home: string; away: string }) {
  return (
    <div className="trend-row">
      <strong>{home}</strong>
      <span>{label}</span>
      <strong>{away}</strong>
    </div>
  );
}

function ModelView({ state, fixture }: { state: PlatformState; fixture: FixtureGroup }) {
  const expectation = state.model_expectation;
  if (!expectation) return null;
  const scoreFamily = state.families.find((item) => item.family_key === "regulation_score");
  const timingFamily = state.families.find((item) => item.family_key === "first_score_timing");
  const exactScore = scoreFamily?.markets.find((market) => market.contract_key === "regulation_exact_score");
  const btts = scoreFamily?.markets.find((market) => market.market_id === "regulation_both_teams_to_score:yes");
  const over = scoreFamily?.markets.find((market) => market.market_id === "regulation_total_goals:over:2.5");
  const first = timingFamily?.markets
    .filter((market) => market.probability !== null)
    .sort((left, right) => (right.probability ?? 0) - (left.probability ?? 0))[0];
  const firstLabels: Record<string, string> = {
    home_first: fixture.fixture.home_team_name,
    away_first: fixture.fixture.away_team_name,
    no_goal: "No goal",
  };
  const firstOutcome = typeof first?.selection.outcome === "string" ? first.selection.outcome : "";
  return (
    <section className="match-section model-view-section" aria-labelledby="model-view-heading">
      <div className="section-heading-row">
        <div>
          <p className="section-label">Forecast summary</p>
          <h2 id="model-view-heading">What the model expects</h2>
        </div>
        <StatusBadge status={scoreFamily?.status ?? "unavailable"} />
      </div>
      <div className="model-view-grid">
        <ModelViewItem
          label="Expected goals"
          value={`${expectation.expected_home_goals.toFixed(2)} – ${expectation.expected_away_goals.toFixed(2)}`}
          note={`${fixture.fixture.home_team_name} – ${fixture.fixture.away_team_name}`}
        />
        <ModelViewItem label="Most likely score" value={exactScore?.label ?? "—"} note={probabilityNote(exactScore)} />
        <ModelViewItem label="Over 2.5 goals" value={formatPercent(over?.probability ?? null)} note="Experimental score view" />
        <ModelViewItem label="Both teams score" value={formatPercent(btts?.probability ?? null)} note="Yes probability" />
        <ModelViewItem label="Scores first" value={firstLabels[firstOutcome] ?? "—"} note={probabilityNote(first)} />
      </div>
    </section>
  );
}

function ModelViewItem({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="model-view-item">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  );
}

function recordLabel(trend: TeamTrend) {
  return trend.sample_size ? `${trend.wins}–${trend.draws}–${trend.losses}` : "—";
}

function probabilityNote(market: MarketQuote | undefined) {
  return market?.probability === null || market?.probability === undefined
    ? "Unavailable at this cutoff"
    : `${formatPercent(market.probability)} probability`;
}

function ResultProbability({ label, value }: { label: string; value: number | null }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{formatPercent(value)}</strong>
    </div>
  );
}

function MarketLine({ market }: { market: MarketQuote }) {
  return (
    <div className="market-line">
      <div>
        <strong>{market.label}</strong>
        <small>{humanizeSelection(market.selection)}</small>
      </div>
      <span>{market.probability === null ? "—" : formatPercent(market.probability)}</span>
      <span>{market.fair_decimal_multiplier === null ? "—" : market.fair_decimal_multiplier.toFixed(2)}</span>
      <span>{market.market_comparison ? market.market_comparison.market_decimal_multiplier.toFixed(2) : "—"}</span>
    </div>
  );
}

function StatusBadge({ status }: { status: FamilyStatus }) {
  return <span className="status-badge" data-status={status}><i aria-hidden="true" />{status}</span>;
}

function BenchmarkSummary({ family, snapshot }: { family: ModelFamily; snapshot: PlatformSnapshot }) {
  const quotes = family.markets
    .map((market) => market.market_comparison)
    .filter((quote): quote is NonNullable<MarketQuote["market_comparison"]> => Boolean(quote));
  if (!quotes.length) {
    return (
      <p className="detail-copy">
        No complete three-way bookmaker consensus was captured before this prediction cutoff.
      </p>
    );
  }
  const latest = quotes.sort((left, right) => left.retrieved_at.localeCompare(right.retrieved_at)).at(-1);
  return (
    <div className="benchmark-copy">
      <strong>{quotes.length} cutoff {quotes.length === 1 ? "benchmark" : "benchmarks"}</strong>
      <p>
        Median of complete 1X2 books after removing each bookmaker&apos;s margin. This is comparison evidence, never a model input.
      </p>
      <small>Retrieved {formatTimestamp(latest?.retrieved_at ?? snapshot.as_of)}</small>
    </div>
  );
}

function UnavailableFamily({ family }: { family: ModelFamily }) {
  return (
    <div className="unavailable-clean">
      <span aria-hidden="true">—</span>
      <div>
        <h3>{family.display_name} is not available yet.</h3>
        <p>{familyStatusCopy(family)}</p>
      </div>
    </div>
  );
}

function familyStatusCopy(family: ModelFamily) {
  if (family.status === "validated") return "Passed its frozen approval test.";
  if (family.status === "experimental") return "Forward testing; excluded from automatic ranking.";
  switch (family.unavailable_reason) {
    case "prospective_holdout_not_started_for_this_fixture":
      return "The frozen forward-test period has not started for this fixture.";
    case "requires_two_timestamp_safe_confirmed_lineups":
      return "Both confirmed lineups must be captured safely before kickoff.";
    case "corner_feature_not_available_at_horizon":
      return "Safely timed corner history is incomplete at this horizon.";
    default:
      return "No safe forecast is available at this information state.";
  }
}

function warningCopy(warning: string) {
  switch (warning) {
    case "experimental_not_eligible_for_automatic_ranking":
      return "This estimate remains experimental and is excluded from automatic ranking.";
    case "home_corner_history_cold_start":
      return "Home-team corner history is sparse.";
    case "away_corner_history_cold_start":
      return "Away-team corner history is sparse.";
    default:
      return humanize(warning);
  }
}

function evidenceRows(evidence: Record<string, unknown>): [string, string][] {
  const labels: Record<string, string> = {
    training_fixtures: "Training matches",
    home_history_matches: "Home-team history",
    away_history_matches: "Away-team history",
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
