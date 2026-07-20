import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const DEFAULT_API_URL = "http://127.0.0.1:8000";

type SnapshotHeartbeatSource = {
  as_of?: unknown;
  model_version?: unknown;
  logical_model_sha256?: unknown;
  prediction_count?: unknown;
  fixture_count?: unknown;
  predictions?: unknown;
};

export async function GET() {
  const apiUrl = (process.env.SOCCER_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
  try {
    const response = await fetch(`${apiUrl}/v1/snapshot`, {
      cache: "no-store",
      headers: { accept: "application/json" },
    });
    if (!response.ok) {
      return unavailable();
    }
    const snapshot = (await response.json()) as SnapshotHeartbeatSource;
    const predictions = Array.isArray(snapshot.predictions) ? snapshot.predictions : null;
    const predictionCount = integerOrFallback(snapshot.prediction_count, predictions?.length);
    const fixtureCount = integerOrFallback(
      snapshot.fixture_count,
      predictions
        ? new Set(
            predictions
              .map((row) =>
                typeof row === "object" && row !== null && "fixture_id" in row
                  ? String(row.fixture_id)
                  : "",
              )
              .filter(Boolean),
          ).size
        : undefined,
    );
    if (
      typeof snapshot.as_of !== "string" ||
      typeof snapshot.model_version !== "string" ||
      typeof snapshot.logical_model_sha256 !== "string" ||
      !Number.isInteger(predictionCount) ||
      predictionCount <= 0 ||
      !Number.isInteger(fixtureCount) ||
      fixtureCount <= 0
    ) {
      return unavailable();
    }
    return NextResponse.json(
      {
        heartbeat_version: "public_prediction_heartbeat_v1",
        as_of: snapshot.as_of,
        model_version: snapshot.model_version,
        logical_model_sha256: snapshot.logical_model_sha256,
        prediction_count: predictionCount,
        fixture_count: fixtureCount,
      },
      { headers: { "cache-control": "no-store" } },
    );
  } catch {
    return unavailable();
  }
}

function integerOrFallback(value: unknown, fallback: number | undefined): number {
  return typeof value === "number" && Number.isInteger(value) ? value : (fallback ?? -1);
}

function unavailable() {
  return NextResponse.json(
    {
      heartbeat_version: "public_prediction_heartbeat_v1",
      status: "unavailable",
    },
    { status: 503, headers: { "cache-control": "no-store" } },
  );
}
