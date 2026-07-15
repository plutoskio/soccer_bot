"use client";

import { motion, useReducedMotion } from "motion/react";
import { useMemo, useState } from "react";
import type { InformationState, Prediction, PredictionSnapshot } from "@/lib/types";

type FixtureGroup = {
  id: string;
  fixture: Prediction["fixture"];
  kickoff: string;
  predictions: Partial<Record<InformationState, Prediction>>;
};

const HORIZONS: { key: InformationState; short: string; label: string }[] = [
  { key: "pre_lineup_72h_clean_v1", short: "T−72", label: "Clean 72-hour view" },
  { key: "pre_lineup_24h_v1", short: "T−24", label: "24-hour view" },
];

const WARNING_LABELS: Record<string, string> = {
  home_team_cold_start: "Limited home-team history",
  away_team_cold_start: "Limited away-team history",
  xg_signal_unavailable_or_prior_only: "xG signal is unavailable or prior-only",
  shots_signal_unavailable_or_prior_only: "Shot signal is unavailable or prior-only",
  moneyline_calibration_not_score_grid_coherent: "Calibrated 1X2 is not a calibrated score grid",
};

export function ProbabilityDesk({ snapshot }: { snapshot: PredictionSnapshot }) {
  const reducedMotion = useReducedMotion();
  const fixtures = useMemo(() => groupFixtures(snapshot.predictions), [snapshot.predictions]);
  const [selectedId, setSelectedId] = useState(fixtures[0]?.id ?? "");
  const [requestedHorizon, setRequestedHorizon] = useState<InformationState>(
    snapshot.available_information_states.includes("pre_lineup_24h_v1")
      ? "pre_lineup_24h_v1"
      : "pre_lineup_72h_clean_v1",
  );

  const selected = fixtures.find((fixture) => fixture.id === selectedId) ?? fixtures[0];
  if (!selected) return <EmptyDesk snapshot={snapshot} />;
  const horizon = selected.predictions[requestedHorizon]
    ? requestedHorizon
    : selected.predictions.pre_lineup_72h_clean_v1
      ? "pre_lineup_72h_clean_v1"
      : "pre_lineup_24h_v1";
  const prediction = selected.predictions[horizon];
  if (!prediction) return <EmptyDesk snapshot={snapshot} />;

  const outcomes = [
    { key: "home", label: selected.fixture.home_team_name, probability: prediction.home_win_probability, raw: prediction.raw_home_win_probability },
    { key: "draw", label: "Draw", probability: prediction.draw_probability, raw: prediction.raw_draw_probability },
    { key: "away", label: selected.fixture.away_team_name, probability: prediction.away_win_probability, raw: prediction.raw_away_win_probability },
  ];
  const trainingEvidence = snapshot.training_evidence;
  const trainingFixtures = trainingEvidence.horizon_training_fixtures[horizon];
  const teamHistoryEnough = Math.min(
    prediction.home_history_matches,
    prediction.away_history_matches,
  ) >= trainingEvidence.team_cold_start_below_matches;
  const trainingSampleEnough = trainingFixtures >= trainingEvidence.minimum_training_fixtures;
  const leastXgHistory = Math.min(prediction.home_xg_history, prediction.away_xg_history);
  const leastShotsHistory = Math.min(prediction.home_shots_history, prediction.away_shots_history);
  const richSignalsFull = Math.min(leastXgHistory, leastShotsHistory) >= trainingEvidence.full_signal_history_matches;

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#workspace" aria-label="Soccer Bot probability desk">
          <span className="brand-mark" aria-hidden="true">SB</span><span>Soccer Bot</span>
        </a>
        <div className="topbar-meta">
          <span className="status-dot" data-stale={snapshot.is_stale} aria-hidden="true" />
          <span>{snapshot.is_stale ? "Snapshot stale" : "Snapshot"} · {formatAsOf(snapshot.as_of)}</span>
          <span className="desktop-only">Regulation · calibrated</span>
        </div>
      </header>

      <div className="desk-grid" id="workspace">
        <nav className="fixture-rail" aria-label="Upcoming fixtures">
          <div className="rail-heading"><p className="eyebrow">Fixture tape</p><span>{snapshot.fixture_count.toString().padStart(2, "0")}</span></div>
          <div className="fixture-list">
            {fixtures.map((fixture, index) => {
              const active = fixture.id === selected.id;
              return (
                <button
                  key={fixture.id}
                  type="button"
                  className="fixture-button"
                  style={{ animationDelay: reducedMotion ? "0ms" : `${index * 35}ms` }}
                  data-active={active}
                  aria-current={active ? "true" : undefined}
                  onClick={() => setSelectedId(fixture.id)}
                >
                  <span className="fixture-time">{formatKickoffShort(fixture.kickoff)}</span>
                  <span className="fixture-teams"><span>{fixture.fixture.home_team_name}</span><span>{fixture.fixture.away_team_name}</span></span>
                  <span className="fixture-horizons" aria-label="Available horizons">
                    {fixture.predictions.pre_lineup_72h_clean_v1 && <i>T−72</i>}
                    {fixture.predictions.pre_lineup_24h_v1 && <i>T−24</i>}
                  </span>
                </button>
              );
            })}
          </div>
        </nav>

        <section className="probability-workspace" aria-labelledby="fixture-title">
            <div
              className="workspace-frame"
              key={`${selected.id}-${horizon}`}
            >
              <div className="fixture-header">
                <div>
                  <p className="eyebrow">{selected.fixture.competition_name}</p>
                  <h1 id="fixture-title"><span>{selected.fixture.home_team_name}</span><em>vs</em><span>{selected.fixture.away_team_name}</span></h1>
                  <p className="kickoff-line">{formatKickoffLong(prediction.kickoff)}</p>
                </div>
                <div className="horizon-switch" aria-label="Prediction horizon">
                  {HORIZONS.map((item) => {
                    const available = Boolean(selected.predictions[item.key]);
                    return <button type="button" key={item.key} className={horizon === item.key ? "active" : ""} disabled={!available} aria-pressed={horizon === item.key} title={available ? item.label : `${item.label} not yet available`} onClick={() => setRequestedHorizon(item.key)}>{item.short}</button>;
                  })}
                </div>
              </div>

              <div className="market-heading">
                <div><p className="eyebrow">Regulation moneyline</p><h2>Sharp probabilities</h2></div>
                <p>Fair price · no bookmaker margin</p>
              </div>

              <div className="probability-field">
                {outcomes.map((outcome, index) => (
                  <div className="outcome" key={outcome.key}>
                    <div className="outcome-copy">
                      <span className="outcome-index">0{index + 1}</span><span className="outcome-name">{outcome.label}</span>
                      <strong>{formatPercent(outcome.probability)}</strong><span className="fair-odds">{formatOdds(outcome.probability)} fair</span>
                    </div>
                    <div className="probability-track" aria-hidden="true">
                      <motion.span initial={false} animate={{ width: `${outcome.probability * 100}%` }} transition={{ duration: reducedMotion ? 0 : 0.55, ease: [0.22, 1, 0.36, 1] }} />
                    </div>
                  </div>
                ))}
              </div>

              <div className="evidence-section">
                <div className="section-title"><p className="eyebrow">Model evidence</p><p>Only data knowable by {horizon === "pre_lineup_24h_v1" ? "T−24" : "clean T−72"}</p></div>
                <div className="evidence-grid">
                  <Evidence label="Expected goals" home={prediction.expected_home_goals} away={prediction.expected_away_goals} digits={2} />
                  <Evidence label="xG coverage" home={prediction.home_xg_history} away={prediction.away_xg_history} />
                  <Evidence label="Shot coverage" home={prediction.home_shots_history} away={prediction.away_shots_history} />
                </div>
              </div>

              <section className="sufficiency-section" aria-labelledby="sufficiency-title">
                <div className="section-title">
                  <p className="eyebrow" id="sufficiency-title">Data sufficiency</p>
                  <p>Thresholds from the frozen model recipe</p>
                </div>
                <div className="sufficiency-grid">
                  <DataDepth
                    label="Team result history"
                    value={`${prediction.home_history_matches} / ${prediction.away_history_matches}`}
                    detail="Home / away prior fixtures"
                    passed={teamHistoryEnough}
                    status={teamHistoryEnough ? "Cold-start minimum met" : `Below ${trainingEvidence.team_cold_start_below_matches}-match minimum`}
                  />
                  <DataDepth
                    label="Horizon training set"
                    value={formatInteger(trainingFixtures)}
                    detail="Eligible historical fixtures"
                    passed={trainingSampleEnough}
                    status={trainingSampleEnough ? `Passes ${formatInteger(trainingEvidence.minimum_training_fixtures)}-fixture minimum` : `Below ${formatInteger(trainingEvidence.minimum_training_fixtures)}-fixture minimum`}
                  />
                  <DataDepth
                    label="Least-covered team"
                    value={`${leastXgHistory} xG · ${leastShotsHistory} shots`}
                    detail="Prior rich-signal observations"
                    passed={richSignalsFull}
                    status={richSignalsFull ? "Full signal depth" : `Full depth starts at ${trainingEvidence.full_signal_history_matches}`}
                  />
                </div>
                <p className="sufficiency-verdict" data-pass={teamHistoryEnough}>
                  <strong>{teamHistoryEnough ? "Team-history minimum met." : "Sparse team-specific evidence."}</strong>{" "}
                  {teamHistoryEnough
                    ? "The fixture is outside cold start, but signal coverage still matters."
                    : "The global training sample passes its fitting minimum, but it cannot replace missing history for these teams; this estimate remains prior-heavy."}
                </p>
              </section>
            </div>
        </section>

        <aside className="model-inspector" aria-label="Model and prediction evidence">
          <div className="inspector-block"><p className="eyebrow">Information state</p><strong>{horizon === "pre_lineup_24h_v1" ? "T−24 hours" : "Clean T−72 hours"}</strong><p>Cutoff {formatTimestamp(prediction.prediction_at)}</p></div>
          <div className="inspector-block">
            <p className="eyebrow">Calibration movement</p>
            {outcomes.map((outcome) => <div className="calibration-row" key={outcome.key}><span>{outcome.key === "home" ? "Home" : outcome.key === "away" ? "Away" : "Draw"}</span><span>{formatSignedPoints(outcome.probability - outcome.raw)}</span></div>)}
            <p className="inspector-note">Change from raw score-model probability.</p>
          </div>
          <div className="inspector-block"><p className="eyebrow">Model identity</p><strong className="mono">{snapshot.model_version}</strong><p className="hash mono">{snapshot.logical_model_sha256.slice(0, 16)}…</p></div>
          <div className="inspector-block warning-block"><p className="eyebrow">Read before use</p><ul>{prediction.warnings.map((warning) => <li key={warning}>{WARNING_LABELS[warning] ?? humanizeWarning(warning)}</li>)}</ul></div>
          <div className="scope-note"><span aria-hidden="true">↗</span><p><strong>Current scope</strong> Regulation 1X2 only. Score-derived and player markets remain locked until their probability layers pass forward validation.</p></div>
        </aside>
      </div>
    </main>
  );
}

