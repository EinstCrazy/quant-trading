"""Command line entry point for the symbol ↔ name table.

    quant-symbols --refresh             # 从数据源重拉全市场代码+名称（几十秒）
    quant-symbols --query 茅台          # 按名称或代码搜索
    quant-symbols --query 3007 --limit 5
    quant-symbols                       # 表的状态：多少只、什么时候刷新过

The web UI's code inputs complete from this table. Without it they still work —
they just complete nothing but the codes you already have cached, which is
precisely the part you did not need help typing.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from quant_research.config import load_settings
from quant_research.data import universe
from quant_research.data.providers import get_provider
from quant_research.logging_setup import get_logger

logger = get_logger("symbols")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-symbols",
        description="维护「股票代码 → 中文名称」表，供 Web 输入框补全与命令行查询。",
    )
    parser.add_argument("--refresh", action="store_true", help="从数据源重拉全市场代码与名称")
    parser.add_argument("--query", "-q", default="", help="按代码前缀或名称搜索，例如 茅台 / 6005")
    parser.add_argument("--limit", type=int, default=20, help="最多返回几条（默认 20）")
    parser.add_argument("--provider", help="数据源（默认取 settings.collection.provider）")
    parser.add_argument("--adjust", help="复权口径，仅用于构造数据源（默认 settings）")
    parser.add_argument("--data", help="数据根目录（默认 settings.data.directory）")
    return parser


def _print_status(frame, directory) -> None:
    if frame.empty:
        print(f"名称表尚未建立：{directory}")
        print("先运行：quant-symbols --refresh")
        return
    stamp = "—"
    if "updated_at" in frame.columns:
        # stored as UTC; show it on the reader's own clock, the one they will
        # compare against their shell history
        raw = str(frame["updated_at"].iloc[0])
        try:
            stamp = datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            stamp = raw[:16].replace("T", " ")
    named = int((frame["name"].astype(str).str.strip() != "").sum())
    print(f"名称表：{len(frame)} 只（{named} 只有中文名），更新于 {stamp}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    directory = args.data or settings.data.directory
    table = universe.load(directory)

    if args.refresh:
        provider_name = args.provider or settings.collection.provider
        adjust = args.adjust or settings.collection.adjust
        provider = get_provider(provider_name, adjust=adjust)
        logger.info("从 %s 拉取全市场代码与名称（akshare 约需数十秒）…", provider_name)
        try:
            count = universe.refresh(provider, directory)
        except Exception as exc:  # noqa: BLE001 - the message is the point
            logger.error("刷新失败：%s", exc)
            return 1
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"已刷新 {count} 只 → {universe.table_path(directory)}（{stamp}）")
        table = universe.load(directory)

    if args.query:
        hits = universe.search(table, args.query, limit=max(1, args.limit))
        if not hits:
            print(
                f"没有匹配「{args.query}」的标的"
                + ("" if not table.empty else "（名称表为空，先 --refresh）")
            )
            return 0
        for item in hits:
            print(f"  {item.symbol:<11}{item.name}")
        return 0

    if not args.refresh:
        _print_status(table, directory)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
