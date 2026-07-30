import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import type {
  MarketRadarLatestResponse,
  MarketRadarSectorDetailResponse,
  MarketRadarSectorListResponse,
} from '../../types/marketRadar';
import MarketRadarPage from '../MarketRadarPage';

const { getLatest, listSectors, getSector } = vi.hoisted(() => ({
  getLatest: vi.fn(),
  listSectors: vi.fn(),
  getSector: vi.fn(),
}));

vi.mock('../../api/marketRadar', () => ({
  marketRadarApi: { getLatest, listSectors, getSector },
}));

const runKey = 'cn:20260730T070000Z:manual';
const asOf = '2026-07-30T07:00:00Z';

function sector(sectorId: string, name: string, score: number) {
  return {
    sectorId,
    name,
    kind: 'industry',
    scoringVersion: 'cn-v1',
    grossScore: score + 2,
    riskDeduction: 2,
    score,
    confidence: 0.82,
    state: score > 70 ? 'leading' : 'neutral',
    factors: {
      trendMomentum: 20,
      relativeStrength: 17,
      capitalFlow: 14,
      breadth: 11,
      liquidityExpansion: 8,
      catalyst: 7,
    },
    riskReasons: score > 70 ? ['crowded_trade'] : [],
    missingFields: score > 70 ? ['capital_flow_20d'] : [],
    source: 'akshare',
    observedAt: asOf,
    quality: 'partial',
    observation: {},
  } as const;
}

const semiconductor = sector('industry:semiconductor', 'Semiconductor', 78);
const bank = sector('industry:bank', 'Bank', 63);

const latest: MarketRadarLatestResponse = {
  available: true,
  run: {
    runKey,
    market: 'cn',
    trigger: 'manual',
    asOf,
    quality: 'partial',
    scoringVersion: 'cn-v1',
    sectors: [semiconductor, bank],
    providerTrace: [],
    etfs: [],
    regime: {
      regimeVersion: 'cn-regime-v1',
      asOf,
      score: 61,
      regime: 'selective',
      confidence: 0.76,
      coverage: 0.8,
      components: null,
      cohortSectorIds: [semiconductor.sectorId, bank.sectorId],
      excludedSectorReasons: {},
      missingFields: [],
      reasons: ['mixed_breadth'],
    },
    positionPlan: {
      policyVersion: 'cn-position-v1',
      asOf,
      regime: 'selective',
      totalPositionMinPct: 35,
      totalPositionMaxPct: 55,
      suggestions: [],
      correlationGroups: [],
      correlationCoverage: 1,
      confidence: 0.72,
      reasonCodes: [],
    },
  },
};

const sectors: MarketRadarSectorListResponse = {
  available: true,
  runKey,
  asOf,
  items: [
    { rank: 1, sector: semiconductor },
    { rank: 2, sector: bank },
  ],
  total: 2,
};

function detailFor(selected = semiconductor): MarketRadarSectorDetailResponse {
  const etfName = selected.sectorId === bank.sectorId ? 'Bank ETF' : 'Chip ETF';
  const etfCode = selected.sectorId === bank.sectorId ? '512800' : '512480';
  return {
    runKey,
    asOf,
    rank: selected.sectorId === bank.sectorId ? 2 : 1,
    sector: selected,
    etfs: [{
      sectorId: selected.sectorId,
      code: etfCode,
      name: etfName,
      status: 'best_supported',
      eligible: true,
      rank: 1,
      score: 81,
      confidence: 0.78,
      components: { liquidity: 90, trend: 76, trackingQuality: 83, cost: 80, size: 88 },
      effectiveWeights: {},
      reasonCodes: [],
      observation: {
        market: 'cn',
        sectorId: selected.sectorId,
        code: etfCode,
        name: etfName,
        observedAt: asOf,
        dataDate: '2026-07-30',
        barStatus: 'finalized',
        source: 'akshare',
        quality: 'complete',
        freshnessSeconds: 120,
        mappingEffectiveFrom: '2026-01-01',
        mappingEffectiveTo: null,
        benchmarkCode: null,
        active: true,
        finalizedSessionCount: 60,
        suspended: false,
        currentPrice: 1.23,
        currentTradedAmount: 1_000_000,
        averageTradedAmount20d: 900_000,
        spreadBps: 5,
        premiumDiscountPct: 0.1,
        return20dPct: 6.2,
        return60dPct: 12.1,
        dailyReturnDates60: [],
        dailyReturns60: [],
        trackingErrorPct: 0.8,
        trackingDifferencePct: -0.2,
        annualFeePct: 0.5,
        sizeCny: 8_000_000_000,
        liquidityStability: 0.92,
        missingFields: [],
        rawReference: {},
      },
    }],
    positionSuggestion: {
      sectorId: selected.sectorId,
      sectorName: selected.name,
      sectorRank: selected.sectorId === bank.sectorId ? 2 : 1,
      etfCode,
      etfStatus: 'best_supported',
      minimumPct: null,
      sectorCapPct: 12,
      etfCapPct: 8,
      jointConfidence: 0.74,
      invalidationCodes: ['sector_state_deteriorated'],
    },
    regime: latest.run?.regime ?? null,
    positionPlan: latest.run?.positionPlan ?? null,
  };
}

