import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const DEFAULT_API_URL = "http://127.0.0.1:8000";

export async function GET() {
  const apiUrl = (process.env.SOCCER_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
  try {
    const response = await fetch(`${apiUrl}/v2/platform-snapshot`, {
      cache: "no-store",
      headers: { accept: "application/json" },
    });
    if (!response.ok) return unavailable();
    const snapshot: unknown = await response.json();
    if (
      typeof snapshot !== "object" ||
      snapshot === null ||
      !("snapshot_version" in snapshot) ||
      snapshot.snapshot_version !== "specialized_bet_platform_snapshot_v1" ||
      !("states" in snapshot) ||
      !Array.isArray(snapshot.states)
    ) {
      return unavailable();
    }
    return NextResponse.json(snapshot, {
      headers: { "cache-control": "no-store" },
    });
  } catch {
    return unavailable();
  }
}

function unavailable() {
  return NextResponse.json(
    { status: "unavailable" },
    { status: 503, headers: { "cache-control": "no-store" } },
  );
}
