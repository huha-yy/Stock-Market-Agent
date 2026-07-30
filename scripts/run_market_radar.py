from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from src.market_radar.factory import build_market_radar_service


build_service = build_market_radar_service


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
