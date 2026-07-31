from __future__ import annotations

from pathlib import Path

from data_provider import DataFetcherManager
from src.config import get_config
from src.market_radar.candidates import CandidateSelector
from src.market_radar.capabilities import MarketRadarEnrichmentConfig
from src.market_radar.capability_provider import ProviderCapabilityAdapter
from src.market_radar.enrichment import MarketRadarEnricher
from src.market_radar.etf_collection import (
    EtfCollectionConfig,
    MarketRadarEtfCollector,
)
from src.market_radar.lifecycle import MarketRadarLifecycleEngine
from src.market_radar.policy_config import (
    EtfPolicyConfig,
    PositionPolicyConfig,
    RegimeConfig,
)
from src.market_radar.providers import LegacyRankingProvider
from src.market_radar.ranking import RankingConfig
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.service import MarketRadarService
from src.market_radar.universe import UniverseLoader


ROOT = Path(__file__).resolve().parents[2]


def build_market_radar_service(
    *,
    persist: bool,
    discovery_only: bool = False,
    repository: MarketRadarRepository | None = None,
) -> MarketRadarService:
    config = get_config()
    ranking_config = RankingConfig(
        scoring_version=config.market_radar_scoring_version,
        stale_after_seconds=config.market_radar_stale_after_seconds,
    )
    enrichment_config = MarketRadarEnrichmentConfig.from_runtime(
        config.market_radar_enrichment_limit,
        config.market_radar_enrichment_budget_seconds,
        config.market_radar_enrichment_max_concurrency,
    )
    manager = DataFetcherManager()
    enricher = None
    etf_collector = None
    candidate_selector = None
    if not discovery_only:
        adapter = ProviderCapabilityAdapter(manager)
        candidate_selector = CandidateSelector()
        enricher = MarketRadarEnricher(
            provider=adapter,
            config=enrichment_config,
        )
        etf_collector = MarketRadarEtfCollector(
            provider=adapter,
            config=EtfCollectionConfig(),
        )
    selected_repository = None
    if persist:
        selected_repository = (
            repository if repository is not None else MarketRadarRepository()
        )
    return MarketRadarService(
        universe_loader=UniverseLoader(
            ROOT / "src/data/market_radar/a_share_etfs.yaml"
        ),
        provider=LegacyRankingProvider(
            manager,
            limit=config.market_radar_provider_limit,
        ),
        repository=selected_repository,
        ranking_config=ranking_config,
        enricher=enricher,
        candidate_selector=candidate_selector,
        enrichment_config=enrichment_config,
        etf_collector=etf_collector,
        etf_policy_config=EtfPolicyConfig(),
        regime_config=RegimeConfig(),
        position_policy_config=PositionPolicyConfig(),
        lifecycle_engine=MarketRadarLifecycleEngine(),
    )
