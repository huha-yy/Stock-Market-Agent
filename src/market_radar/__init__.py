from importlib import import_module

from src.market_radar.models import (
    DataQuality,
    EtfDefinition,
    FactorBreakdown,
    RadarRunSnapshot,
    SectorDefinition,
    SectorObservation,
    SectorScore,
    SectorState,
)

_LAZY_IMPORTS = {
    "LegacyRankingProvider": "src.market_radar.providers",
    "MarketRadarProvider": "src.market_radar.providers",
    "ProviderBatch": "src.market_radar.providers",
    "RankingConfig": "src.market_radar.ranking",
    "score_sectors": "src.market_radar.ranking",
    "MarketRadarReplayEngine": "src.market_radar.replay",
    "ReplayFrame": "src.market_radar.replay",
    "MarketRadarRepository": "src.market_radar.repository",
    "MarketRadarService": "src.market_radar.service",
    "UniverseLoader": "src.market_radar.universe",
}


def __getattr__(name: str):
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value

__all__ = [
    "DataQuality",
    "EtfDefinition",
    "FactorBreakdown",
    "LegacyRankingProvider",
    "MarketRadarProvider",
    "MarketRadarReplayEngine",
    "MarketRadarRepository",
    "MarketRadarService",
    "ProviderBatch",
    "RadarRunSnapshot",
    "RankingConfig",
    "ReplayFrame",
    "SectorDefinition",
    "SectorObservation",
    "SectorScore",
    "SectorState",
    "UniverseLoader",
    "score_sectors",
]