function renderPage() {
  window.localStorage.setItem('dsa.uiLanguage', 'en');
  return render(
    <UiLanguageProvider>
      <MarketRadarPage />
    </UiLanguageProvider>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  getLatest.mockResolvedValue(latest);
  listSectors.mockResolvedValue(sectors);
  getSector.mockImplementation((sectorId: string) => (
    Promise.resolve(detailFor(sectorId === bank.sectorId ? bank : semiconductor))
  ));
});

describe('MarketRadarPage', () => {
  it('renders overview and preserves persisted ranking order', async () => {
    renderPage();

    expect(await screen.findByText('Selective')).toBeInTheDocument();
    const rows = screen.getAllByRole('button', { name: /sector rank/i });
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('Semiconductor'),
      expect.stringContaining('Bank'),
    ]);
    expect(screen.getByText('35.0% - 55.0%')).toBeInTheDocument();
  });

  it('loads detail when a sector is selected', async () => {
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /Bank/ }));
    await waitFor(() => expect(getSector).toHaveBeenCalledWith(bank.sectorId));
    expect(await screen.findByText('Bank ETF')).toBeInTheDocument();
  });

  it('shows bootstrap state and retries both summaries', async () => {
    getLatest.mockResolvedValue({ available: false, run: null });
    listSectors.mockResolvedValue({ available: false, runKey: null, asOf: null, items: [], total: 0 });
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }));
    await waitFor(() => {
      expect(getLatest).toHaveBeenCalledTimes(2);
      expect(listSectors).toHaveBeenCalledTimes(2);
    });
  });

  it('rejects summary payloads from different snapshots', async () => {
    listSectors.mockResolvedValue({ ...sectors, runKey: 'cn:other' });
    renderPage();

    expect(await screen.findByText('Market Radar overview could not be loaded')).toBeInTheDocument();
    expect(screen.queryByText('Semiconductor')).not.toBeInTheDocument();
  });

  it('keeps the summary usable when sector detail fails', async () => {
    getSector.mockRejectedValue(new Error('detail unavailable'));
    renderPage();

    expect(await screen.findByText('Semiconductor')).toBeInTheDocument();
    expect(await screen.findByText('Sector detail could not be loaded')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Bank/ })).toBeEnabled();
  });

  it('renders legacy snapshots without regime or position policy', async () => {
    getLatest.mockResolvedValue({
      ...latest,
      run: latest.run ? { ...latest.run, regime: null, positionPlan: null } : null,
    });
    renderPage();

    expect(await screen.findByText('Semiconductor')).toBeInTheDocument();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0);
  });

  it('falls back safely for unknown enum values', async () => {
    getLatest.mockResolvedValue({
      ...latest,
      run: latest.run ? {
        ...latest.run,
        quality: 'future_quality',
        regime: latest.run.regime ? { ...latest.run.regime, regime: 'future_regime' } : null,
      } : null,
    });
    renderPage();

    expect(await screen.findByText('Unknown (future_regime)')).toBeInTheDocument();
    expect(screen.getByText('Unknown (future_quality)')).toBeInTheDocument();
  });

  it('refreshes summaries manually', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }));

    await waitFor(() => expect(getLatest).toHaveBeenCalledTimes(2));
    expect(listSectors).toHaveBeenCalledTimes(2);
  });

  it('ignores a stale detail response after a newer selection', async () => {
    const first = deferred<MarketRadarSectorDetailResponse>();
    getSector.mockImplementationOnce(() => first.promise);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /Bank/ }));
    expect(await screen.findByText('Bank ETF')).toBeInTheDocument();
    first.resolve(detailFor(semiconductor));

    await waitFor(() => expect(screen.queryByText('Chip ETF')).not.toBeInTheDocument());
    expect(screen.getByText('Bank ETF')).toBeInTheDocument();
  });
});
