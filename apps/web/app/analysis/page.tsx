import { ModelAnalysis } from "@/components/model-analysis";
import { SiteHeader } from "@/components/site-header";
import { getPredictionHistory } from "@/lib/history";
import { modelAnalysis } from "@/lib/model-analysis";
import { getPredictionSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";

export default async function AnalysisPage() {
  const [snapshot, history] = await Promise.all([
    getPredictionSnapshot().catch(() => undefined),
    getPredictionHistory().catch(() => undefined),
  ]);
  return (
    <>
      <SiteHeader active="analysis" snapshot={snapshot} />
      <ModelAnalysis analysis={modelAnalysis} history={history} />
    </>
  );
}
