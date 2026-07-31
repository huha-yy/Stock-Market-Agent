export type MarketRadarQuality = 'complete' | 'partial' | 'stale' | 'unavailable';
export type MarketRadarSectorKind = 'industry' | 'concept';
export type MarketRadarSectorState =
  | 'leading'
  | 'improving'
  | 'neutral'
  | 'weakening'
  | 'avoid'
  | 'insufficient_data';
export type MarketRadarEtfStatus =
  | 'best_supported'
  | 'candidate'
  | 'rejected'
  | 'insufficient_data';
export type MarketRadarRegime =
  | 'risk_on'
  | 'selective'
  | 'defensive'
  | 'risk_off'
  | 'insufficient_data';

export interface MarketRadarFactorBreakdown {
  trendMomentum: number;
  relativeStrength: number;
  capitalFlow: number;
  breadth: number;
  liquidityExpansion: number;
  catalyst: number;
}

export interface MarketRadarSectorScore {
  sectorId: string;
  name: string;
  kind: MarketRadarSectorKind;
  scoringVersion: 'cn-v1';
  grossScore: number;
  riskDeduction: number;
  score: number;
  confidence: number;
  state: MarketRadarSectorState;
  factors: MarketRadarFactorBreakdown | Record<string, unknown>;
  riskReasons: string[];
  missingFields: string[];
  source: string;
  observedAt: string;
  quality: MarketRadarQuality;
  observation: Record<string, unknown>;
}

export interface MarketRadarEtfObservation {
  market: 'cn';
  sectorId: string;
  code: string;
  name: string;
  observedAt: string;
  dataDate: string | null;
  barStatus: 'provisional' | 'finalized' | null;
  source: string;
  quality: MarketRadarQuality;
  freshnessSeconds: number;
  mappingEffectiveFrom: string;
  mappingEffectiveTo: string | null;
  benchmarkCode: string | null;
  active: boolean | null;
  finalizedSessionCount: number | null;
  suspended: boolean | null;
  currentPrice: number | null;
  currentTradedAmount: number | null;
  averageTradedAmount20d: number | null;
  spreadBps: number | null;
  premiumDiscountPct: number | null;
  return20dPct: number | null;
  return60dPct: number | null;
  dailyReturnDates60: string[];
  dailyReturns60: number[];
  trackingErrorPct: number | null;
  trackingDifferencePct: number | null;
  annualFeePct: number | null;
  sizeCny: number | null;
  liquidityStability: number | null;
  missingFields: string[];
  rawReference: Record<string, unknown>;
}

export interface MarketRadarEtfComponentScores {
  liquidity: number | null;
  trend: number | null;
  trackingQuality: number | null;
  cost: number | null;
  size: number | null;
}

export interface MarketRadarEtfSelection {
  sectorId: string;
  code: string;
  name: string;
  status: MarketRadarEtfStatus;
  eligible: boolean;
  rank: number | null;
  score: number | null;
  confidence: number;
  components: MarketRadarEtfComponentScores;
  effectiveWeights: Record<string, number>;
  reasonCodes: string[];
  observation: MarketRadarEtfObservation;
}

export interface MarketRadarRegimeComponents {
  benchmarkTrend: number;
  positiveSectorDiffusion: number;
  flowDiffusion: number;
  liquidityDiffusion: number;
  nonRiskSectorShare: number;
}

export interface MarketRadarRegimeAssessment {
  regimeVersion: 'cn-regime-v1';
  asOf: string;
  score: number | null;
  regime: MarketRadarRegime;
  confidence: number;
  coverage: number;
  components: MarketRadarRegimeComponents | null;
  cohortSectorIds: string[];
  excludedSectorReasons: Record<string, string[]>;
  missingFields: string[];
  reasons: string[];
}

export interface MarketRadarPositionSuggestion {
  sectorId: string;
  sectorName: string;
  sectorRank: number;
  etfCode: string;
  etfStatus: 'best_supported' | 'candidate';
  minimumPct: null;
  sectorCapPct: number;
  etfCapPct: number;
  jointConfidence: number;
  invalidationCodes: string[];
}

export interface MarketRadarCorrelationGroup {
  etfCodes: string[];
  maximumTotalPct: number;
}

export interface MarketRadarPositionPlan {
  policyVersion: 'cn-position-v1';
  asOf: string;
  regime: MarketRadarRegime;
  totalPositionMinPct: number;
  totalPositionMaxPct: number;
  suggestions: MarketRadarPositionSuggestion[];
  correlationGroups: MarketRadarCorrelationGroup[];
  correlationCoverage: number;
  confidence: number;
  reasonCodes: string[];
}

export interface MarketRadarSnapshot {
  runKey: string;
  market: 'cn';
  trigger: 'manual' | 'schedule' | 'replay';
  asOf: string;
  quality: MarketRadarQuality;
  scoringVersion: 'cn-v1';
  sectors: MarketRadarSectorScore[];
  providerTrace: Array<Record<string, unknown>>;
  etfs: MarketRadarEtfSelection[];
  regime: MarketRadarRegimeAssessment | null;
  positionPlan: MarketRadarPositionPlan | null;
}

export interface MarketRadarLatestResponse {
  available: boolean;
  run: MarketRadarSnapshot | null;
}

export interface MarketRadarSectorListItem {
  rank: number;
  sector: MarketRadarSectorScore;
}

export interface MarketRadarSectorListResponse {
  available: boolean;
  runKey: string | null;
  asOf: string | null;
  items: MarketRadarSectorListItem[];
  total: number;
}

export interface MarketRadarSectorDetailResponse {
  runKey: string;
  asOf: string;
  rank: number;
  sector: MarketRadarSectorScore;
  etfs: MarketRadarEtfSelection[];
  positionSuggestion: MarketRadarPositionSuggestion | null;
  regime: MarketRadarRegimeAssessment | null;
  positionPlan: MarketRadarPositionPlan | null;
}
