import { beforeEach, describe, expect, it, vi } from 'vitest';
import { marketRadarApi } from '../marketRadar';

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock('../index', () => ({
  default: { get },
}));

describe('marketRadarApi', () => {
  beforeEach(() => {
    get.mockReset();
  });

  it('loads and camel-cases the latest snapshot', async () => {
    get.mockResolvedValueOnce({
      data: {
        available: true,
        run: {
          run_key: 'cn:20260730T070000Z:manual',
          as_of: '2026-07-30T07:00:00Z',
          market: 'cn',
          trigger: 'manual',
          quality: 'partial',
          scoring_version: 'cn-v1',
          sectors: [],
          provider_trace: [],
          etfs: [],
          regime: null,
          position_plan: null,
        },
      },
    });

    const result = await marketRadarApi.getLatest();

    expect(get).toHaveBeenCalledWith('/api/v1/market-radar/latest');
    expect(result.run?.runKey).toBe('cn:20260730T070000Z:manual');
    expect(result.run?.scoringVersion).toBe('cn-v1');
    expect(result.run?.positionPlan).toBeNull();
  });

  it('loads the persisted sector ranking', async () => {
    get.mockResolvedValueOnce({
      data: {
        available: true,
        run_key: 'cn:20260730T070000Z:manual',
        as_of: '2026-07-30T07:00:00Z',
        items: [
          {
            rank: 1,
            sector: {
              sector_id: 'industry:semiconductor',
              name: 'Semiconductor',
              gross_score: 84,
              risk_deduction: 2,
            },
          },
        ],
        total: 1,
      },
    });

    const result = await marketRadarApi.listSectors();

    expect(get).toHaveBeenCalledWith('/api/v1/market-radar/sectors');
    expect(result.runKey).toBe('cn:20260730T070000Z:manual');
    expect(result.items[0].sector.sectorId).toBe('industry:semiconductor');
    expect(result.items[0].sector.riskDeduction).toBe(2);
  });

  it('encodes canonical sector ids in the detail path', async () => {
    get.mockResolvedValueOnce({
      data: {
        run_key: 'cn:20260730T070000Z:manual',
        as_of: '2026-07-30T07:00:00Z',
        rank: 1,
        sector: { sector_id: 'industry:semiconductor/value' },
        etfs: [],
        position_suggestion: null,
        regime: null,
        position_plan: null,
      },
    });

    const result = await marketRadarApi.getSector('industry:semiconductor/value');

    expect(get).toHaveBeenCalledWith(
      '/api/v1/market-radar/sectors/industry%3Asemiconductor%2Fvalue',
    );
    expect(result.sector.sectorId).toBe('industry:semiconductor/value');
    expect(result.positionSuggestion).toBeNull();
  });
});
