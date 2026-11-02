"""Command line entry point for the nightly screen.

    quant-screen --provider baostock --top-n 10
    quant-screen --provider baostock --review --horizon 5

The screen is meant to run after the close, once the day has landed. The review
is what you run weeks later to find out whether any of it was worth the trouble.
"""

from __future__ import annotations

import argparse

import pandas as pd

from quant_research.config import load_settings
from quant_research.data.cache import (
    build_panel_cache,
    cache_path,
    load_panel_cache,
)
from quant_research.indicators import params_from_settings
from quant_research.logging_setup import get_logger
from quant_research.scoring import screen, to_target_weights
from quant_research.scoring.ledger import ScreenLedger, review

logger = get_logger("screen")


def _names(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _weights(text: str) -> dict[str, float] | None:
    """Parse ``momentum=1.0,rsi_reversal=0.3``; an empty string means "equal"."""
    pairs = [part.strip() for part in text.split(",") if part.strip()]
    if not pairs:
        return None
    weights: dict[str, float] = {}
    for pair in pairs:
        name, _, value = pair.partition("=")
        if not value:
            raise ValueError(f"expected name=weight, got {pair!r}")
        weights[name.strip()] = float(value)
    return weights


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-screen",
        description=(
            "Rank the market and print a short list, or review how earlier lists did. "
            "Defaults come from config/settings.yaml."
        ),
    )
    parser.add_argument("--provider", help="data source to screen (default: settings)")
    parser.add_argument("--adjust", help="price adjustment, e.g. qfq (default: settings)")
    parser.add_argument("--data", help="data root holding raw/ and cache/ (default: settings)")
    parser.add_argument("--top-n", type=int, default=20, help="how many names to keep")
    parser.add_argument("--min-history", type=int, default=60, help="sessions of history required")
    parser.add_argument(
        "--min-amount", type=float, default=None, help="drop names below this average turnover"
    )
    parser.add_argument("--as-of", help="screen this date instead of the latest one")
    parser.add_argument(
        "--indicator-factors",
        default="",
        help=(
            "comma-separated technical factors to add: rsi_reversal, macd_trend, "
            "boll_position (default: none — see docs/INDICATORS.md for why)"
        ),
    )
    parser.add_argument(
        "--factor-weights",
        default="",
        help="comma-separated name=weight, e.g. momentum=1.0,rsi_reversal=0.3",
    )
    parser.add_argument("--note", default="", help="free-text note stored with the record")
    parser.add_argument(
        "--no-record", action="store_true", help="print the list but do not write it down"
    )
    parser.add_argument(
        "--review", action="store_true", help="score earlier screens instead of making a new one"
    )
    parser.add_argument("--horizon", type=int, default=5, help="sessions held, for --review")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    root = args.data or settings.data.directory
    provider = args.provider or settings.collection.provider
    adjust = args.adjust or settings.collection.adjust

    try:
        factor_weights = _weights(args.factor_weights)
        indicator_factors = _names(args.indicator_factors)

        if not cache_path(root, provider, adjust).is_file():
            logger.info("no cached panel yet, building one from the store")
            build_panel_cache(root, provider, adjust)
        panel = load_panel_cache(root, provider, adjust)

        if args.review:
            _print_review(ScreenLedger(), panel, horizon=args.horizon)
            return 0

        result = screen(
            panel,
            as_of=args.as_of,
            top_n=args.top_n,
            min_history=args.min_history,
            min_amount=args.min_amount,
            factor_weights=factor_weights,
            indicator_factors=indicator_factors,
            indicator_params=params_from_settings(settings),
        )
        _print_picks(result)

        if not args.no_record:
            ledger = ScreenLedger()
            record = ledger.record(
                result,
                provider=provider,
                adjust=adjust,
                params={
                    "top_n": args.top_n,
                    "min_history": args.min_history,
                    "min_amount": args.min_amount,
                    # recorded so a list can be reproduced: "which factors" is not
                    # reconstructable from the picks themselves
                    "indicator_factors": list(indicator_factors),
                    "factor_weights": factor_weights,
                },
                note=args.note,
            )
            print(f"\n已归档 → {ledger.path}  ({record.as_of}，{len(record.symbols)} 只)")
        return 0
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("screen failed: %s: %s", type(exc).__name__, exc)
        return 1


def _print_picks(result) -> None:  # type: ignore[no-untyped-def]
    print(
        f"截至 {pd.Timestamp(result.as_of).date()} · 候选 {result.universe} 只"
        f" · 剔除 {result.dropped}"
    )
    print()
    print(result.picks.to_string(float_format=lambda value: f"{value:,.4f}"))
    weights = to_target_weights(result)
    held = weights.loc[result.as_of]
    held = held[held > 0]
    print(f"\n等权目标仓位：{len(held)} 只 × {held.iloc[0]:.2%}")


def _print_review(ledger: ScreenLedger, panel, horizon: int) -> None:  # type: ignore[no-untyped-def]
    frame = review(ledger, panel, horizon=horizon)
    if frame.empty:
        print("还没有可复盘的选股记录——先跑一次 quant-screen，或等持有期走完。")
        return
    print(f"复盘：买入=次日开盘，持有 {horizon} 个交易日，卖出=收盘\n")
    print(frame.to_string(index=False, float_format=lambda value: f"{value:+.4f}"))
    print(
        f"\n共 {len(frame)} 次选股｜平均超额 {frame['excess'].mean():+.4f}"
        f"｜跑赢市场 {frame['beat_market'].mean():.1%}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
