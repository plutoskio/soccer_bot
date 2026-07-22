export type AnalysisMetricKey = "log_loss" | "brier";
export type AnalysisHorizonKey = "t24" | "t72";

export interface AnalysisMetric {
  champion: number;
  baseline: number;
  delta: number;
  confidence95: [number, number];
}

export interface AnalysisHorizon {
  key: AnalysisHorizonKey;
  shortLabel: string;
  label: string;
  description: string;
  finalTestFixtures: number;
  trainingRows: number;
  calibrationFixtures: number;
  calibrationError: number;
  temperature: number;
  calibrationLogLossBefore: number;
  calibrationLogLossAfter: number;
  metrics: Record<AnalysisMetricKey, AnalysisMetric>;
}

export interface CalibrationBin {
  bin: number;
  fixtures: number;
  meanPredicted: number;
  observedRate: number;
}

export interface RollingPerformance {
  month: string;
  fixtures: number;
  logLoss: number;
  logLossConfidence95: [number, number];
  brier: number;
  brierConfidence95: [number, number];
}

export interface CompetitionPerformance {
  competition: string;
  country: string;
  fixtures: number;
  logLoss: number;
  brier: number;
}

export interface ModelAnalysisSnapshot {
  modelVersion: string;
  modelLabel: string;
  status: "validated";
  createdAt: string;
  evaluationVersion: string;
  evaluationReportSha256: string;
  logicalModelSha256: string;
  totalTrainingRows: number;
  calendarMonthBlocks: number;
  bootstrapReplicates: number;
  horizons: AnalysisHorizon[];
  calibrationBins: CalibrationBin[];
  rollingPerformance: RollingPerformance[];
  competitionMinimumFixtures: number;
  competitionPerformance: CompetitionPerformance[];
  retrospectiveBookmaker: {
    label: string;
    fixtures: number;
    modelLogLoss: number;
    marketLogLoss: number;
    marketMinusModel: number;
    confidence95: [number, number];
    timingLimitation: string;
  };
}

