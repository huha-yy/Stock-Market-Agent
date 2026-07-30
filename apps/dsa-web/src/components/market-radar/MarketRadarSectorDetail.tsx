import type React from 'react';
import { Badge, Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import type { MarketRadarSectorDetailResponse } from '../../types/marketRadar';

interface MarketRadarSectorDetailProps {
  detail: MarketRadarSectorDetailResponse;
}

const factorKeys: Record<string, UiTextKey> = {
  trendMomentum: 'marketRadar.factor.trendMomentum',
  relativeStrength: 'marketRadar.factor.relativeStrength',
  capitalFlow: 'marketRadar.factor.capitalFlow',
  breadth: 'marketRadar.factor.breadth',
  liquidityExpansion: 'marketRadar.factor.liquidityExpansion',
  catalyst: 'marketRadar.factor.catalyst',
};
const etfStatuses = ['best_supported', 'candidate', 'rejected', 'insufficient_data'];

function humanize(value: string): string {
  return value.replaceAll('_', ' ');
}

function percent(value: number | null | undefined, unavailable: string): string {
  return value == null || !Number.isFinite(value) ? unavailable : `${value.toFixed(1)}%`;
}

function confidence(value: number | null | undefined, unavailable: string): string {
  return value == null || !Number.isFinite(value)
    ? unavailable
    : `${Math.round(value * 100)}%`;
}

export const MarketRadarSectorDetail: React.FC<MarketRadarSectorDetailProps> = ({ detail }) => {
  const { t } = useUiLanguage();
  const unavailable = t('marketRadar.unavailable');
  const factors = Object.entries(detail.sector.factors);

  return (
    <div className="space-y-4">
      <Card className="rounded-lg">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs text-secondary-text">{t('marketRadar.detail.title')} · #{detail.rank}</p>
            <h2 className="mt-1 text-lg font-semibold text-foreground">{detail.sector.name}</h2>
          </div>
          <div className="text-right">
            <p className="font-mono text-2xl font-semibold text-foreground">{detail.sector.score.toFixed(1)}</p>
            <p className="text-xs text-secondary-text">{confidence(detail.sector.confidence, unavailable)}</p>
          </div>
        </div>
        <h3 className="mt-5 text-sm font-semibold text-foreground">{t('marketRadar.detail.factors')}</h3>
        <dl className="mt-2 grid grid-cols-2 gap-x-5 gap-y-2 text-sm sm:grid-cols-3">
          {factors.map(([key, value]) => (
            <div key={key} className="flex items-center justify-between gap-2 border-b border-border/40 py-1.5">
              <dt className="truncate text-secondary-text" title={key}>{factorKeys[key] ? t(factorKeys[key]) : humanize(key)}</dt>
              <dd className="font-mono text-foreground">{typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : unavailable}</dd>
            </div>
          ))}
        </dl>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <EvidenceList title={t('marketRadar.detail.risks')} values={detail.sector.riskReasons} empty={t('marketRadar.detail.noRisks')} />
          <EvidenceList title={t('marketRadar.detail.missing')} values={detail.sector.missingFields} empty={t('marketRadar.detail.noMissing')} />
        </div>
      </Card>

      <Card title={t('marketRadar.detail.etfs')} className="rounded-lg">
        {detail.etfs.length === 0 ? (
          <p className="text-sm text-secondary-text">{t('marketRadar.detail.noEtfs')}</p>
        ) : (
          <div className="divide-y divide-border/40">
            {detail.etfs.map((etf) => {
              const status = etfStatuses.includes(etf.status)
                ? t(`marketRadar.etf.${etf.status}` as UiTextKey)
                : t('marketRadar.unknown', { value: etf.status });
              return (
                <div key={etf.code} className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-foreground">{etf.name}</p>
                    <p className="mt-1 font-mono text-xs text-secondary-text">{etf.code}</p>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge className="rounded-md" variant={etf.eligible ? 'success' : 'warning'}>{status}</Badge>
                    <span className="font-mono text-sm text-foreground">{etf.score == null ? unavailable : etf.score.toFixed(1)}</span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card title={t('marketRadar.detail.policy')} className="rounded-lg">
        {detail.positionSuggestion ? (
          <>
            <dl className="grid grid-cols-3 gap-3">
              <Metric label={t('marketRadar.detail.sectorCap')} value={percent(detail.positionSuggestion.sectorCapPct, unavailable)} />
              <Metric label={t('marketRadar.detail.etfCap')} value={percent(detail.positionSuggestion.etfCapPct, unavailable)} />
              <Metric label={t('marketRadar.detail.jointConfidence')} value={confidence(detail.positionSuggestion.jointConfidence, unavailable)} />
            </dl>
            <EvidenceList
              className="mt-4"
              title={t('marketRadar.detail.invalidation')}
              values={detail.positionSuggestion.invalidationCodes}
              empty={unavailable}
            />
          </>
        ) : (
          <p className="text-sm text-secondary-text">{t('marketRadar.detail.noPolicy')}</p>
        )}
      </Card>
    </div>
  );
};

const Metric: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="min-w-0">
    <dt className="text-xs text-secondary-text">{label}</dt>
    <dd className="mt-1 truncate font-mono text-sm font-semibold text-foreground" title={value}>{value}</dd>
  </div>
);

const EvidenceList: React.FC<{
  title: string;
  values: string[];
  empty: string;
  className?: string;
}> = ({ title, values, empty, className = '' }) => (
  <section className={className}>
    <h3 className="text-sm font-semibold text-foreground">{title}</h3>
    {values.length > 0 ? (
      <ul className="mt-2 space-y-1 text-xs text-secondary-text">
        {values.map((value) => <li key={value}>• {humanize(value)}</li>)}
      </ul>
    ) : <p className="mt-2 text-xs text-secondary-text">{empty}</p>}
  </section>
);
