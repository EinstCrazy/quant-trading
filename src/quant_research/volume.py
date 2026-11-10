"""成交量分析：大盘、板块、个股三层量能，以及量价组合的含义。

成交量本身没有方向——放量可以是资金流入，也可以是有人借量派发。所以这里
不做「放量=看涨」这种单边判断，只给**量价组合的四象限**和每个象限的常见
含义，把「该盯什么」落成可核对的数字：

- 放量上涨：跟随资金在，但次日最怕量能突然缩回 1 倍以下；
- 缩量上涨：涨得动但没人跟，持续性存疑；
- 放量下跌：有资金在离场，别在量能峰值处抄底；
- 缩量下跌：抛压在衰减，但承接同样不足，等量能重新放大。

三层嵌套的意义：大盘量能决定「今天是不是干活的天气」，板块量能回答「钱在
往哪儿去」，个股量能回答「这只票的量是跟风还是自己的事」。只看个股量比，
在普涨普跌日会被大盘量能整体抬高或压低——所以每一层都和自己的 20 日均量
比，而不是和绝对成交额比。

样本诚实：这里的「大盘」是已落盘样本的合计成交额，不是交易所全市场成交额。
样本小时（比如 30 只蓝筹），它反映的是这些票的资金活跃度，不是市场总量。
``total_symbols`` 如实标注。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

__all__ = [
    "MarketVolume",
    "SectorVolume",
    "StockVolume",
    "VolumeRules",
    "market",
    "market_from_index",
    "sector",
    "stock",
]

# 量能比值的经验阈值。放量 1.2 / 缩量 0.8 是常见口径；2.5 倍以上算巨量，
# 0.5 倍以下算地量——这两个不是「好/坏」，是「不寻常」。
DEFAULT_SURGE = 1.2
DEFAULT_SHRINK = 0.8
DEFAULT_HUGE = 2.5
DEFAULT_LOW = 0.5
TREND_SHORT = 5
TREND_LONG = 20


@dataclass(frozen=True)
class VolumeRules:
    surge: float = DEFAULT_SURGE
    shrink: float = DEFAULT_SHRINK
    huge: float = DEFAULT_HUGE
    low: float = DEFAULT_LOW
    trend_short: int = TREND_SHORT
    trend_long: int = TREND_LONG

    def __post_init__(self) -> None:
        if not 0 < self.shrink < self.surge:
            raise ValueError("缩量线必须小于放量线，且都为正")
        if self.huge <= self.surge or self.low >= self.shrink:
            raise ValueError("巨量线要高于放量线，地量线要低于缩量线")
        if not 2 <= self.trend_short < self.trend_long:
            raise ValueError("量能趋势窗口必须满足 2 <= 短窗口 < 长窗口")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else round(number, 2)


def _series(panel: Any) -> tuple[pd.Series, str]:
    """Total traded amount per day across the panel, plus its unit.

    prefers amount (yuan) because comparing volumes across stocks only makes
    sense in money; falls back to volume (lots) when a source has none.
    """
    if panel.has_field("amount"):
        return panel.field("amount").sum(axis=1), "amount"
    if panel.has_field("volume"):
        return panel.field("volume").sum(axis=1), "volume"
    raise ValueError("面板里没有成交额也没有成交量，算不了量能分析")


def _scale(unit: str) -> float:
    """Raw unit → display unit: 亿 for money, 万手 for lots."""
    return 1e8 if unit == "amount" else 1e4


def _window_stats(series: pd.Series, rules: VolumeRules) -> dict[str, Any]:
    """Today vs the trailing mean — the same convention as ``volume_ratio``.

    ``shift(1)`` is deliberate: today's spike must not flatter its own baseline.
    """
    if len(series) < rules.trend_long + 1:
        raise ValueError(
            f"面板不足 {rules.trend_long + 1} 个交易日，算不了 {rules.trend_long} 日均量"
        )
    today = float(series.iloc[-1])
    base_long = float(series.iloc[-(rules.trend_long + 1) : -1].mean())
    base_short = float(series.iloc[-(rules.trend_short + 1) : -1].mean())
    ratio = today / base_long if base_long else None
    trend = base_short / base_long if base_long else None
    return {
        "today": today,
        "base_long": base_long,
        "base_short": base_short,
        "ratio": ratio,
        "trend": trend,
    }


def _level(ratio: float | None, rules: VolumeRules) -> str:
    if ratio is None:
        return "未知"
    if ratio >= rules.huge:
        return "巨量"
    if ratio >= rules.surge:
        return "放量"
    if ratio <= rules.low:
        return "地量"
    if ratio <= rules.shrink:
        return "缩量"
    return "平量"


def _trend_label(trend: float | None, rules: VolumeRules) -> str | None:
    """Short window vs long window: is volume building up or fading out?"""
    if trend is None:
        return None
    if trend >= rules.surge:
        return "近 5 日在放量"
    if trend <= rules.shrink:
        return "近 5 日在缩量"
    return "近 5 日量能平稳"


def _streak(series: pd.Series, rules: VolumeRules) -> int:
    """Consecutive days whose volume exceeded the prior 20-day mean."""
    if len(series) < 2:
        return 0
    count = 0
    for index in range(len(series) - 1, 0, -1):
        window = series.iloc[max(0, index - rules.trend_long) : index]
        if len(window) < rules.trend_long or not float(window.mean()):
            break
        if float(series.iloc[index]) > float(window.mean()) * rules.surge:
            count += 1
        else:
            break
    return count


def _quadrant(change: float | None, ratio: float | None, rules: VolumeRules) -> str:
    """The four classic price-volume combinations; flat volume is its own case."""
    if change is None or ratio is None:
        return "数据不足"
    rising = change > 0.1
    falling = change < -0.1
    if ratio >= rules.surge:
        if rising:
            return "放量上涨"
        return "放量下跌" if falling else "放量滞涨"
    if ratio <= rules.shrink:
        if rising:
            return "缩量上涨"
        return "缩量下跌" if falling else "缩量整理"
    return "平量上涨" if rising else ("平量下跌" if falling else "平量整理")


def _fmt_ratio(ratio: float | None) -> str:
    return "—" if ratio is None else f"{ratio:.2f} 倍"


def _recent_series(
    series: pd.Series, scale: float, rules: VolumeRules
) -> tuple[dict[str, Any], ...]:
    """Last ten days, each with the ratio its own trailing 20-day mean."""
    rows: list[dict[str, Any]] = []
    for index in range(max(0, len(series) - 10), len(series)):
        window = series.iloc[max(0, index - rules.trend_long) : index]
        base = float(window.mean()) if len(window) else 0.0
        value = float(series.iloc[index])
        rows.append(
            {
                "date": str(series.index[index].date()),
                "amount": _num(value / scale),
                "ratio": _num(value / base) if base else None,
            }
        )
    return tuple(rows)


# --- 大盘 ----------------------------------------------------------------------


@dataclass(frozen=True)
class MarketVolume:
    as_of: str
    unit: str
    amount: float
    base_long: float
    base_short: float
    ratio: float | None
    trend: float | None
    change_pct: float | None
    level: str
    quadrant: str
    streak: int
    advice: str
    series: tuple[dict[str, Any], ...]
    n_symbols: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "unit": self.unit,
            "amount": self.amount,
            "base_long": self.base_long,
            "base_short": self.base_short,
            "ratio": self.ratio,
            "trend": self.trend,
            "change_pct": self.change_pct,
            "level": self.level,
            "quadrant": self.quadrant,
            "streak": self.streak,
            "advice": self.advice,
            "series": list(self.series),
            "n_symbols": self.n_symbols,
        }


def _market_advice(quadrant: str, ratio: float | None, streak: int, rules: VolumeRules) -> str:
    r = _fmt_ratio(ratio)
    if quadrant == "放量上涨":
        tail = f"已经连续 {streak} 天放量，量能维持是趋势延续的前提。" if streak >= 2 else ""
        return (
            f"总量 {r}（20 日均量的倍数）伴随上涨：跟随资金在，赚钱效应在扩散。"
            f"次日最怕量能突然缩回 1 倍以下——放量上涨的持续性取决于跟随资金，别只看涨幅。{tail}"
        )
    if quadrant == "缩量上涨":
        return (
            f"总量只有 {r}：涨得动但没人跟。无量上涨的持续性存疑，"
            "这个位置不适合加仓；等量能重新放大到 1.2 倍以上再看。"
        )
    if quadrant == "放量下跌":
        return (
            f"总量 {r} 伴随下跌：有资金在离场。别在放量下跌里抄底——"
            "等量能萎缩（回到 0.8 倍以下）之后的止跌，才算抛压衰竭。"
        )
    if quadrant == "缩量下跌":
        return (
            f"总量 {r}：抛压在衰减，但承接同样不足。缩量下跌不是买入信号，"
            "只是「跌不动」；等量能重新放大且价格不再新低，才说明有人接。"
        )
    if quadrant == "放量滞涨":
        return (
            f"总量 {r} 却涨不动：有人在借这个量能派发。放量滞涨比放量下跌更值得警惕，"
            "因为它看起来不难看。"
        )
    if quadrant == "缩量整理":
        return f"总量 {r}，缩量整理：变盘前的安静，方向没出来之前不动。"
    return f"总量 {r}，与 {rules.trend_long} 日均量基本持平：平常的一天，按原计划走。"


def market(panel: Any, rules: VolumeRules | None = None) -> MarketVolume:
    """样本合计路径：把面板聚合成单日成交额序列，再走与指数相同的分析。"""
    rules = rules or VolumeRules()
    series, unit = _series(panel)
    close = panel.prices
    if len(close) < 2:
        raise ValueError("面板不足两天，算不了当日涨跌")
    return _market_from_series(
        series, close.iloc[-1] / close.iloc[-2] - 1, rules, unit, int(close.shape[1])
    )


def _market_from_series(
    series: pd.Series,
    day_change: pd.Series,
    rules: VolumeRules,
    unit: str,
    n_symbols: int,
) -> MarketVolume:
    """指数路径与样本路径共用的分析主体。

    ``day_change`` 是当日涨跌幅比例（close/prev - 1）——指数路径传单元素
    序列，样本路径传逐票序列取均值。
    """
    stats = _window_stats(series, rules)
    change = float(day_change.mean() * 100)
    ratio = stats["ratio"]
    quadrant = _quadrant(change, ratio, rules)
    scale = _scale(unit)
    recent = _recent_series(series, scale, rules)
    return MarketVolume(
        as_of=str(series.index[-1].date()),
        unit=unit,
        amount=_num(stats["today"] / scale) or 0.0,
        base_long=_num(stats["base_long"] / scale) or 0.0,
        base_short=_num(stats["base_short"] / scale) or 0.0,
        ratio=_num(ratio),
        trend=_num(stats["trend"]),
        change_pct=_num(change),
        level=_level(ratio, rules),
        quadrant=quadrant,
        streak=_streak(series, rules),
        advice=_market_advice(quadrant, ratio, _streak(series, rules), rules),
        series=recent,
        n_symbols=n_symbols,
    )


def market_from_index(frame: pd.DataFrame, rules: VolumeRules | None = None) -> MarketVolume:
    """真实指数路径：指数日线的成交额与收盘，替代样本合计。

    ``frame`` 需要 close 与 amount 两列（见 data.index）。指数没有「逐票
    均值」，涨跌就是指数自身的涨跌，n_symbols 恒为 1——它不是样本，如实标注。
    """
    rules = rules or VolumeRules()
    if len(frame) < 2:
        raise ValueError("指数日线不足两天，算不了当日涨跌")
    return _market_from_series(
        frame["amount"].astype(float),
        frame["close"].astype(float).iloc[-1] / frame["close"].astype(float).iloc[-2] - 1,
        rules,
        "amount",
        1,
    )


# --- 板块 ----------------------------------------------------------------------


@dataclass(frozen=True)
class SectorVolume:
    sector: str
    n: int
    amount: float
    ratio: float | None
    share_pct: float | None
    change_pct: float | None
    up_count: int
    level: str
    quadrant: str
    trend: float | None
    advice: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sector": self.sector,
            "n": self.n,
            "amount": self.amount,
            "ratio": self.ratio,
            "share_pct": self.share_pct,
            "change_pct": self.change_pct,
            "up_count": self.up_count,
            "level": self.level,
            "quadrant": self.quadrant,
            "trend": self.trend,
            "advice": self.advice,
        }


def _sector_advice(
    quadrant: str, ratio: float | None, up: int, n: int, share: float | None, rules: VolumeRules
) -> str:
    r = _fmt_ratio(ratio)
    if quadrant == "放量上涨":
        base = f"放量上涨（{r}，{up}/{n} 只上涨）：资金在流入这个板块。看领涨股的量能能否维持，而不是看涨幅排名。"
    elif quadrant == "放量下跌":
        base = f"放量下跌（{r}）：有资金在出。先减到能承受的部分，别在这个位置补仓摊薄。"
    elif quadrant == "缩量上涨":
        base = f"缩量上涨（{r}）：没有增量资金，涨的是存量博弈，轮动随时可能中断。"
    elif quadrant == "缩量下跌":
        base = f"缩量调整（{r}）：抛压不重，但也没有人急着买。等量能重新放大再判断方向。"
    elif quadrant == "放量滞涨":
        base = f"放量滞涨（{r}）：量能上来了价格却没动，板块内部分歧在加大。"
    else:
        base = f"量能平稳（{r}），{up}/{n} 只上涨：没有增量信息，按原计划处理。"
    if share is not None and share >= 40:
        base += f"它占了全样本成交额的 {share:.0f}%——量能高度集中，其他板块的上涨更像补涨。"
    return base


def sector(
    panel: Any,
    sector_map: dict[str, str],
    names: dict[str, str] | None = None,
    rules: VolumeRules | None = None,
) -> list[SectorVolume]:
    rules = rules or VolumeRules()
    raw, unit = _series(panel)
    scale = _scale(unit)
    source = panel.field("amount") if panel.has_field("amount") else panel.field("volume")
    total_today = float(raw.iloc[-1])
    close = panel.prices
    change_1d = (close.iloc[-1] / close.iloc[-2] - 1) * 100

    rows: list[SectorVolume] = []
    for name in sorted(set(sector_map.values())):
        members = [
            symbol
            for symbol, value in sector_map.items()
            if value == name and symbol in source.columns
        ]
        if not members:
            continue
        own = source[members].sum(axis=1)
        try:
            stats = _window_stats(own, rules)
        except ValueError:
            continue
        moves = change_1d[members].dropna()
        change = _num(float(moves.mean())) if len(moves) else None
        up = int((moves > 0).sum())
        ratio = stats["ratio"]
        share = _num(stats["today"] / total_today * 100) if total_today else None
        rows.append(
            SectorVolume(
                sector=name,
                n=len(members),
                amount=_num(stats["today"] / scale) or 0.0,
                ratio=_num(ratio),
                share_pct=share,
                change_pct=change,
                up_count=up,
                level=_level(ratio, rules),
                quadrant=_quadrant(change, ratio, rules),
                trend=_num(stats["trend"]),
                advice=_sector_advice(
                    _quadrant(change, ratio, rules), ratio, up, len(members), share, rules
                ),
            )
        )
    rows.sort(key=lambda row: row.ratio if row.ratio is not None else -1, reverse=True)
    return rows


# --- 个股 ----------------------------------------------------------------------


@dataclass(frozen=True)
class StockVolume:
    symbol: str
    name: str
    sector: str | None
    close: float | None
    change_pct: float | None
    amount: float
    ratio: float | None
    level: str
    quadrant: str
    trend: float | None
    advice: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "close": self.close,
            "change_pct": self.change_pct,
            "amount": self.amount,
            "ratio": self.ratio,
            "level": self.level,
            "quadrant": self.quadrant,
            "trend": self.trend,
            "advice": self.advice,
        }


def _stock_advice(
    quadrant: str,
    ratio: float | None,
    level: str,
    change: float | None,
    trend: float | None,
    rules: VolumeRules,
) -> str:
    r = _fmt_ratio(ratio)
    if level == "巨量":
        direction = "收在高位是换手接力" if (change or 0) > 0 else "收在低位更像派发"
        return f"巨量 {r}：分歧极大，{direction}。看明天量能能否维持——巨量之后通常不是平静。"
    if level == "地量":
        return f"地量（{r}）：变盘前的安静。方向出来之前不动，别在缩无可缩时猜方向。"
    if quadrant == "放量上涨":
        base = f"放量上涨（{r}）：资金在买。止损可以上移，但不追高——量能峰值往往也是情绪峰值。"
    elif quadrant == "放量滞涨":
        base = f"放量 {r} 却几乎不涨：有人在借量派发，这比放量下跌更值得警惕。"
    elif quadrant == "放量下跌":
        base = f"放量下跌（{r}）：出货或恐慌。别在量能峰值处补仓，等缩量后的止跌。"
    elif quadrant == "缩量上涨":
        base = "缩量上涨：惜售但没人追，涨得轻但也不牢。别追高。"
    elif quadrant == "缩量下跌":
        base = "缩量下跌：抛压在衰竭，但承接不足。等放量止跌（放量且不创新低）才是信号。"
    else:
        base = f"量能平稳（{r}），没有额外的量能信息。"
    if trend is not None and trend >= rules.surge:
        base += "近 5 日量能持续高于 20 日均量，属于有资金持续关注的类型。"
    elif trend is not None and trend <= rules.shrink:
        base += "近 5 日量能低于 20 日均量，关注度在下降。"
    return base


def stock(
    panel: Any,
    symbols: list[str],
    sector_map: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
    rules: VolumeRules | None = None,
) -> list[StockVolume]:
    """Volume read for specific stocks — positions and watchlist first."""
    rules = rules or VolumeRules()
    sector_map = sector_map or {}
    names = names or {}
    close = panel.prices
    if panel.has_field("amount"):
        source, unit = panel.field("amount"), "amount"
    elif panel.has_field("volume"):
        source, unit = panel.field("volume"), "volume"
    else:
        raise ValueError("面板里没有成交额也没有成交量，算不了个股量能")
    scale = _scale(unit)
    rows: list[StockVolume] = []
    for symbol in symbols:
        if symbol not in close.columns or symbol not in source.columns:
            continue
        own = source[symbol].dropna()
        if len(own) < rules.trend_long + 1:
            continue
        stats = _window_stats(own, rules)
        price = close[symbol].dropna()
        change = (
            _num((float(price.iloc[-1]) / float(price.iloc[-2]) - 1) * 100)
            if len(price) > 1
            else None
        )
        ratio = stats["ratio"]
        level = _level(ratio, rules)
        quadrant = _quadrant(change, ratio, rules)
        rows.append(
            StockVolume(
                symbol=symbol,
                name=names.get(symbol, ""),
                sector=sector_map.get(symbol),
                close=_num(float(price.iloc[-1])),
                change_pct=change,
                amount=_num(stats["today"] / scale) or 0.0,
                ratio=_num(ratio),
                level=level,
                quadrant=quadrant,
                trend=_num(stats["trend"]),
                advice=_stock_advice(quadrant, ratio, level, change, stats["trend"], rules),
            )
        )
    rows.sort(key=lambda row: row.ratio if row.ratio is not None else -1, reverse=True)
    return rows
