"""Command line: which names carry financial risk the price does not show?

    quant-fundamentals --symbols 600519.SH,601166.SH
    quant-fundamentals --refresh                 # refetch and store
    quant-fundamentals --as-of 2026-09-01        # what was knowable that day
    quant-fundamentals --min-level 中            # only the ones worth worrying about

Sorted riskiest first, because the useful thing to see is what to exclude, not a
list in the order it arrived.
"""

from __future__ import annotations

import argparse
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.config import load_settings
from quant_research.data.cache import load_panel_cache
from quant_research.fundamentals import assess_many, collect, load, merge, store
from quant_research.fundamentals.risk import LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_UNKNOWN
from quant_research.logging_setup import get_logger

logger = get_logger("fundamentals")

_ORDER = {LEVEL_HIGH: 3, LEVEL_MEDIUM: 2, "低": 1, LEVEL_UNKNOWN: 0}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-fundamentals",
        description="Read quarterly reports and flag financial risk before a name enters a screen.",
    )
    parser.add_argument("--provider", help="data source (financial data: baostock only)")
    parser.add_argument("--adjust", help="price adjustment, for locating the cached panel")
    parser.add_argument("--data", help="data root holding raw/ and cache/ (default: settings)")
    parser.add_argument(
        "--symbols", default="", help="comma-separated; default: names in the panel"
    )
    parser.add_argument("--quarters", type=int, default=0, help="how many quarters to fetch")
    parser.add_argument("--as-of", dest="as_of", help="judge with reports public on this date")
    parser.add_argument("--refresh", action="store_true", help="refetch instead of using the store")
    parser.add_argument(
        "--min-level",
        default="",
        choices=["", "中", "高"],
        help="only show names at or above this risk level",
    )
    return parser


def _names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _symbols(args: Any, root: Path, provider: str, adjust: str) -> list[str]:
    wanted = _names(args.symbols)
    if wanted:
        return wanted
    panel = load_panel_cache(root, provider, adjust)
    return [str(column) for column in panel.prices.columns]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    root = Path(args.data or settings.data.directory)
    provider = args.provider or settings.collection.provider
    adjust = args.adjust or settings.collection.adjust
    directory = Path(settings.fundamentals.output_directory)

    if provider != "baostock":
        # saying this out loud beats a confusing empty table: only one source
        # serves quarterly filings here, and it is not the default one
        logger.error(
            "财务数据目前只支持 baostock（当前 provider=%s）。"
            "用 --provider baostock 运行，或先 quant-collect --provider baostock 收数。",
            provider,
        )
        return 2

    try:
        symbols = _symbols(args, root, provider, adjust)
        if not symbols:
            logger.error("没有标的可评估：传 --symbols，或先收数生成行情缓存")
            return 1

        panel = load(directory, provider)
        known = (
            set(panel.frame.index.get_level_values("symbol")) if not panel.frame.empty else set()
        )
        # fetch the names we do not hold yet, plus all of them when asked to
        # refresh: a store that already has three names must not silently skip
        # the fourth just because it is no longer empty
        missing = [symbol for symbol in symbols if symbol not in known]
        wanted = symbols if args.refresh else missing
        if wanted:
            quarters = args.quarters or settings.fundamentals.quarters
            logger.info("拉取 %d 只标的最近 %d 个季度的财报", len(wanted), quarters)
            fresh = collect(
                wanted,
                quarters=quarters,
                pause=settings.fundamentals.pause_seconds,
            )
            panel = merge(panel, fresh)
            target = store(panel, directory)
            logger.info("已写入 %s（%d 行）", target, len(panel.frame))
        if panel.frame.empty:
            logger.error("没有拿到任何财报数据，可能是标的代码不对或数据源没有披露")
            return 1

        risks = assess_many(panel, symbols, as_of=args.as_of)
        if args.min_level:
            floor = _ORDER[args.min_level]
            risks = [risk for risk in risks if _ORDER.get(risk.level, 0) >= floor]

        _print(risks, panel, as_of=args.as_of)
        return 0
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("财报评估失败：%s: %s", type(exc).__name__, exc)
        return 1


def _number(value: float | None, scale: float = 1.0, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value * scale:.{digits}f}"


def _width(text: str) -> int:
    """How many terminal columns a string occupies.

    A CJK character takes two, so ``f"{'负债率':>9}"`` yields a column that
    visibly drifts against the digits printed beneath it.
    """
    return sum(2 if unicodedata.east_asian_width(character) in "WF" else 1 for character in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = max(0, width - _width(text))
    return " " * gap + text if right else text + " " * gap


#: (title, width in terminal columns, right-aligned)
_COLUMNS: tuple[tuple[str, int, bool], ...] = (
    ("symbol", 11, False),
    ("level", 6, False),
    ("score", 6, True),
    ("负债率", 9, True),
    ("流动比", 8, True),
    ("现金/利润", 11, True),
    ("净利同比", 10, True),
)


def _row(cells: list[str]) -> str:
    return "".join(
        _pad(text, width, right) for text, (_, width, right) in zip(cells, _COLUMNS, strict=True)
    )


def _print(risks: list[Any], panel: Any, as_of: str | None = None) -> None:
    if not risks:
        print("没有符合条件的结果")
        return

    periods: dict[str, str] = {}
    for risk in risks:
        if risk.snapshot is not None:
            periods[risk.snapshot.period] = risk.snapshot.published
    latest = max(periods) if periods else "—"
    dates = sorted(set(periods.values()))
    if not dates:
        shape = "没有财报"
    elif len(dates) == 1:
        shape = f"最近一期 {latest}（披露 {dates[0]}）"
    else:
        # names do not file on the same day; one date here would be a lie by
        # omission, the 31st says nothing about the one that filed on the 24th
        shape = f"最近一期 {latest}（披露 {dates[0]} ~ {dates[-1]}）"
    print(f"财报风险 · {len(risks)} 只 · {shape}")
    if as_of:
        print(f"按 {as_of} 当日已披露的口径评估：之后发布的财报未计入")
    print()

    print(_row([title for title, _, _ in _COLUMNS]) + "  原因")
    for risk in sorted(risks, key=lambda item: (-item.score, item.symbol)):
        snapshot = risk.snapshot
        if snapshot is None:
            print(_row([risk.symbol, risk.level, "—", "—", "—", "—", "—"]) + "  无财报")
            continue
        cells = [
            risk.symbol,
            risk.level,
            f"{risk.score:.0f}",
            _number(snapshot.liability_to_asset, 100, 1) + "%",
            _number(snapshot.current_ratio),
            _number(snapshot.cfo_to_profit),
            _number(snapshot.net_profit_yoy, 100, 1) + "%",
        ]
        print(_row(cells) + f"  {risk.reason}")
    print()

    high = sum(1 for risk in risks if risk.level == LEVEL_HIGH)
    medium = sum(1 for risk in risks if risk.level == LEVEL_MEDIUM)
    unknown = sum(1 for risk in risks if risk.level == LEVEL_UNKNOWN)
    print(f"高风险 {high} 只 · 中风险 {medium} 只 · 无财报 {unknown} 只")
    print()
    print("读法：")
    print("  红旗阈值按非金融工业股标定。毛利率缺失且负债率 > 85% 的按金融口径处理——")
    print("  存款是银行的原料而不是风险，但它仍然背着 91% 的负债，这一点值得知道。")
    print("  「未知」是没有财报，不是没有风险；拿不到数据时不要把它当成通过。")
    print("  这是一张排查清单，不是投资结论：被标红的公司可能正在好转，没被标红的也可能刚出事。")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
