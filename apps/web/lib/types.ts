export type InformationState = "pre_lineup_72h_clean_v1" | "pre_lineup_24h_v1";
export type FamilyStatus = "validated" | "experimental" | "unavailable" | "unsupported";

export interface FixtureMetadata {
  fixture_id: string;
  home_team_name: string;
  away_team_name: string;
  competition_name: string;
  neutral_venue?: boolean;
}

export interface SettlementProbabilities {
  win: number;
  half_win: number;
  push: number;
  half_loss: number;
  loss: number;
}

export interface MarketQuote {
  market_id: string;
  contract_key: string;
  group: string;
  label: string;
  selection: Record<string, string | number>;
  line: number | null;
  probability: number | null;
  fair_decimal_multiplier: number | null;
  settlement_probabilities: SettlementProbabilities | null;
  market_comparison: ExternalMarketQuote | null;
  live_market: ExternalMarketQuote | null;
}

export interface ExternalMarketQuote {
  source: "api_football";
  quote_type: "cutoff_consensus";
  market_probability: number;
  market_decimal_multiplier: number;
  bookmaker_count: number;
  consensus_method: "median_proportional_devig";
  observed_at: string;
  retrieved_at: string;
}

export interface ModelFamily {
  family_key: string;
  display_name: string;
  status: FamilyStatus;
  model_version: string;
  logical_model_sha256: string | null;
  eligible_for_ranking: boolean;
  unavailable_reason: string | null;
  evidence: Record<string, unknown> & { warnings?: string[] };
  markets: MarketQuote[];
}

export interface PlatformState {
  fixture_id: string;
  fixture: FixtureMetadata;
  kickoff: string;
  prediction_at: string;
  issued_at: string;
  information_state: InformationState;
  match_context?: MatchContext;
  model_expectation?: ModelExpectation;
  families: ModelFamily[];
}

export interface ModelExpectation {
  expected_home_goals: number;
  expected_away_goals: number;
}

export interface MatchContext {
  cutoff_at: string;
  home: TeamMatchContext;
  away: TeamMatchContext;
}

export interface TeamMatchContext {
  team_id: string;
  rest_days: number | null;
  matches_last_7d: number;
  matches_last_14d: number;
  matches_last_30d: number;
  recent_matches: RecentMatch[];
  trends: {
    last_5: TeamTrend;
    last_10: TeamTrend;
  };
}

export interface RecentMatch {
  fixture_id: string;
  kickoff: string;
  available_at: string;
  competition_name: string;
  opponent_name: string;
  venue: "home" | "away";
  neutral_venue: boolean;
  team_score: number;
  opponent_score: number;
  outcome: "win" | "draw" | "loss";
}

export interface TeamTrend {
  sample_size: number;
  wins: number;
  draws: number;
  losses: number;
  goals_for_per_match: number | null;
  goals_against_per_match: number | null;
  clean_sheet_rate: number | null;
  both_teams_scored_rate: number | null;
}

export interface PlatformSnapshot {
  snapshot_version: "specialized_bet_platform_snapshot_v1";
  created_at: string;
  as_of: string;
  snapshot_age_seconds: number;
  is_stale: boolean;
  family_registry_version: string;
  market_comparison_status: string | null;
  market_data: {
    source?: "api_football";
    consensus_method?: "median_proportional_devig";
    cutoff_market_fixture_count?: number;
    cutoff_market_quote_count?: number;
    minimum_bookmakers?: number;
    minimum_observed_bookmakers?: number | null;
    maximum_observed_bookmakers?: number | null;
    cutoff_policy?: string;
    model_usage_policy?: string;
  };
  ranking_policy: "validated_families_only";
  models: Record<string, { model_version: string; logical_sha256?: string; status: FamilyStatus }>;
  target_audit: Record<string, number>;
  fixture_count: number;
  state_count: number;
  available_information_states: InformationState[];
  state_rows_sha256: string;
  states: PlatformState[];
}
