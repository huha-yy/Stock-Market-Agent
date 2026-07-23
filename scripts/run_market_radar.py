from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def build_service(
    *,
    persist: bool,
    discovery_only: bool = False,
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
    return MarketRadarService(
        universe_loader=UniverseLoader(
            ROOT / "src/data/market_radar/a_share_etfs.yaml"
        ),
        provider=LegacyRankingProvider(
            manager,
            limit=config.market_radar_provider_limit,
        ),
        repository=MarketRadarRepository() if persist else None,
        ranking_config=ranking_config,
        enricher=enricher,
        candidate_selector=candidate_selector,
        enrichment_config=enrichment_config,
        etf_collector=etf_collector,
        etf_policy_config=EtfPolicyConfig(),
        regime_config=RegimeConfig(),
        position_policy_config=PositionPolicyConfig(),
    )


def _write_output_atomic(output: Path, rendered: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(rendered + "\n")
            temporary.flush()
        temporary_path.replace(output)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one A-share Market Radar snapshot"
    )
    parser.add_argument("--market", default="cn")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.market != "cn":
        print("Market Radar supports --market cn only", file=sys.stderr)
        return 2

    try:
        snapshot = build_service(
            persist=args.persist,
            discovery_only=args.discovery_only,
        ).run(
            market="cn",
            persist=args.persist,
            discovery_only=args.discovery_only,
        )
        rendered = snapshot.model_dump_json(indent=2)
        if args.output:
            _write_output_atomic(args.output, rendered)
        else:
            print(rendered)
        return 0
    except Exception as exc:
        print(f"Market Radar failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
