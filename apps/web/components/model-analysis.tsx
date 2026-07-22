"use client";

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useState } from "react";
import type {
  HistoryFixture,
  HistoryMarket,
  HistoryPredictionGroup,
  PredictionHistory,
} from "@/lib/history";
import type {
  AnalysisHorizon,
  AnalysisMetric,
  AnalysisMetricKey,
  ModelAnalysisSnapshot,
} from "@/lib/model-analysis";

const METRICS: { key: AnalysisMetricKey; label: string; description: string }[] = [
  {
    key: "log_loss",
    label: "Log loss",
    description: "Penalizes confident wrong probabilities most heavily. Lower is better.",
  },
  {
    key: "brier",
    label: "Brier score",
    description: "Measures squared probability error across home, draw, and away. Lower is better.",
  },
];

export function ModelAnalysis({ analysis, history }: { analysis: ModelAnalysisSnapshot; history?: PredictionHistory }) {
  const reducedMotion = useReducedMotion();
  const [metricKey, setMetricKey] = useState<AnalysisMetricKey>("log_loss");
  const [calibrationBin, setCalibrationBin] = useState(4);
  const metric = METRICS.find((item) => item.key === metricKey) ?? METRICS[0];
  const selectedCalibration = analysis.calibrationBins.find((item) => item.bin === calibrationBin) ?? analysis.calibrationBins[4];

  return (
    <main className="page-shell analysis-page">
      <section className="analysis-hero">
        <div className="analysis-hero-copy">
          <p className="section-label">Model analysis</p>
          <h1>How the model earns trust.</h1>
          <p>
            Frozen out-of-sample evidence for regulation match-result probabilities.
            Every figure below comes from the champion&apos;s immutable evaluation record.
          </p>
        </div>
        <div className="analysis-model-state">
          <span className="status-badge" data-status={analysis.status}><i />{analysis.status}</span>
          <strong>{analysis.modelLabel}</strong>
          <small>{humanizeVersion(analysis.modelVersion)}</small>
        </div>
      </section>

      <section className="analysis-overview" aria-label="Evaluation overview">
        <OverviewItem value={analysis.horizons[0].finalTestFixtures.toLocaleString()} label="T−24 final-test fixtures" />
        <OverviewItem value={analysis.horizons[1].finalTestFixtures.toLocaleString()} label="T−72 final-test fixtures" />
        <OverviewItem value={analysis.calendarMonthBlocks.toString()} label="Calendar-month blocks" />
        <OverviewItem value={analysis.bootstrapReplicates.toLocaleString()} label="Bootstrap samples" />
      </section>

      <section className="analysis-section performance-section">
        <div className="analysis-section-heading">
          <div>
            <p className="section-label">Out-of-sample scoring</p>
            <h2>Champion vs frozen baseline</h2>
            <p>{metric.description}</p>
          </div>
          <div className="analysis-metric-control" aria-label="Scoring metric">
            {METRICS.map((item) => (
              <button
                type="button"
                key={item.key}
                data-active={metricKey === item.key}
                aria-pressed={metricKey === item.key}
                onClick={() => setMetricKey(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            className="horizon-score-list"
            key={metricKey}
            initial={reducedMotion ? false : { opacity: 0, y: 5 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reducedMotion ? undefined : { opacity: 0, y: -4 }}
            transition={{ duration: 0.2 }}
          >
            {analysis.horizons.map((horizon) => (
              <HorizonScore key={horizon.key} horizon={horizon} metric={horizon.metrics[metricKey]} />
            ))}
          </motion.div>
        </AnimatePresence>

        <div className="confidence-key" aria-hidden="true">
          <span><i className="confidence-line" />95% month-block interval</span>
          <span><i className="confidence-zero" />No difference</span>
        </div>
      </section>

      <section className="analysis-section examples-section">
        <div className="analysis-section-heading compact-heading">
          <div>
            <p className="section-label">Published archive</p>
            <h2>What we published, then what happened</h2>
            <p>The latest five settled forecasts preserved before kickoff. Expand a match to inspect exact scores, goal distributions, totals, and handicaps.</p>
          </div>
          <span className="examples-role">Immutable forward evidence</span>
        </div>
        <PublishedArchive history={history} />
      </section>

      <section className="analysis-section calibration-section">
        <div className="analysis-section-heading compact-heading">
          <div>
            <p className="section-label">Calibration</p>
            <h2>Confidence that matches reality</h2>
            <p>Temperature was fitted only on the calibration year, before the final test was opened.</p>
          </div>
        </div>
        <div className="calibration-ledger">
          <div className="analysis-table-head" aria-hidden="true">
            <span>Horizon</span><span>Final-test error</span><span>Temperature</span><span>Calibration change</span>
          </div>
          {analysis.horizons.map((horizon) => (
            <div className="calibration-row" key={horizon.key}>
              <div><strong>{horizon.shortLabel}</strong><small>{horizon.calibrationFixtures.toLocaleString()} calibration fixtures</small></div>
              <div><strong>{formatPercent(horizon.calibrationError)}</strong><small>absolute probability gap</small></div>
              <div><strong>{horizon.temperature.toFixed(3)}</strong><small>frozen scaling value</small></div>
              <div>
                <strong>{horizon.calibrationLogLossBefore.toFixed(4)} <span>→</span> {horizon.calibrationLogLossAfter.toFixed(4)}</strong>
                <small>calibration-fold log loss</small>
              </div>
            </div>
          ))}
        </div>
        <div className="calibration-explorer">
          <div className="calibration-explorer-copy">
            <span>Selected probability range</span>
            <strong>{calibrationBin * 10}–{calibrationBin * 10 + 10}%</strong>
            <p>
              The model averaged <b>{formatPercent(selectedCalibration.meanPredicted)}</b> in this band.
              The outcome happened <b>{formatPercent(selectedCalibration.observedRate)}</b> of the time.
            </p>
            <small>{selectedCalibration.fixtures.toLocaleString()} home, draw, or away outcome probabilities · T−24 final test</small>
          </div>
          <div className="calibration-bin-chart" aria-label="Calibration probability ranges">
            {analysis.calibrationBins.map((bin) => (
              <button
                type="button"
                key={bin.bin}
                data-active={bin.bin === calibrationBin}
                data-low-sample={bin.fixtures < 200}
                aria-pressed={bin.bin === calibrationBin}
                onClick={() => setCalibrationBin(bin.bin)}
              >
                <i style={{ height: `${Math.max(8, bin.observedRate * 100)}%` }} />
                <span>{bin.bin * 10}%</span>
              </button>
            ))}
          </div>
        </div>
      </section>

      <RollingPerformanceSection analysis={analysis} />

      <CompetitionSection analysis={analysis} />

      <BookmakerSection analysis={analysis} history={history} />

      <section className="analysis-section evidence-section">
        <div className="analysis-section-heading compact-heading">
          <div>
            <p className="section-label">Evidence coverage</p>
            <h2>What the production refit learned from</h2>
            <p>Training coverage is shown separately from final-test performance to avoid mixing evidence roles.</p>
          </div>
          <strong className="coverage-total">{analysis.totalTrainingRows.toLocaleString()}<small>point-in-time rows</small></strong>
        </div>
        <div className="coverage-rows">
          {analysis.horizons.map((horizon) => (
            <div className="coverage-row" key={horizon.key}>
              <div><strong>{horizon.shortLabel}</strong><span>{horizon.label}</span></div>
              <div className="coverage-track" aria-hidden="true"><i style={{ width: `${horizon.trainingRows / analysis.horizons[0].trainingRows * 100}%` }} /></div>
              <strong>{horizon.trainingRows.toLocaleString()}</strong>
            </div>
          ))}
        </div>
      </section>

      <section className="analysis-section methodology-section">
        <div className="analysis-section-heading compact-heading">
          <div>
            <p className="section-label">Evaluation protocol</p>
            <h2>Chronology first, final test once</h2>
          </div>
        </div>
        <ol className="methodology-flow">
          <li><span>01</span><strong>Develop</strong><p>Select the rich xG and shots correction using development data only.</p></li>
          <li><span>02</span><strong>Calibrate</strong><p>Fit probability temperature on a later, isolated calibration year.</p></li>
          <li><span>03</span><strong>Test once</strong><p>Freeze the recipe, score the last period once, and retain uncertainty by month.</p></li>
        </ol>
        <div className="analysis-footnote">
          <p>This is an experimental forecasting system, not a claim of guaranteed betting edge. Monthly and competition views are descriptive slices of the frozen final test and must not be used to retune the current model.</p>
          <dl>
            <div><dt>Evaluation</dt><dd>{analysis.evaluationVersion}</dd></div>
            <div><dt>Report hash</dt><dd>{shortHash(analysis.evaluationReportSha256)}</dd></div>
            <div><dt>Model hash</dt><dd>{shortHash(analysis.logicalModelSha256)}</dd></div>
          </dl>
        </div>
      </section>
    </main>
  );
}

function OverviewItem({ value, label }: { value: string; label: string }) {
  return <div><strong>{value}</strong><span>{label}</span></div>;
}

function HorizonScore({ horizon, metric }: { horizon: AnalysisHorizon; metric: AnalysisMetric }) {
  const improvement = Math.abs(metric.delta);
  const relativeImprovement = improvement / metric.baseline;
  const domain = 0.006;
  const zero = 92;
  const x = (value: number) => zero + value / domain * 82;
  const lower = x(metric.confidence95[0]);
  const upper = x(metric.confidence95[1]);
  const point = x(metric.delta);

  return (
    <article className="horizon-score">
      <div className="horizon-score-title">
        <span>{horizon.shortLabel}</span>
        <div><strong>{horizon.label}</strong><small>{horizon.finalTestFixtures.toLocaleString()} final-test fixtures</small></div>
      </div>
      <div className="score-pair">
        <div><span>Champion</span><strong>{metric.champion.toFixed(4)}</strong></div>
        <div><span>Baseline</span><strong>{metric.baseline.toFixed(4)}</strong></div>
      </div>
      <div className="improvement-copy">
        <strong>−{improvement.toFixed(5)}</strong>
        <span>{formatPercent(relativeImprovement)} lower</span>
      </div>
      <div className="confidence-plot" role="img" aria-label={`95% confidence interval ${metric.confidence95[0].toFixed(5)} to ${metric.confidence95[1].toFixed(5)}; zero means no difference`}>
        <i className="plot-axis" />
        <i className="plot-zero" style={{ left: `${zero}px` }} />
        <i className="plot-interval" style={{ left: `${lower}px`, width: `${upper - lower}px` }} />
        <i className="plot-point" style={{ left: `${point}px` }} />
      </div>
    </article>
  );
}

function PublishedArchive({ history }: { history?: PredictionHistory }) {
  const [showAll, setShowAll] = useState(false);
  const [expandedFixture, setExpandedFixture] = useState<string | null>(null);
  if (!history) {
    return (
      <div className="history-empty" role="status">
        <strong>The published archive is waiting for its first verified settlement artifact.</strong>
        <p>No backtest rows are substituted here. The collector will populate this section from immutable pre-kickoff evidence.</p>
      </div>
    );
  }
  const visible = showAll ? history.fixtures : history.fixtures.slice(0, 5);
  return (
    <>
      <div className="published-history-list">
        {visible.map((fixture) => (
          <PublishedFixture
            fixture={fixture}
            key={fixture.fixture_id}
            expanded={expandedFixture === fixture.fixture_id}
            onToggle={() => setExpandedFixture(expandedFixture === fixture.fixture_id ? null : fixture.fixture_id)}
          />
        ))}
      </div>
      <div className="history-list-footer">
        <p>{history.fixture_count.toLocaleString()} safely settled {history.fixture_count === 1 ? "match" : "matches"} · updated {formatExampleDate(history.as_of)}</p>
        {history.fixtures.length > 5 && (
          <button type="button" onClick={() => setShowAll(!showAll)}>{showAll ? "Show latest five" : `See all ${history.fixtures.length}`}</button>
        )}
      </div>
    </>
  );
}

function PublishedFixture({ fixture, expanded, onToggle }: { fixture: HistoryFixture; expanded: boolean; onToggle: () => void }) {
  const [predictionKey, setPredictionKey] = useState(fixture.prediction_groups[0].prediction_key);
  const [marketGroup, setMarketGroup] = useState("Match result");
  const prediction = fixture.prediction_groups.find((item) => item.prediction_key === predictionKey) ?? fixture.prediction_groups[0];
  const resultMarkets = prediction.markets.filter((market) => market.group === "Match result");
  const top = resultMarkets.reduce((best, item) => (item.probability ?? 0) > (best.probability ?? 0) ? item : best, resultMarkets[0]);
  const groups = Array.from(new Set(prediction.markets.map((market) => market.group)));
  const activeGroup = groups.includes(marketGroup) ? marketGroup : groups[0];
  const markets = prediction.markets.filter((market) => market.group === activeGroup);
  const topHit = top?.realized_settlement === "win";

  return (
    <article className="published-fixture" data-expanded={expanded}>
      <button className="published-fixture-summary" type="button" aria-expanded={expanded} onClick={onToggle}>
        <span className="published-date"><time>{formatExampleDate(fixture.kickoff)}</time><small>{fixture.competition_name}</small></span>
        <span className="published-teams">
          <strong>{fixture.home_team_name}</strong><b>{fixture.result.home_goals}</b>
          <strong>{fixture.away_team_name}</strong><b>{fixture.result.away_goals}</b>
        </span>
        <span className="published-top-outcome"><small>Highest probability</small><strong>{top?.label ?? "—"} {top?.probability == null ? "" : formatPercent(top.probability)}</strong><em data-hit={topHit}>{topHit ? "Landed" : "Did not land"}</em></span>
        <i aria-hidden="true">{expanded ? "−" : "+"}</i>
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div className="published-fixture-detail" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: .24 }}>
            <div className="history-detail-controls">
              <div className="history-horizon-tabs" aria-label="Published horizon">
                {fixture.prediction_groups.map((group) => (
                  <button type="button" key={group.prediction_key} data-active={group.prediction_key === prediction.prediction_key} onClick={() => setPredictionKey(group.prediction_key)}>{formatHorizon(group.information_state)}</button>
                ))}
              </div>
              <div className="history-issued"><span>{prediction.evidence_label}</span><small>Published {formatTimestamp(prediction.first_published_at)}</small></div>
            </div>
            <div className="history-expectation">
              <span>Expected goals</span><strong>{fixture.home_team_name} {prediction.expected_home_goals.toFixed(2)}</strong><strong>{fixture.away_team_name} {prediction.expected_away_goals.toFixed(2)}</strong>
            </div>
            <div className="history-market-tabs" aria-label="Forecast market group">
              {groups.map((group) => <button type="button" key={group} data-active={group === activeGroup} onClick={() => setMarketGroup(group)}>{group}</button>)}
            </div>
            <div className="history-market-list">
              <div className="history-market-head"><span>Selection</span><span>Forecast</span><span>Fair multiplier</span><span>Result</span></div>
              {markets.map((market) => <HistoryMarketRow market={market} key={market.market_id} />)}
            </div>
            <p className="history-detail-note">Fair multipliers are probability inverses, not bookmaker payouts. Asian-line rows preserve half-win, push, and half-loss probabilities.</p>
          </motion.div>
        )}
      </AnimatePresence>
    </article>
  );
}

function HistoryMarketRow({ market }: { market: HistoryMarket }) {
  return (
    <div className="history-market-row" data-settlement={market.realized_settlement}>
      <strong>{market.label}</strong>
      <span>{market.probability == null ? settlementForecast(market) : formatPercent(market.probability)}</span>
      <span>{market.fair_decimal_multiplier == null ? "—" : `${market.fair_decimal_multiplier.toFixed(2)}×`}</span>
      <em>{humanizeSettlement(market.realized_settlement)}</em>
    </div>
  );
}

function RollingPerformanceSection({ analysis }: { analysis: ModelAnalysisSnapshot }) {
  const [metric, setMetric] = useState<AnalysisMetricKey>("log_loss");
  const reducedMotion = useReducedMotion();
  const rows = analysis.rollingPerformance;
  const values = rows.map((row) => metric === "log_loss" ? row.logLoss : row.brier);
  const intervals = rows.map((row) => metric === "log_loss" ? row.logLossConfidence95 : row.brierConfidence95);
  const minimum = Math.min(...intervals.map((row) => row[0]));
  const maximum = Math.max(...intervals.map((row) => row[1]));
  const x = (index: number) => 42 + index * (776 / (rows.length - 1));
  const y = (value: number) => 215 - (value - minimum) / (maximum - minimum) * 170;
  const points = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
  return (
    <section className="analysis-section rolling-section">
      <div className="analysis-section-heading">
        <div><p className="section-label">Stability through time</p><h2>Trailing three-month performance</h2><p>Each point pools the three months ending at the label. Whiskers are 95% mean intervals; partial periods appear only when the pooled sample reaches 500 fixtures.</p></div>
        <MetricControl value={metric} onChange={setMetric} />
      </div>
      <div className="rolling-chart-wrap">
        <svg className="rolling-chart" viewBox="0 0 860 260" role="img" aria-label={`Trailing three-month ${metric === "log_loss" ? "log loss" : "Brier score"} with 95 percent intervals`}>
          <line x1="42" y1="215" x2="818" y2="215" className="rolling-axis" />
          {rows.map((row, index) => (
            <g key={row.month}>
              <line x1={x(index)} x2={x(index)} y1={y(intervals[index][0])} y2={y(intervals[index][1])} className="rolling-whisker" />
              <line x1={x(index) - 4} x2={x(index) + 4} y1={y(intervals[index][0])} y2={y(intervals[index][0])} className="rolling-whisker" />
              <line x1={x(index) - 4} x2={x(index) + 4} y1={y(intervals[index][1])} y2={y(intervals[index][1])} className="rolling-whisker" />
              <text x={x(index)} y="240" textAnchor="middle">{formatMonth(row.month)}</text>
            </g>
          ))}
          <motion.polyline points={points} className="rolling-line" fill="none" initial={reducedMotion ? false : { pathLength: 0, opacity: 0 }} whileInView={{ pathLength: 1, opacity: 1 }} viewport={{ once: true }} transition={{ duration: .7 }} />
          {rows.map((row, index) => (
            <circle key={row.month} cx={x(index)} cy={y(values[index])} r="4" className="rolling-point">
              <title>{`${formatMonth(row.month)}: ${values[index].toFixed(4)} across ${row.fixtures} fixtures`}</title>
            </circle>
          ))}
        </svg>
      </div>
      <div className="rolling-summary"><span>Lower is better</span><strong>Latest window {values.at(-1)?.toFixed(4)}</strong><small>{rows.at(-1)?.fixtures.toLocaleString()} fixtures</small></div>
    </section>
  );
}

function CompetitionSection({ analysis }: { analysis: ModelAnalysisSnapshot }) {
  const [metric, setMetric] = useState<AnalysisMetricKey>("log_loss");
  const values = analysis.competitionPerformance.map((row) => metric === "log_loss" ? row.logLoss : row.brier);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  return (
    <section className="analysis-section competition-section">
      <div className="analysis-section-heading">
        <div><p className="section-label">Competition breakdown</p><h2>Only meaningful sample sizes</h2><p>Competitions remain hidden until they have at least {analysis.competitionMinimumFixtures} T−24 final-test fixtures. These are descriptive results, not separate model rankings.</p></div>
        <MetricControl value={metric} onChange={setMetric} />
      </div>
      <div className="competition-ledger">
        {analysis.competitionPerformance.map((row) => {
          const value = metric === "log_loss" ? row.logLoss : row.brier;
          const width = 22 + (value - minimum) / Math.max(.0001, maximum - minimum) * 78;
          return <div className="competition-row" key={`${row.country}:${row.competition}`}><div><strong>{row.competition}</strong><small>{row.country}</small></div><span>{row.fixtures.toLocaleString()} fixtures</span><i><b style={{ width: `${width}%` }} /></i><strong>{value.toFixed(4)}</strong></div>;
        })}
      </div>
    </section>
  );
}

function BookmakerSection({ analysis, history }: { analysis: ModelAnalysisSnapshot; history?: PredictionHistory }) {
  const benchmark = analysis.retrospectiveBookmaker;
  const readiness = history?.bookmaker_readiness;
  const forward = readiness?.comparison;
  return (
    <section className="analysis-section bookmaker-section">
      <div className="analysis-section-heading compact-heading"><div><p className="section-label">Market benchmark</p><h2>Model versus bookmaker consensus</h2><p>The retrospective benchmark is visible now. Forward API-Football comparisons stay locked until timestamp-safe settled coverage passes a frozen gate.</p></div></div>
      <div className="bookmaker-comparison">
        <div className="bookmaker-retrospective"><span>Retrospective · T−24</span><h3>{benchmark.label}</h3><div><p><small>Model log loss</small><strong>{benchmark.modelLogLoss.toFixed(4)}</strong></p><p><small>Market log loss</small><strong>{benchmark.marketLogLoss.toFixed(4)}</strong></p><p><small>Market advantage</small><strong>{Math.abs(benchmark.marketMinusModel).toFixed(4)}</strong></p></div><p>{benchmark.fixtures.toLocaleString()} paired fixtures. {benchmark.timingLimitation}</p></div>
        <div className="bookmaker-forward" data-ready={readiness?.status === "ready"}>
          <span>Forward benchmark</span>
          <h3>{forward ? "Timestamp-safe comparison ready" : "Collecting timestamp-safe prices"}</h3>
          {forward ? (
            <div className="bookmaker-forward-stats">
              <p><small>Model log loss</small><strong>{forward.model_log_loss.toFixed(4)}</strong></p>
              <p><small>Market log loss</small><strong>{forward.market_log_loss.toFixed(4)}</strong></p>
              <p><small>Market − model</small><strong>{forward.market_minus_model.toFixed(4)}</strong></p>
            </div>
          ) : (
            <><strong>{readiness?.settled_fixture_horizons ?? 0}</strong><small>of {readiness?.minimum_settled_fixture_horizons ?? 500} settled fixture-horizons · {readiness?.calendar_months ?? 0} of {readiness?.minimum_calendar_months ?? 3} months</small></>
          )}
          <p>{forward ? `${forward.paired_fixture_horizons.toLocaleString()} paired forward observations passed the predeclared gate.` : "Performance remains hidden until the predeclared sample and calendar-month minimums are both met."}</p>
        </div>
      </div>
    </section>
  );
}

function MetricControl({ value, onChange }: { value: AnalysisMetricKey; onChange: (value: AnalysisMetricKey) => void }) {
  return <div className="analysis-metric-control" aria-label="Scoring metric">{METRICS.map((item) => <button type="button" key={item.key} data-active={value === item.key} aria-pressed={value === item.key} onClick={() => onChange(item.key)}>{item.label}</button>)}</div>;
}

function settlementForecast(market: HistoryMarket) {
  const values = market.settlement_probabilities;
  if (!values) return "—";
  return `${formatPercent(values.win)} win · ${formatPercent(values.push)} push`;
}

function humanizeSettlement(value: string) {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function formatHorizon(value: string) {
  if (value === "pre_lineup_24h_v1") return "T−24";
  if (value === "pre_lineup_72h_clean_v1") return "T−72";
  return "Lineup";
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function formatMonth(value: string) {
  return new Intl.DateTimeFormat("en-GB", { month: "short" }).format(new Date(`${value}-01T00:00:00Z`));
}

function formatExampleDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(new Date(value));
}

function formatPercent(value: number) {
  return new Intl.NumberFormat("en", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function shortHash(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-8)}`;
}

function humanizeVersion(value: string) {
  return value.replaceAll("_", " ");
}
