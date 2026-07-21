from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from data_provider import DataFetcherManager
from src.config import get_config
from src.market_radar.providers import LegacyRankingProvider
from src.market_radar.ranking import RankingConfig
from src.market_radar.repository import MarketRadarRepository
from src.market_radar.service import MarketRadarService
from src.market_radar.universe import UniverseLoader


def build_service() -> MarketRadarService:
    config = get_config()
    return MarketRadarService(
        universe_loader=UniverseLoader(
            ROOT / "src/data/market_radar/a_share_etfs.yaml"
        ),
        provider=LegacyRankingProvider(
            DataFetcherManager(),
            limit=config.market_radar_provider_limit,
        ),
        repository=MarketRadarRepository(),
        ranking_config=RankingConfig(
            scoring_version=config.market_radar_scoring_version,
            stale_after_seconds=config.market_radar_stale_after_seconds,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one A-share Market Radar snapshot"
    )
    parser.add_argument("--market", default="cn")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.market != "cn":
        print("Market Radar Phase 1 supports --market cn only", file=sys.stderr)
        return 2

    try:
        snapshot = build_service().run(market="cn", persist=args.persist)
        rendered = snapshot.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
        return 0
    except Exception as exc:
        print(f"Market Radar failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
