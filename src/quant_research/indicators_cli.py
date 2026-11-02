"""Command line entry point for the technical indicators.

    quant-indicators --provider baostock --symbols 600519.SH,000001.SZ
    quant-indicators --provider baostock --all
    quant-indicators --from-cache

Parameters come from the ``indicators`` section of ``config/settings.yaml``;
this entry point only decides where the prices come from and where the numbers
land. That is deliberate — a period like RSI(14) belongs in configuration, not
in a shell history.
"""

from __future__ import annotations

import argparse

from quant_research.config import load_settings
from quant_research.data.cache import load_panel_cache
from quant_research.data.providers import get_provider
from quant_research.indicators import (
    compute_all,
    panel_from_frames,
    params_from_settings,
    store_bundle,
)
from quant_research.logging_setup import get_logger

logger = get_logger("indicators")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-indicators",
        description=(
            "Compute the usual technical indicators from collected prices and "
            "store one file per indicator. Parameters live in config/settings.yaml."
        ),
    )
    parser.add_argument("--provider", help="data source to read (default: settings)")
    parser.add_argument("--adjust", help="price adjustment, e.g. qfq (default: settings)")
    parser.add_argument("--symbols", help="comma-separated codes, e.g. 600519.SH,000001.SZ")
    parser.add_argument(
        "--all", dest="all_symbols", action="store_true", help="every symbol the source lists"
    )
    parser.add_argument("--start", help="first date to fetch")
    parser.add_argument("--end", help="last date to fetch")
    parser.add_argument(
        "--from-cache",
        dest="from_cache",
        action="store_true",
        help="compute from the cached panel instead of asking the source again",
    )
    parser.add_argument("--output-directory", help="where the indicator files go")
    parser.add_argument("--format", choices=("parquet", "csv"), help="storage format")
    parser.add_argument("--config", help="path to a settings file")
    return parser


def _symbols(requested: str | None, sweep: bool, provider) -> list[str]:  # type: ignore[no-untyped-def]
    if requested:
        return [part.strip() for part in requested.split(",") if part.strip()]
    if sweep:
        return list(provider.list_symbols())
    raise ValueError("no symbols given: pass --symbols 600519.SH,... or --all")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)

    provider = args.provider or settings.collection.provider
    adjust = args.adjust or settings.collection.adjust
    params = params_from_settings(settings)

    try:
        if args.from_cache:
            panel = load_panel_cache(settings.data.directory, provider, adjust)
        else:
            source = get_provider(provider, adjust=adjust)
            symbols = _symbols(args.symbols, args.all_symbols, source)
            logger.info("fetching %d symbols from %s", len(symbols), provider)
            panel = panel_from_frames(
                source.fetch_daily(symbols, start=args.start, end=args.end),
                source=provider,
                adjust=adjust,
            )
        bundle = compute_all(panel, params)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("indicators failed: %s: %s", type(exc).__name__, exc)
        return 1

    target = store_bundle(
        bundle,
        args.output_directory or settings.indicators.output_directory,
        provider,
        adjust,
        fmt=args.format or settings.indicators.format,
    )

    sample = next(iter(bundle.frames.values()))
    print(
        f"已计算 {len(bundle.frames)} 个指标 · {len(sample.columns)} 只标的"
        f" · {len(sample.index)} 个交易日"
        f"（{sample.index.min().date()} → {sample.index.max().date()}）"
    )
    print(f"→ {target}")
    print("  " + "、".join(bundle.fields))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
