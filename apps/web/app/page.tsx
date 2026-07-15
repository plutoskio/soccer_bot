import { ProbabilityDesk } from "@/components/probability-desk";
import { getPredictionSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";

export default async function Home() {
  try {
    return <ProbabilityDesk snapshot={await getPredictionSnapshot()} />;
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown API error";
    return (
      <main className="system-state">
        <div className="wordmark"><span aria-hidden="true">SB</span> Soccer Bot</div>
        <p className="eyebrow">Probability desk unavailable</p>
        <h1>The forecast snapshot could not be loaded.</h1>
        <p className="state-copy">The interface fails closed when its audited prediction source is unavailable. No probabilities have been guessed or cached in the browser.</p>
        <code>{message}</code>
      </main>
    );
  }
}