// Frozen, presentation-ready projection of the immutable champion manifest and
// promoted evaluation report. Keep the hashes visible so displayed figures can
// always be traced back to their source artifacts.
export const modelAnalysis: ModelAnalysisSnapshot = {
  modelVersion: "regulation_champion_v1",
  modelLabel: "Rich-rate corrected Poisson",
  status: "validated",
  createdAt: "2026-07-14T22:32:39.361091+00:00",
  evaluationVersion: "regulation_rich_rate_promoted_evaluation_v1",
  evaluationReportSha256: "06e04082d1e2c690bf30e86e74d184d5b1cf5cdb88ebbfb4cca9227fc0f1798f",
  logicalModelSha256: "8be7ffad15d12e7e603b2d9f3dd8dcd5e742e0f80846bcb6cd45c9ca40d7ef7a",
  totalTrainingRows: 73_258,
  calendarMonthBlocks: 13,
  bootstrapReplicates: 2_000,
  horizons: [
    {
      key: "t24",
      shortLabel: "T−24",
      label: "24 hours before kickoff",
      description: "The primary comparable pre-lineup anchor.",
      finalTestFixtures: 5_159,
      trainingRows: 38_445,
      calibrationFixtures: 5_227,
      calibrationError: 0.007842382999574442,
      temperature: 1.1806793063486158,
      calibrationLogLossBefore: 1.002662404464849,
      calibrationLogLossAfter: 0.9999234560107949,
      metrics: {
        log_loss: {
          champion: 1.0143714973668785,
          baseline: 1.0189007515969993,
          delta: -0.004529254230120794,
          confidence95: [-0.005577739865582407, -0.00352999029977899],
        },
        brier: {
          champion: 0.6070100353406094,
          baseline: 0.6101580242088051,
          delta: -0.0031479888681956963,
          confidence95: [-0.00381674867038811, -0.00251955885677055],
        },
      },
    },
    {
      key: "t72",
      shortLabel: "T−72",
      label: "Clean 72-hour horizon",
      description: "Available only when kickoff was known by the exact cutoff.",
      finalTestFixtures: 4_743,
      trainingRows: 34_813,
      calibrationFixtures: 4_795,
      calibrationError: 0.008531420853543888,
      temperature: 1.171755567418676,
      calibrationLogLossBefore: 1.0033968070055814,
      calibrationLogLossAfter: 1.00093685012413,
      metrics: {
        log_loss: {
          champion: 1.0167267399908175,
          baseline: 1.0210660024578632,
          delta: -0.004339262467045687,
          confidence95: [-0.005321465291872119, -0.0034037232808047033],
        },
        brier: {
          champion: 0.6088213890826455,
          baseline: 0.6118198581332002,
          delta: -0.0029984690505547073,
          confidence95: [-0.0036960563132534246, -0.0023478859880537535],
        },
      },
    },
  ],
  calibrationBins: [
    { bin: 0, fixtures: 261, meanPredicted: 0.0718996103, observedRate: 0.0727969349 },
    { bin: 1, fixtures: 1_704, meanPredicted: 0.1630178402, observedRate: 0.1660798122 },
    { bin: 2, fixtures: 6_208, meanPredicted: 0.2517550332, observedRate: 0.2570876289 },
    { bin: 3, fixtures: 3_086, meanPredicted: 0.3441380772, observedRate: 0.3305249514 },
    { bin: 4, fixtures: 2_003, meanPredicted: 0.4465903097, observedRate: 0.4553170245 },
    { bin: 5, fixtures: 1_280, meanPredicted: 0.5450713822, observedRate: 0.54609375 },
    { bin: 6, fixtures: 583, meanPredicted: 0.6409202673, observedRate: 0.6466552316 },
    { bin: 7, fixtures: 249, meanPredicted: 0.74021329, observedRate: 0.686746988 },
    { bin: 8, fixtures: 90, meanPredicted: 0.8363357023, observedRate: 0.8 },
    { bin: 9, fixtures: 13, meanPredicted: 0.9303732557, observedRate: 0.7692307692 },
  ],
  rollingPerformance: [
    { month: "2025-08", fixtures: 560, logLoss: 1.0164698829, logLossConfidence95: [0.9817527902, 1.0511869756], brier: 0.6092123026, brierConfidence95: [0.584706491, 0.6337181143] },
    { month: "2025-09", fixtures: 1_023, logLoss: 1.0217968383, logLossConfidence95: [0.996325868, 1.0472678087], brier: 0.6138191775, brierConfidence95: [0.5958269607, 0.6318113943] },
    { month: "2025-10", fixtures: 1_433, logLoss: 1.0179491259, logLossConfidence95: [0.9965545832, 1.0393436684], brier: 0.6106917004, brierConfidence95: [0.5956098606, 0.6257735403] },
    { month: "2025-11", fixtures: 1_521, logLoss: 1.0109939583, logLossConfidence95: [0.9899169481, 1.0320709686], brier: 0.6052969995, brierConfidence95: [0.5904521279, 0.620141871] },
    { month: "2025-12", fixtures: 1_514, logLoss: 1.0096490062, logLossConfidence95: [0.9880213516, 1.0312766606], brier: 0.6031380904, brierConfidence95: [0.5880747053, 0.6182014755] },
    { month: "2026-01", fixtures: 1_510, logLoss: 1.0049738217, logLossConfidence95: [0.9825491928, 1.0273984505], brier: 0.5993105442, brierConfidence95: [0.5839271421, 0.6146939463] },
    { month: "2026-02", fixtures: 1_493, logLoss: 1.0012948415, logLossConfidence95: [0.9785292123, 1.0240604708], brier: 0.5966652804, brierConfidence95: [0.5810660326, 0.6122645282] },
    { month: "2026-03", fixtures: 1_504, logLoss: 1.0025284121, logLossConfidence95: [0.9803958046, 1.0246610196], brier: 0.5983083594, brierConfidence95: [0.5829941776, 0.6136225412] },
    { month: "2026-04", fixtures: 1_549, logLoss: 1.0072835251, logLossConfidence95: [0.9864959661, 1.0280710842], brier: 0.6019793682, brierConfidence95: [0.5873188373, 0.6166398991] },
    { month: "2026-05", fixtures: 1_518, logLoss: 1.0285809385, logLossConfidence95: [1.0080645612, 1.0490973157], brier: 0.6171334589, brierConfidence95: [0.6026366577, 0.6316302601] },
    { month: "2026-06", fixtures: 1_076, logLoss: 1.0309968032, logLossConfidence95: [1.0068505181, 1.0551430882], brier: 0.6183880008, brierConfidence95: [0.601328384, 0.6354476176] },
    { month: "2026-07", fixtures: 591, logLoss: 1.0523680953, logLossConfidence95: [1.0199691147, 1.0847670759], brier: 0.6334457644, brierConfidence95: [0.61062188, 0.6562696489] },
  ],
  competitionMinimumFixtures: 250,
  competitionPerformance: [
    { competition: "Serie A", country: "Italy", fixtures: 380, logLoss: 1.0161144021, brier: 0.6083804735 },
    { competition: "La Liga", country: "Spain", fixtures: 380, logLoss: 1.0001184909, brier: 0.5970908767 },
    { competition: "Premier League", country: "England", fixtures: 380, logLoss: 1.0402861867, brier: 0.6261756717 },
    { competition: "Jupiler Pro League", country: "Belgium", fixtures: 321, logLoss: 1.0480014963, brier: 0.6319018529 },
    { competition: "Eredivisie", country: "Netherlands", fixtures: 309, logLoss: 1.0077286001, brier: 0.6018815021 },
    { competition: "Ligue 1", country: "France", fixtures: 309, logLoss: 1.0294275677, brier: 0.6181665487 },
    { competition: "Primeira Liga", country: "Portugal", fixtures: 308, logLoss: 0.951181199, brier: 0.5659143089 },
    { competition: "Bundesliga", country: "Germany", fixtures: 308, logLoss: 1.0123702438, brier: 0.6011136574 },
    { competition: "Süper Lig", country: "Turkey", fixtures: 306, logLoss: 1.0175659045, brier: 0.6094421983 },
    { competition: "Czech Liga", country: "Czech Republic", fixtures: 279, logLoss: 1.0246117296, brier: 0.6161241367 },
  ],
  retrospectiveBookmaker: {
    label: "Football-Data closing consensus",
    fixtures: 1_752,
    modelLogLoss: 1.0198198855,
    marketLogLoss: 0.9774406426,
    marketMinusModel: -0.042379243,
    confidence95: [-0.0523418446, -0.0328736669],
    timingLimitation: "Closing prices have no quote timestamps and are retrospective evidence only.",
  },
};
