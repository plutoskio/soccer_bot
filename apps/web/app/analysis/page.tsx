import { SiteHeader } from "@/components/site-header";

export default function AnalysisPage() {
  return (
    <>
      <SiteHeader active="analysis" />
      <main className="page-shell analysis-page">
        <section className="analysis-intro">
          <p className="section-label">Model analysis</p>
          <h1>Performance reporting is coming next.</h1>
          <p>
            This page will explain how each model performs over time, across competitions, and at each prediction horizon.
          </p>
        </section>
        <section className="planned-analysis" aria-label="Planned model analysis">
          <div><span>01</span><strong>Calibration</strong><p>Whether predicted probabilities match observed frequencies.</p></div>
          <div><span>02</span><strong>Scoring</strong><p>Log loss, Brier score, and comparisons with frozen baselines.</p></div>
          <div><span>03</span><strong>Coverage</strong><p>Evidence depth across competitions, teams, and time horizons.</p></div>
          <div><span>04</span><strong>Stability</strong><p>Performance through time and under changing match conditions.</p></div>
        </section>
      </main>
    </>
  );
}
