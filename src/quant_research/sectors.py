"""板块分析：板块内部到底发生了什么。

``/sectors/movers`` 回答「哪些板块在动」；这里回答「动的是谁、里面个股什么
样」——板块涨 3%，是三五只权重在拉，还是二十只一起涨？这决定了「买板块」
还是「挑个股」哪个更划算。

三个层次：

1. **板块汇总**：等权 N 日涨幅、上涨占比、中位涨幅、内部分化（成分股涨幅的
   标准差）、当日成交额合计，以及板块内领涨/领跌各前几只；
2. **成分股明细**：每只票的当日/N 日涨幅、相对板块的超额、RSI、是否站上
   MA20、量比、成交额；
3. **分化度**：标准差高 = 板块内部差异大，选股比选板块重要；标准差低 =
   齐涨齐跌，板块本身更有意义。

克制：不知道新闻就不猜驱动，只描述结构；样本是已落盘的票，不是全市场，
输出里的 ``n_symbols`` 如实标注，别拿 30 只的「板块涨幅」当行业指数。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from quant_research.indicators import rsi, sma

__all__ = ["Member", "SectorRow", "analyze", "members"]


def _num(value: Any) -> float | None:
    """NaN / None → None；其余四舍五入到两位。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, 2)


@dataclass(frozen=True)
class Member:
    """One stock inside a sector, as of the last bar."""

    symbol: str
    name: str
    close: float | None
    change_pct: float | None
    change_nd: float | None
    excess_nd: float | None
    rsi_14: float | None
    above_ma20: bool | None
    volume_ratio: float | None
    amount: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "close": self.close,
            "change_pct": self.change_pct,
            "change_nd": self.change_nd,
            "excess_nd": self.excess_nd,
            "rsi_14": self.rsi_14,
            "above_ma20": self.above_ma20,
            "volume_ratio": self.volume_ratio,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class SectorRow:
    sector: str
    n: int
    change_1d: float | None
    change_nd: float | None
    up_ratio: float | None
    median_change_nd: float | None
    dispersion: float | None
    amount: float | None
    leaders: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    laggards: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "n": self.n,
            "change_1d": self.change_1d,
            "change_nd": self.change_nd,
            "up_ratio": self.up_ratio,
            "median_change_nd": self.median_change_nd,
            "dispersion": self.dispersion,
            "amount": self.amount,
            "leaders": list(self.leaders),
            "laggards": list(self.laggards),
        }


def _table(
    panel: Any,
    sector_map: dict[str, str],
    names: dict[str, str] | None,
    days: int,
) -> pd.DataFrame:
    """One row per traded symbol that has a sector: prices, changes, amount."""
    close = panel.prices
    if len(close) < days + 1:
        raise ValueError(f"面板不足 {days + 1} 个交易日，算不了 {days} 日板块涨幅")
    frame = pd.DataFrame(
        {
            "sector": pd.Series(sector_map),
            "name": pd.Series(names or {}),
            "close": close.iloc[-1],
            "change_1d": (close.iloc[-1] / close.iloc[-2] - 1) * 100,
            "change_nd": (close.iloc[-1] / close.iloc[-1 - days] - 1) * 100,
        }
    )
    if panel.has_field("amount"):
        frame["amount"] = panel.field("amount").iloc[-1]
    else:
        frame["amount"] = float("nan")
    frame = frame.dropna(subset=["sector", "close"])
    return frame[frame["sector"].astype(str).str.len() > 0]


def analyze(
    panel: Any,
    sector_map: dict[str, str],
    names: dict[str, str] | None = None,
    days: int = 5,
    top: int = 3,
) -> list[SectorRow]:
    """Per-sector summary, sorted by N-day change (leaders first)."""
    table = _table(panel, sector_map, names, days)
    rows: list[SectorRow] = []
    for sector, chunk in table.groupby("sector"):
        ordered = chunk.sort_values("change_nd", ascending=False)
        moves = chunk["change_nd"].dropna()
        leaders = tuple(
            {
                "symbol": symbol,
                "name": str(record.get("name") or ""),
                "change_nd": _num(record["change_nd"]),
            }
            for symbol, record in ordered.head(top).iterrows()
        )
        laggards = tuple(
            {
                "symbol": symbol,
                "name": str(record.get("name") or ""),
                "change_nd": _num(record["change_nd"]),
            }
            for symbol, record in ordered.tail(top).iloc[::-1].iterrows()
            if len(ordered) > top  # 板块不足两倍 top 时，领跌会和领涨重复
        )
        rows.append(
            SectorRow(
                sector=str(sector),
                n=len(chunk),
                change_1d=_num(chunk["change_1d"].mean()),
                change_nd=_num(moves.mean()) if len(moves) else None,
                up_ratio=_num((moves > 0).mean() * 100) if len(moves) else None,
                median_change_nd=_num(moves.median()) if len(moves) else None,
                dispersion=_num(moves.std()) if len(moves) > 1 else None,
                amount=_num(chunk["amount"].sum(min_count=1) / 1e8),
                leaders=leaders,
                laggards=laggards,
            )
        )
    rows.sort(key=lambda row: row.change_nd if row.change_nd is not None else -1e9, reverse=True)
    return rows


def members(
    panel: Any,
    sector_map: dict[str, str],
    sector: str,
    names: dict[str, str] | None = None,
    days: int = 5,
) -> list[Member]:
    """Every stock in one sector, richest movers first, with relative strength.

    RSI/MA20 只对这一个板块的几十只算——全市场逐列算是浪费，这个端点的
    使用场景就是「点开一个板块」。
    """
    table = _table(panel, sector_map, names, days)
    scoped = table[table["sector"].astype(str) == sector]
    if scoped.empty:
        return []
    sector_change = scoped["change_nd"].mean()
    close = panel.prices
    volume = panel.field("volume") if panel.has_field("volume") else None
    rows: list[Member] = []
    for symbol, record in scoped.sort_values("change_nd", ascending=False).iterrows():
        series = close[symbol].dropna()
        frame = series.to_frame(name=symbol)
        rsi_value = _num(rsi(frame).iloc[-1, 0]) if len(series) > 15 else None
        ma20 = _num(sma(frame, 20).iloc[-1, 0]) if len(series) >= 20 else None
        ratio = None
        if volume is not None:
            traded = volume[symbol].dropna()
            if len(traded) > 5:
                base = float(traded.iloc[-6:-1].mean())
                ratio = _num(float(traded.iloc[-1]) / base) if base else None
        rows.append(
            Member(
                symbol=symbol,
                name=str(record.get("name") or ""),
                close=_num(record["close"]),
                change_pct=_num(record["change_1d"]),
                change_nd=_num(record["change_nd"]),
                excess_nd=_num(record["change_nd"] - sector_change)
                if not math.isnan(record["change_nd"])
                else None,
                rsi_14=rsi_value,
                above_ma20=None if ma20 is None else bool(record["close"] > ma20),
                volume_ratio=ratio,
                amount=_num(record["amount"] / 1e8),
            )
        )
    return rows
