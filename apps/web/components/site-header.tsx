import Link from "next/link";
import { formatAsOf } from "@/lib/format";
import { snapshotLabel } from "@/lib/platform";
import type { PlatformSnapshot } from "@/lib/types";

export function SiteHeader({
  active,
  snapshot,
}: {
  active: "matches" | "analysis";
  snapshot?: PlatformSnapshot;
}) {
  return (
    <>
      <header className="site-header">
        <div className="site-header-inner">
          <Link className="site-brand" href="/">Soccer Bot</Link>
          <PrimaryNav active={active} className="site-nav desktop-nav" />
          {snapshot ? (
            <div className="header-status" data-stale={snapshot.is_stale}>
              <i aria-hidden="true" />
              <span>{snapshotLabel(snapshot)}</span>
              <time>{formatAsOf(snapshot.as_of)}</time>
            </div>
          ) : <span className="header-spacer" />}
        </div>
      </header>
      <PrimaryNav active={active} className="site-nav mobile-nav" />
    </>
  );
}

function PrimaryNav({ active, className }: { active: "matches" | "analysis"; className: string }) {
  return (
    <nav className={className} aria-label={className.includes("mobile") ? "Mobile navigation" : "Primary navigation"}>
      <Link data-active={active === "matches"} href="/">Matches</Link>
      <Link data-active={active === "analysis"} href="/analysis">Model analysis</Link>
    </nav>
  );
}
