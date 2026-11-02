"""Command line: does a factor rank future returns?

    quant-factor --provider baostock --horizon 5
    quant-factor --provider baostock --factors momentum,rsi_reversal --detail momentum

The screen can rank a market without this; what it cannot do is tell you whether
the ranking means anything. A factor that shows no IC and no ordered buckets is
a second way to say what another factor already said, and the score would be
better off without the extra weight.
"""

from __future__ import annotations

import argparse
from typing import Any

import pandas as pd

from quant_research.config import load_settings
from quant_research.data.cache import build_panel_cache, cache_path, load_panel_cache
from quant_research.indicators import params_from_settings
from quant_research.logging_setup import get_logger
from quant_research.scoring.diagnostics import (
    available_factors,
    compute_factor,
    factor_ic,
    forward_return,
    ic_decay,
    quantile_returns,
    summarise_ic,
)

logger = get_logger("factor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-factor",
        description=(
            "Measure whether each factor ranks forward returns: IC, its "
            "t-statistic, and the quantile ladder."
        ),
    )
    parser.add_argument("--provider", help="data source (default: settings)")
    parser.add_argument("--adjust", help="price adjustment, e.g. qfq (default: settings)")
    parser.add_argument("--data", help="data root holding raw/ and cache/ (default: settings)")
    parser.add_argument(
        "--factors",
        default="",
        help=f"comma-separated factors (default: all of {', '.join(available_factors())})",
    )
    parser.add_argument("--horizon", type=int, default=5, help="forward days to evaluate")
    parser.add_argument("--quantiles", type=int, default=5, help="how many score buckets")
    parser.add_argument(
        "--detail", default="", help="comma-separated factors to print in full (buckets + decay)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    root = args.data or settings.data.directory
    provider = args.provider or settings.collection.provider
    adjust = args.adjust or settings.collection.adjust
    names = _names(args.factors) or available_factors()
    details = _names(args.detail)

    try:
        if not cache_path(root, provider, adjust).is_file():
            logger.info("no cached panel yet, building one from the store")
            build_panel_cache(root, provider, adjust)
        panel = load_panel_cache(root, provider, adjust)
        prices = panel.prices.sort_index()
        params = params_from_settings(settings)

        ahead = forward_return(prices, args.horizon)
        usable = ahead.dropna(how="all")
        print(
            f"{provider}/{adjust} · {prices.shape[1]} 只标的 · "
            f"{len(usable)} 个交易日 · 持有 {args.horizon} 日"
        )
        print(
            f"IC 计算区间 {usable.index.min().date()} → {usable.index.max().date()}"
            f"（最后 {args.horizon} 天没有结果，已排除）"
        )
        print()

        rows = [_measure(name, prices, ahead, args, params) for name in names]
        _print_table(rows)
        _print_reading(len(prices.columns))
        for name in details:
            if name in names:
                _print_detail(name, prices, ahead, args, params)
        return 0
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("diagnostics failed: %s: %s", type(exc).__name__, exc)
        return 1


def _names(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _measure(
    name: str, prices: pd.DataFrame, ahead: pd.DataFrame, args: Any, params: Any
) -> dict[str, Any]:
    """One row of the table; failures become a row too, not a crash."""
    row: dict[str, Any] = {"name": name, "error": ""}
    try:
        factor = compute_factor(name, prices, params)
        summary = summarise_ic(factor_ic(factor, ahead), horizon=args.horizon)
    except ValueError as exc:
        row["error"] = str(exc)
        return row

    row.update(
        ic=summary.mean,
        t_adj=summary.t_stat_adjusted,
        positive=summary.positive_share,
        days=summary.days,
        q1="—",
        q5="—",
        mono=float("nan"),
    )
    try:
        spread = quantile_returns(factor, ahead, quantiles=args.quantiles)
    except ValueError:
        return row
    row["q1"] = f"{spread.returns.iloc[0]:+.2%}"
    row["q5"] = f"{spread.returns.iloc[-1]:+.2%}"
    row["mono"] = spread.monotonicity
    return row


def _print_table(rows: list[dict[str, Any]]) -> None:
    print(
        f"{'factor':<18}{'IC':>9}{'t(adj)':>9}{'IC>0':>8}{'Q1':>9}{'Q5':>9}{'mono':>7}{'days':>7}"
    )
    for row in rows:
        if row["error"]:
            print(f"{row['name']:<18}  — {row['error']}")
            continue
        print(
            f"{row['name']:<18}{row['ic']:>9.4f}{row['t_adj']:>9.2f}"
            f"{row['positive']:>7.0%}{row['q1']:>9}{row['q5']:>9}"
            f"{row['mono']:>7.2f}{row['days']:>7d}"
        )
    print()


def _print_reading(cross_section: int) -> None:
    """The numbers need a scale or they will be read as noise by default."""
    print("读法：")
    print("  IC 是逐日截面秩相关。日度 IC 长期在 0.03–0.05 属于常见水平，不等于没用；")
    print("  真正要警惕的是 IC 接近 0 且分层不单调——那说明这个因子只是在重复别的因子。")
    print("  t(adj) 已按重叠持有期除以 √h 打折：持有 5 日、每天测一次，五个观测里四个是重复的。")
    print("  Q1→Q5 是最低分与最高分组的平均收益，mono 是分层收益与分位序号的秩相关。")
    print(f"  这一截面只有 {cross_section} 只标的，分层每格样本很小，别把小数位当精度。")
    print()


def _print_detail(
    name: str, prices: pd.DataFrame, ahead: pd.DataFrame, args: Any, params: Any
) -> None:
    factor = compute_factor(name, prices, params)
    spread = quantile_returns(factor, ahead, quantiles=args.quantiles)
    print(f"{name} · 分层平均收益（持有 {args.horizon} 日）")
    for bucket, value in spread.returns.items():
        print(f"  Q{int(bucket) + 1}  {value:+7.2%}   平均 {spread.sizes[bucket]:.1f} 只")
    print()

    table = ic_decay(factor, prices, horizons=(1, 5, 10, 20))
    print(f"{name} · IC 衰减")
    for horizon, row in table.iterrows():
        print(
            f"  持有 {int(horizon):>2} 日   IC {row['mean_ic']:+.4f}   "
            f"t(adj) {row['t_adjusted']:+.2f}   IC>0 {row['positive_share']:.0%}"
        )
    print()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
