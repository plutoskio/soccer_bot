import { FixtureIndex } from "@/components/fixture-index";
import { SiteHeader } from "@/components/site-header";
import { getPredictionSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";

export default async function Home() {
  try {
    const snapshot = await getPredictionSnapshot();
    return (
      <>
        <SiteHeader active="matches" snapshot={snapshot} />
        <FixtureIndex snapshot={snapshot} />
      </>
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown API error";
    return <UnavailableState message={message} />;
  }
}

function UnavailableState({ message }: { message: string }) {
  return (
    <>
      <SiteHeader active="matches" />
      <main className="system-state">
        <p className="section-label">Forecasts unavailable</p>
        <h1>The latest predictions could not be loaded.</h1>
        <p>The product stops before displaying unvalidated or invented probabilities.</p>
        <code>{message}</code>
      </main>
    </>
  );
}
