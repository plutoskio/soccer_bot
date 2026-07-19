import type { PlatformSnapshot } from "./types";

const DEFAULT_API_URL = "http://127.0.0.1:8000";

export async function getPredictionSnapshot(): Promise<PlatformSnapshot> {
  const apiUrl = (process.env.SOCCER_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
  const response = await fetch(`${apiUrl}/v2/platform-snapshot`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Prediction API returned ${response.status}`);
  return response.json() as Promise<PlatformSnapshot>;
}
