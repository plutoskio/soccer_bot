export type InformationState = "pre_lineup_72h_clean_v1" | "pre_lineup_24h_v1";

export interface FixtureMetadata {
  fixture_id: string;
  home_team_name: string;
  away_team_name: string;
  competition_name: string;
}

export interface Prediction {
  fixture_id: string;
  fixture: FixtureMetadata;
  kickoff: string;
  prediction_at: string;
  information_state: InformationState;
  model_version: string;
  home_win_probability: number;
  draw_probability: number;
  away_win_probability: number;
  raw_home_win_probability: number;
  raw_draw_probability: number;
  raw_away_win_probability: number;
  expected_home_goals: number;
  expected_away_goals: number;
  home_history_matches: number;
  away_history_matches: number;
  home_xg_history: number;
  away_xg_history: number;
  home_shots_history: number;
  away_shots_history: number;
  source_max_retrieved_at?: string | null;
  issued_at?: string;
  issuance_status?: "strict_forward_frozen" | "legacy_reconstructed_frozen";
  issuance_policy_version?: string;
  availability_policy_version?: string;
  immutable_prediction_sha256?: string;
  warnings: string[];
}

export interface PredictionSnapshot {
  snapshot_version: string;
  model_version: string;
  logical_model_sha256: string;
  model_reproducibility_sha256?: string | null;
  prediction_rows_sha256: string;
  created_at: string;
  as_of: string;
  supported_output: "regulation_moneyline";
  distribution_limitation: string | null;
  availability_policy?: {
    policy_version: string;
    [key: string]: unknown;
  } | null;
  issuance_policy?: {
    policy_version: string;
    [key: string]: unknown;
  } | null;
  training_evidence: {
    horizon_training_fixtures: Record<InformationState, number>;
    minimum_training_fixtures: number;
    team_cold_start_below_matches: number;
    full_signal_history_matches: number;
  };
  fixture_count: number;
  prediction_count: number;
  snapshot_age_seconds: number;
  is_stale: boolean;
  available_information_states: InformationState[];
  predictions: Prediction[];
}
