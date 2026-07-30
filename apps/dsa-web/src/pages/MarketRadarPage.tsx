import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Radar, RefreshCw } from 'lucide-react';
import { marketRadarApi } from '../api/marketRadar';
import { createParsedApiError, getParsedApiError, type ParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, EmptyState, PageHeader } from '../components/common';
import {
  MarketRadarOverview,
  MarketRadarSectorDetail,
  MarketRadarSectorList,
} from '../components/market-radar';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type {
  MarketRadarLatestResponse,
  MarketRadarSectorDetailResponse,
  MarketRadarSectorListResponse,
} from '../types/marketRadar';

type SummaryState =
  | { status: 'loading' }
  | { status: 'error'; error: ParsedApiError }
  | { status: 'ready'; latest: MarketRadarLatestResponse; sectors: MarketRadarSectorListResponse };

type DetailState =
  | { status: 'idle' | 'loading' }
  | { status: 'error'; error: ParsedApiError }
  | { status: 'ready'; detail: MarketRadarSectorDetailResponse };

const MarketRadarPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [summaryState, setSummaryState] = useState<SummaryState>({ status: 'loading' });
  const [selectedSectorId, setSelectedSectorId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ status: 'idle' });
  const summaryRequestRef = useRef(0);
  const detailRequestRef = useRef(0);

  useEffect(() => {
    document.title = t('marketRadar.pageTitle');
  }, [t]);

  const toSectionError = useCallback((error: unknown, key: 'marketRadar.summaryError' | 'marketRadar.detailError') => {
    const parsed = getParsedApiError(error);
    return createParsedApiError({
      title: t(key),
      message: parsed.message,
      rawMessage: parsed.rawMessage,
      status: parsed.status,
      category: parsed.category,
    });
  }, [t]);

  const loadSummary = useCallback(async () => {
    const requestId = ++summaryRequestRef.current;
    setSummaryState({ status: 'loading' });
    try {
      const [latest, sectors] = await Promise.all([
        marketRadarApi.getLatest(),
        marketRadarApi.listSectors(),
      ]);
      if (requestId !== summaryRequestRef.current) return;
      if (latest.run && sectors.runKey && latest.run.runKey !== sectors.runKey) {
        throw new Error('Market Radar summary snapshot mismatch');
      }
      setSummaryState({ status: 'ready', latest, sectors });
      setSelectedSectorId((current) => (
        current && sectors.items.some(({ sector }) => sector.sectorId === current)
          ? current
          : sectors.items[0]?.sector.sectorId ?? null
      ));
    } catch (error) {
      if (requestId === summaryRequestRef.current) {
        setSummaryState({ status: 'error', error: toSectionError(error, 'marketRadar.summaryError') });
      }
    }
  }, [toSectionError]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  useEffect(() => {
    if (!selectedSectorId) {
      ++detailRequestRef.current;
      setDetailState({ status: 'idle' });
      return;
    }
    const requestId = ++detailRequestRef.current;
    setDetailState({ status: 'loading' });
    void marketRadarApi.getSector(selectedSectorId)
      .then((detail) => {
        if (requestId === detailRequestRef.current) {
          setDetailState({ status: 'ready', detail });
        }
      })
      .catch((error) => {
        if (requestId === detailRequestRef.current) {
          setDetailState({ status: 'error', error: toSectionError(error, 'marketRadar.detailError') });
        }
      });
  }, [selectedSectorId, toSectionError]);

  const ready = summaryState.status === 'ready' ? summaryState : null;
  const hasSnapshot = Boolean(ready?.latest.available && ready.latest.run && ready.sectors.available);

  return (
    <AppPage className="space-y-5">
      <PageHeader
        eyebrow="Market Radar"
        title={t('marketRadar.title')}
        description={t('marketRadar.description')}
        actions={(
          <button
            type="button"
            onClick={() => void loadSummary()}
            className="inline-flex h-9 items-center gap-2 rounded-md border border-border/60 bg-elevated px-3 text-sm font-medium text-foreground transition hover:border-cyan/50 hover:text-cyan disabled:opacity-50"
            disabled={summaryState.status === 'loading'}
          >
            <RefreshCw size={15} aria-hidden="true" />
            {t('marketRadar.refresh')}
          </button>
        )}
      />

      {summaryState.status === 'loading' ? (
        <div className="rounded-lg border border-border/50 bg-card/50 px-5 py-8 text-center text-sm text-secondary-text" role="status">
          {t('marketRadar.loading')}
        </div>
      ) : null}

      {summaryState.status === 'error' ? (
        <ApiErrorAlert error={summaryState.error} actionLabel={t('common.retry')} onAction={() => void loadSummary()} />
      ) : null}

      {ready && !hasSnapshot ? (
        <EmptyState
          icon={<Radar size={24} aria-hidden="true" />}
          title={t('marketRadar.emptyTitle')}
          description={t('marketRadar.emptyDescription')}
        />
      ) : null}

      {ready && hasSnapshot && ready.latest.run ? (
        <>
          <MarketRadarOverview snapshot={ready.latest.run} />
          <div className="grid items-start gap-5 lg:grid-cols-[minmax(18rem,0.85fr)_minmax(0,1.4fr)]">
            <MarketRadarSectorList
              items={ready.sectors.items}
              selectedSectorId={selectedSectorId}
              onSelect={setSelectedSectorId}
            />
            <div className="min-w-0">
              {detailState.status === 'loading' || detailState.status === 'idle' ? (
                <div className="rounded-lg border border-border/50 bg-card/50 px-5 py-8 text-center text-sm text-secondary-text" role="status">
                  {t('common.loading')}
                </div>
              ) : null}
              {detailState.status === 'error' ? <ApiErrorAlert error={detailState.error} /> : null}
              {detailState.status === 'ready' ? <MarketRadarSectorDetail detail={detailState.detail} /> : null}
            </div>
          </div>
        </>
      ) : null}
    </AppPage>
  );
};

export default MarketRadarPage;
