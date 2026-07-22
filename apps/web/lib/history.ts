export type HistorySettlement = "win" | "half_win" | "push" | "half_loss" | "loss";

export interface HistoryMarket {
  market_id: string;
  group: string;
  label: string;
  probability: number | null;
  fair_decimal_multiplier: number | null;
  settlement_probabilities: Record<HistorySettlement, number> | null;
  realized_settlement: HistorySettlement;
  market_comparison: null | {
    market_probability: number;
    bookmaker_count: number;
  };
}

export interface HistoryPredictionGroup {
  prediction_key: string;
  evidence_label: string;
  family_key: string;
  display_name: string;
  model_version: string;
  model_status_at_prediction: "experimental" | "validated";
  information_state: string;
  prediction_at: string;
  first_published_at: string;
  eligible_for_performance_claim: boolean;
  expected_home_goals: number;
  expected_away_goals: number;
  warnings: string[];
  markets: HistoryMarket[];
}

export interface HistoryFixture {
  fixture_id: string;
  kickoff: string;
  competition_id: string;
  competition_name: string;
  home_team_name: string;
  away_team_name: string;
  result: {
    status: "settled";
    home_goals: number;
    away_goals: number;
    outcome: "home_win" | "draw" | "away_win";
    settled_at: string;
  };
  prediction_groups: HistoryPredictionGroup[];
}

export interface PredictionHistory {
  history_version: "published_prediction_history_v1";
  generated_at: string;
  as_of: string;
  fixture_count: number;
  prediction_group_count: number;
  history_rows_sha256: string;
  returned_fixture_count: number;
  has_more: boolean;
  bookmaker_readiness: {
    status: "collecting" | "ready";
    settled_timestamp_safe_quotes: number;
    settled_fixture_horizons: number;
    calendar_months: number;
    minimum_settled_fixture_horizons: number;
    minimum_calendar_months: number;
    performance_statistics_exposed: boolean;
    gate_policy: string;
    comparison: null | {
      paired_fixture_horizons: number;
      model_log_loss: number;
      market_log_loss: number;
      market_minus_model: number;
    };
  };
  fixtures: HistoryFixture[];
}

const DEFAULT_API_URL = "http://127.0.0.1:8000";

export async function getPredictionHistory(limit = 50): Promise<PredictionHistory> {
  const apiUrl = (process.env.SOCCER_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
  const response = await fetch(`${apiUrl}/v2/history?limit=${limit}`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Prediction history API returned ${response.status}`);
  return response.json() as Promise<PredictionHistory>;
}
