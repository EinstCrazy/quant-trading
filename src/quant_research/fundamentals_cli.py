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
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.config import load_settings
from quant_research.data.cache import load_panel_cache
from quant_research.fundamentals import assess_many, collect, load, merge, regulatory, store
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
        "--no-scan",
        action="store_true",
        help="skip the announcement scan; use only what is already stored",
    )
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

        investigated = _regulatory(args, settings)
        _print(risks, panel, as_of=args.as_of, investigated=investigated)
        return 0
    except (ValueError, FileNotFoundError, KeyError) as exc:
        logger.error("财报评估失败：%s: %s", type(exc).__name__, exc)
        return 1


def _regulatory(args: Any, settings: Any) -> dict[str, Any]:
    """Anyone under investigation, from the store, the manual list, and a scan.

    The scan is incremental by design: a day once read is never read again, so
    the steady-state cost is one request. The manual list covers what a window
    cannot — cases opened before the scan started and still open today.
    """
    directory = Path(settings.regulatory.output_directory)
    moment = pd.Timestamp(args.as_of).date() if args.as_of else date.today()

    if args.no_scan:
        logger.info("跳过公告扫描（--no-scan），只用已存事件")
    else:
        covered = regulatory.load_state(directory)
        days = regulatory.recent_sessions(settings.regulatory.lookback_days, today=moment)
        pending = [day for day in days if day not in covered]
        if pending:
            logger.info(
                "扫描 %d 个交易日的公告（已覆盖 %d 天，只取新增）", len(pending), len(covered)
            )
            outcome = regulatory.scan(pending)
            regulatory.store(outcome.events, directory)
            # only the days that actually got read: a failed date stays out of
            # the state so the next run retries it instead of skipping it
            regulatory.save_state(outcome.scanned, directory)
            if outcome.events:
                logger.info("新增 %d 条监管事件", len(outcome.events))
            if outcome.failed:
                logger.warning(
                    "%d 个交易日取数失败，下次会重试：%s",
                    len(outcome.failed),
                    ", ".join(outcome.failed),
                )

    combined = regulatory.merge(
        regulatory.load(directory),
        regulatory.load_manual(settings.regulatory.manual_file),
    )
    return regulatory.active(combined, as_of=moment, within_days=settings.regulatory.active_days)


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
    ("监管", 7, False),
)


def _percent(value: float | None, scale: float = 100.0, digits: int = 1) -> str:
    """``—`` when missing, not ``—%``: a dash with a unit still claims a unit."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value * scale:.{digits}f}%"


def _row(cells: list[str]) -> str:
    # one space between columns: a right-aligned cell has no trailing space of
    # its own, so without this the last column butts against the next label
    return " ".join(
        _pad(text, width, right) for text, (_, width, right) in zip(cells, _COLUMNS, strict=True)
    )


def _print_regulatory(investigated: dict[str, Any], as_of: str | None = None) -> None:
    """The alert that matters most, printed before everything else.

    A ratio that looks wrong invites a second look. A 立案告知书 is not a
    judgement call: refinancing and restructuring stop, and the outcome is
    usually a restatement or a delisting. It belongs at the top, not in a column.
    """
    if not investigated:
        print("监管风险 · 未发现处于证监会调查中的标的")
        print()
        return

    print(f"⚠ 监管风险 · {len(investigated)} 只正在被证监会调查（策略默认排除）")
    print()
    for event in sorted(
        investigated.values(), key=lambda item: (item.date, item.symbol), reverse=True
    ):
        title = event.title if len(event.title) <= 44 else event.title[:43] + "…"
        print(f"  {event.symbol:<11}{event.date:<12}{_pad(event.name, 10)}{title}")
    print()
    print("  立案期间再融资、重组、发行股份购买资产都会被卡住，供应商与银行也会收紧；")
    print("  解除条件是有结案公告，不是股价反弹。名单来源：全市场公告扫描 + 手工登记。")
    if as_of:
        print(f"  按 {as_of} 当日已公告的口径：之后发布的立案未计入。")
    print()


def _print(
    risks: list[Any],
    panel: Any,
    as_of: str | None = None,
    investigated: dict[str, Any] | None = None,
) -> None:
    found = investigated or {}
    _print_regulatory(found, as_of)
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
        watch = "立案" if risk.symbol in found else "—"
        if snapshot is None:
            print(_row([risk.symbol, risk.level, "—", "—", "—", "—", "—", watch]) + "  无财报")
            continue
        cells = [
            risk.symbol,
            risk.level,
            f"{risk.score:.0f}",
            _percent(snapshot.liability_to_asset),
            _number(snapshot.current_ratio),
            _number(snapshot.cfo_to_profit),
            _percent(snapshot.net_profit_yoy),
            watch,
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
