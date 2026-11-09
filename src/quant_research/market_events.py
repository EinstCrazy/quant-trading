"""最近大事件：从行情本身检测值得注意的日子。

项目没有接新闻源，「大事件」在这里只能诚实地定义为行情的异常——普涨普跌日、
板块集体异动、个股暴涨暴跌、创阶段新高/新低。这四类事件覆盖了「那天发生了
什么」的全部可验证内容：新闻可以事后解读，行情异动本身就是可操作的信号。

阈值用绝对百分比（A 股语境）而不是分位数：分位数自适应会让「3% 的普跌」在
高波动市场里每天都触发，事件列表就失去了「值得注意」的含义。绝对阈值简单、
可解释，跨市场不通用——但这个项目只有一个市场。

一只票持续创新高不该每天报一次：只报窗口内第一次突破，连续新高是趋势，
不是事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from quant_research.data.loader import PricePanel
from quant_research.logging_setup import get_logger

logger = get_logger("market_events")

__all__ = ["detect_events"]

# 阈值（百分比）：普跌/普涨的日中位、板块等权日涨跌、个股单日涨跌
MARKET_THRESHOLD = 2.0
SECTOR_THRESHOLD = 3.0
SECTOR_CONSENSUS = 0.65
STOCK_THRESHOLD = 7.0
LOOKBACK_HIGH_LOW = 60

_KIND_ORDER = {"market": 0, "sector": 1, "stock": 2, "high": 3, "low": 4}


@dataclass(frozen=True)
class Event:
    date: str
    kind: str
    title: str
    detail: str = ""
    symbols: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "symbols": list(self.symbols),
        }


def detect_events(
    panel: PricePanel,
    sector_map: dict[str, str] | None = None,
    *,
    days: int = 10,
) -> list[dict[str, Any]]:
    """Scan the last ``days`` sessions and return events, newest first.

    A missing sector table only removes the sector kind of events: market and
    stock anomalies do not need it, and the caller labels the gap.
    """
    close = panel.prices
    if len(close) < 2:
        return []
    changes = close.pct_change().dropna(how="all") * 100
    window = changes.tail(days)

    sector_map = sector_map or {}

    events: list[Event] = []
    for date, row in window.iterrows():
        day = date.strftime("%m-%d")
        valid = row.dropna()

        # 市场日：中位涨幅是全体样本的中心，个别票的暴涨暴跌掩盖不了它
        if not valid.empty and abs(valid.median()) >= MARKET_THRESHOLD:
            up, down = int((valid > 0).sum()), int((valid < 0).sum())
            events.append(
                Event(
                    date=day,
                    kind="market",
                    title=f"市场{'普涨' if valid.median() > 0 else '普跌'}：中位 {valid.median():+.1f}%",
                    detail=f"上涨 {up} · 下跌 {down}（样本 {len(valid)} 只）",
                )
            )

        # 板块日：等权涨幅显著且成分高度同向——单票拉动不叫板块异动
        if sector_map:
            grouped = pd.DataFrame(
                {"sector": valid.index.map(sector_map), "change": valid}
            ).dropna()
            for sector, chunk in grouped.groupby("sector"):
                if len(chunk) < 2:
                    continue
                mean = chunk["change"].mean()
                share = (chunk["change"] * (1 if mean > 0 else -1) > 0).mean()
                if abs(mean) >= SECTOR_THRESHOLD and share >= SECTOR_CONSENSUS:
                    events.append(
                        Event(
                            date=day,
                            kind="sector",
                            title=f"{sector} 集体{'上涨' if mean > 0 else '下跌'} {mean:+.1f}%",
                            detail=f"{round(share * len(chunk))}/{len(chunk)} 只同向",
                            symbols=tuple(chunk.index[chunk["change"].abs() > 0]),
                        )
                    )

        # 个股日：单票的大幅波动自身就是事件，不必等到板块或市场层面
        movers = valid[valid.abs() >= STOCK_THRESHOLD]
        for symbol, change in movers.sort_values(key=abs, ascending=False).items():
            events.append(
                Event(
                    date=day,
                    kind="stock",
                    title=f"{symbol} 单日 {change:+.1f}%",
                    symbols=(symbol,),
                )
            )

    # 新高/新低：逐票找窗口内第一次突破；连续新高是趋势，不是事件
    reported: set[tuple[str, str]] = set()
    for symbol in close.columns:
        series = close[symbol].dropna()
        if len(series) < LOOKBACK_HIGH_LOW // 2:
            continue
        for date in [d for d in window.index if d in series.index]:
            past = series.loc[:date].iloc[:-1].tail(LOOKBACK_HIGH_LOW)
            if len(past) < LOOKBACK_HIGH_LOW // 2:
                continue
            value = series.loc[date]
            if value >= past.max():
                kind, word = "high", "新高"
            elif value <= past.min():
                kind, word = "low", "新低"
            else:
                continue
            if (symbol, kind) not in reported:
                reported.add((symbol, kind))
                events.append(
                    Event(
                        date=date.strftime("%m-%d"),
                        kind=kind,
                        title=f"{symbol} 创 {LOOKBACK_HIGH_LOW} 日{word}",
                    )
                )

    events.sort(key=lambda event: (event.date, _KIND_ORDER.get(event.kind, 9)))
    recent = events[-80:]
    recent.reverse()  # 最新在前：事件列表是倒着读的报纸
    return [event.to_dict() for event in recent]
