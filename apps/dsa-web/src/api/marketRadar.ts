import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  MarketRadarLatestResponse,
  MarketRadarSectorDetailResponse,
  MarketRadarSectorListResponse,
} from '../types/marketRadar';

export const marketRadarApi = {
  async getLatest(): Promise<MarketRadarLatestResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/market-radar/latest',
    );
    return toCamelCase<MarketRadarLatestResponse>(response.data);
  },

  async listSectors(): Promise<MarketRadarSectorListResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/market-radar/sectors',
    );
    return toCamelCase<MarketRadarSectorListResponse>(response.data);
  },

  async getSector(sectorId: string): Promise<MarketRadarSectorDetailResponse> {
    const path = `/api/v1/market-radar/sectors/${encodeURIComponent(sectorId)}`;
    const response = await apiClient.get<Record<string, unknown>>(path);
    return toCamelCase<MarketRadarSectorDetailResponse>(response.data);
  },
};