function Evidence({ label, home, away, digits = 0 }: { label: string; home: number; away: number; digits?: number }) {
  return <div className="evidence-row"><span>{label}</span><strong>{home.toFixed(digits)}</strong><i aria-hidden="true">/</i><strong>{away.toFixed(digits)}</strong></div>;
}

function DataDepth({ label, value, detail, passed, status }: { label: string; value: string; detail: string; passed: boolean; status: string }) {
  return <div className="data-depth"><p className="eyebrow">{label}</p><strong>{value}</strong><p>{detail}</p><span data-pass={passed}>{status}</span></div>;
}

function EmptyDesk({ snapshot }: { snapshot: PredictionSnapshot }) {
  return <main className="system-state"><div className="wordmark"><span aria-hidden="true">SB</span> Soccer Bot</div><p className="eyebrow">Snapshot {formatAsOf(snapshot.as_of)}</p><h1>No horizon is ready yet.</h1><p className="state-copy">Upcoming fixtures will appear only after a valid leakage-safe cutoff becomes due.</p></main>;
}

function groupFixtures(predictions: Prediction[]): FixtureGroup[] {
  const groups = new Map<string, FixtureGroup>();
  predictions.forEach((prediction) => {
    const existing = groups.get(prediction.fixture_id) ?? { id: prediction.fixture_id, fixture: prediction.fixture, kickoff: prediction.kickoff, predictions: {} };
    existing.predictions[prediction.information_state] = prediction;
    groups.set(prediction.fixture_id, existing);
  });
  return Array.from(groups.values()).sort((a, b) => new Date(a.kickoff).getTime() - new Date(b.kickoff).getTime());
}

const formatter = (options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Luxembourg", ...options });
function formatKickoffShort(value: string) { return formatter({ weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatKickoffLong(value: string) { return formatter({ weekday: "long", day: "2-digit", month: "long", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatTimestamp(value: string) { return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatAsOf(value: string) { return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function formatPercent(value: number) { return `${(value * 100).toFixed(1)}%`; }
function formatOdds(value: number) { return (1 / value).toFixed(2); }
function formatSignedPoints(value: number) { const points = value * 100; return `${points >= 0 ? "+" : ""}${points.toFixed(1)} pp`; }
function formatInteger(value: number) { return new Intl.NumberFormat("en-GB").format(value); }
function humanizeWarning(value: string) { return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()); }
