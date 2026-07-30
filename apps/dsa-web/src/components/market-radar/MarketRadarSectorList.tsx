import type React from 'react';
import { Badge, Card } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import type { MarketRadarSectorListItem } from '../../types/marketRadar';
import { cn } from '../../utils/cn';

interface MarketRadarSectorListProps {
  items: MarketRadarSectorListItem[];
  selectedSectorId: string | null;
  onSelect: (sectorId: string) => void;
}

const states = ['leading', 'improving', 'neutral', 'weakening', 'avoid', 'insufficient_data'];

export const MarketRadarSectorList: React.FC<MarketRadarSectorListProps> = ({
  items,
  selectedSectorId,
  onSelect,
}) => {
  const { t } = useUiLanguage();

  return (
    <Card padding="none" className="overflow-hidden rounded-lg">
      <div className="border-b border-border/50 px-4 py-4">
        <h2 id="market-radar-sector-ranking-title" className="text-base font-semibold text-foreground">{t('marketRadar.ranking.title')}</h2>
        <p className="mt-1 text-xs text-secondary-text">{t('marketRadar.ranking.description')}</p>
      </div>
      <div className="hidden grid-cols-[3rem_minmax(0,1fr)_4rem_5rem] gap-2 border-b border-border/50 px-4 py-2 text-xs text-secondary-text sm:grid">
        <span>{t('marketRadar.ranking.rank')}</span>
        <span>{t('marketRadar.ranking.sector')}</span>
        <span className="text-right">{t('marketRadar.ranking.score')}</span>
        <span className="text-right">{t('marketRadar.ranking.confidence')}</span>
      </div>
      <div
        role="list"
        aria-labelledby="market-radar-sector-ranking-title"
        className="divide-y divide-border/40"
      >
        {items.map(({ rank, sector }) => {
          const state = states.includes(sector.state)
            ? t(`marketRadar.state.${sector.state}` as UiTextKey)
            : t('marketRadar.unknown', { value: sector.state });
          const score = sector.score.toFixed(1);
          const confidence = `${Math.round(sector.confidence * 100)}%`;
          return (
            <div key={sector.sectorId} role="listitem">
              <button
                type="button"
                aria-label={t('marketRadar.ranking.aria', {
                  rank,
                  name: sector.name,
                  state,
                  score,
                  confidence,
                })}
                aria-pressed={sector.sectorId === selectedSectorId}
                onClick={() => onSelect(sector.sectorId)}
                className={cn(
                  'grid w-full grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-2 px-4 py-3 text-left transition hover:bg-elevated/60 sm:grid-cols-[3rem_minmax(0,1fr)_4rem_5rem]',
                  sector.sectorId === selectedSectorId && 'bg-cyan/8 ring-1 ring-inset ring-cyan/25',
                )}
              >
                <span className="font-mono text-sm text-secondary-text">#{rank}</span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-foreground">{sector.name}</span>
                  <Badge className="mt-1 rounded-md" variant={sector.state === 'leading' ? 'success' : 'default'}>{state}</Badge>
                </span>
                <span className="text-right font-mono text-sm font-semibold text-foreground">{score}</span>
                <span className="hidden text-right font-mono text-sm text-secondary-text sm:block">{confidence}</span>
              </button>
            </div>
          );
        })}
      </div>
    </Card>
  );
};
