import { notFound } from "next/navigation";
import { MatchDetail } from "@/components/match-detail";
import { SiteHeader } from "@/components/site-header";
import { groupFixtures } from "@/lib/platform";
import { getPredictionSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";

export default async function MatchPage({
  params,
}: {
  params: Promise<{ fixtureId: string }>;
}) {
  const [{ fixtureId }, snapshot] = await Promise.all([params, getPredictionSnapshot()]);
  const fixture = groupFixtures(snapshot.states).find((item) => item.id === fixtureId);
  if (!fixture) notFound();

  return (
    <>
      <SiteHeader active="matches" snapshot={snapshot} />
      <MatchDetail fixture={fixture} snapshot={snapshot} />
    </>
  );
}
