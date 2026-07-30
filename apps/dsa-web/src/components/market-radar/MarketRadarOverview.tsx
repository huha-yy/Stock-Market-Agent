import type React from 'react';
import { Badge, Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import type { MarketRadarSnapshot } from '../../types/marketRadar';

interface MarketRadarOverviewProps {
  snapshot: MarketRadarSnapshot;
}

function knownLabel(
  value: string | null | undefined,
  prefix: 'marketRadar.regime' | 'marketRadar.quality',
  known: readonly string[],
  t: ReturnType<typeof useUiLanguage>['t'],
): string {
  if (!value) return t('marketRadar.unavailable');
  if (!known.includes(value)) return t('marketRadar.unknown', { value });
  return t(`${prefix}.${value}` as UiTextKey);
}

function percent(value: number | null | undefined, unavailable: string): string {
  return value == null || !Number.isFinite(value) ? unavailable : `${value.toFixed(1)}%`;
}

export const MarketRadarOverview: React.FC<MarketRadarOverviewProps> = ({ snapshot }) => {
  const { language, t } = useUiLanguage();
  const unavailable = t('marketRadar.unavailable');
  const positionPlan = snapshot.positionPlan;
  const positionRange = positionPlan
    ? `${percent(positionPlan.totalPositionMinPct, unavailable)} - ${percent(positionPlan.totalPositionMaxPct, unavailable)}`
    : unavailable;
  const observedAt = Number.isNaN(Date.parse(snapshot.asOf))
    ? unavailable
    : new Intl.DateTimeFormat(language === 'en' ? 'en-US' : 'zh-CN', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(snapshot.asOf));

  const metrics = [
    {
      label: t('marketRadar.overview.regime'),
      value: knownLabel(
        snapshot.regime?.regime,
        'marketRadar.regime',
        ['risk_on', 'selective', 'defensive', 'risk_off', 'insufficient_data'],
        t,
      ),
    },
    {
      label: t('marketRadar.overview.quality'),
      value: knownLabel(
        snapshot.quality,
        'marketRadar.quality',
        ['complete', 'partial', 'stale', 'unavailable'],
        t,
      ),
    },
    {
      label: t('marketRadar.overview.coverage'),
      value: snapshot.regime
        ? percent(snapshot.regime.coverage * 100, unavailable)
        : unavailable,
    },
    { label: t('marketRadar.overview.positionRange'), value: positionRange },
  ];

  return (
    <Card padding="none" className="overflow-hidden rounded-lg">
      <div className="grid grid-cols-2 divide-x divide-y divide-border/50 lg:grid-cols-4 lg:divide-y-0">
        {metrics.map((metric) => (
          <div key={metric.label} className="min-w-0 px-4 py-4">
            <p className="text-xs text-secondary-text">{metric.label}</p>
            <p className="mt-1 truncate text-base font-semibold text-foreground" title={metric.value}>
              {metric.value}
            </p>
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-2 border-t border-border/50 px-4 py-3 text-xs text-secondary-text sm:flex-row sm:items-center sm:justify-between">
        <span>{t('marketRadar.overview.snapshot')}: {observedAt}</span>
        <Badge className="max-w-full rounded-md font-mono" title={snapshot.runKey}>
          <span className="truncate">{t('marketRadar.overview.runKey')}: {snapshot.runKey}</span>
        </Badge>
      </div>
    </Card>
  );
};
