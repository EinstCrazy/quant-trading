"""Technical indicators: wide frame in, wide frame out.

The formulas, their usual parameters and — more importantly — the ways they get
written slightly wrong are all in ``docs/INDICATORS.md``. This module is that
document in code, so the two must not drift apart.

Three rules hold for every function here:

1. **Only the past is visible.** Every value comes from ``rolling`` / ``ewm`` /
   ``cumsum``, so a value on day *t* never depends on day *t+1*. Truncating the
   history and recomputing gives byte-identical results for the overlapping days
   — that is the test that keeps it honest.
2. **``ewm`` always carries ``adjust=False`` and an explicit ``min_periods``.**
   The pandas default corrects for the warm-up, which is not what any trading
   platform does; and without ``min_periods`` the very first bar produces a
   "14-day RSI" computed from one price.
3. **Division by zero is decided, not inherited.** A one-sided run, a limit-up
   lock and a flat day all produce a zero denominator somewhere. Each has a
   documented answer below rather than an ``inf`` that silently spreads.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant_research.data.loader import DataMeta, PricePanel

__all__ = [
    "INDICATOR_CATALOG",
    "IndicatorBundle",
    "IndicatorDoc",
    "IndicatorParams",
    "adx",
    "atr",
    "bias",
    "boll",
    "catalog",
    "cci",
    "compute_all",
    "doc_for",
    "donchian",
    "ema",
    "family_of",
    "kdj",
    "load_bundle",
    "macd",
    "mfi",
    "obv",
    "panel_from_frames",
    "params_from_settings",
    "psy",
    "realized_volatility",
    "roc",
    "rsi",
    "sar",
    "sma",
    "store_bundle",
    "true_range",
    "volume_ratio",
    "vr",
    "vwap",
    "williams_r",
]

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class IndicatorParams:
    """Every knob, defaulted to what the market convention uses.

    Changing a period changes the key the indicator is stored under — ``rsi_14``
    becomes ``rsi_21`` — so a stored bundle can never be silently read back as
    if it had been computed with different settings.
    """

    ma_periods: tuple[int, ...] = (5, 10, 20, 60)
    ema_periods: tuple[int, ...] = (12, 26)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    boll_period: int = 20
    boll_std: float = 2.0
    kdj_period: int = 9
    kdj_smooth: int = 3
    atr_period: int = 14
    williams_period: int = 14
    cci_period: int = 14
    adx_period: int = 14
    volatility_period: int = 20
    bias_period: int = 20
    roc_period: int = 12
    psy_period: int = 12
    mfi_period: int = 14
    volume_ratio_period: int = 5
    donchian_period: int = 20
    vr_period: int = 26
    turnover_ma_period: int = 20

    def __post_init__(self) -> None:
        for name in (
            "macd_fast",
            "macd_slow",
            "macd_signal",
            "rsi_period",
            "boll_period",
            "kdj_period",
            "kdj_smooth",
            "atr_period",
            "williams_period",
            "cci_period",
            "adx_period",
            "volatility_period",
            "bias_period",
            "roc_period",
            "psy_period",
            "mfi_period",
            "volume_ratio_period",
            "donchian_period",
            "vr_period",
            "turnover_ma_period",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"indicators.{name} must be at least 1, got {value}")
        for name in ("ma_periods", "ema_periods"):
            for period in getattr(self, name):
                if period < 1:
                    raise ValueError(f"indicators.{name} entries must be at least 1, got {period}")
        if self.boll_std <= 0:
            raise ValueError(f"indicators.boll_std must be positive, got {self.boll_std}")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ma_periods"] = list(self.ma_periods)
        payload["ema_periods"] = list(self.ema_periods)
        return payload


# --- trend -----------------------------------------------------------------


def sma(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Simple moving average. ``NaN`` until the window is full."""
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Exponential moving average, recursive — the way every platform draws it.

    ``adjust=False`` is the whole point: pandas' default reweights the warm-up
    and gives different numbers for the first few dozen bars.
    """
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


@dataclass(frozen=True)
class MacdLines:
    """DIF, its signal line, and the histogram."""

    dif: pd.DataFrame
    dea: pd.DataFrame
    hist: pd.DataFrame


def macd(close: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> MacdLines:
    """MACD. The histogram is ``2 × (DIF - DEA)``, the domestic convention.

    Foreign references usually plot ``1 ×``. Either is defensible; what matters
    is that a threshold copied from somewhere else was written for one of them.
    """
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return MacdLines(dif=dif, dea=dea, hist=2 * (dif - dea))


# --- oscillators -----------------------------------------------------------


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder's RSI.

    Two zero-denominator cases, both decided here rather than left as ``inf``:
    no losses in the window is a run of gains, so 100; no gains *and* no losses
    is a flat, data-less window, so ``NaN`` — calling that 100 would invent a
    signal that is not there.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    alpha = 1 / n
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=n).mean()

    ratio = avg_gain / avg_loss.where(avg_loss > 0)
    rsi_ = (100 - 100 / (1 + ratio)).where(avg_loss > 0, 100.0)
    return rsi_.where((avg_gain > 0) | (avg_loss > 0))


@dataclass(frozen=True)
class KdjLines:
    k: pd.DataFrame
    d: pd.DataFrame
    j: pd.DataFrame


def kdj(
    high: pd.DataFrame,
    low: pd.DataFrame,
    close: pd.DataFrame,
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> KdjLines:
    """KDJ. Recursive, so this is the one indicator that loops.

    Two details that are easy to get wrong and hard to notice:

    - ``K`` and ``D`` start at **50**, not at the first RSV. Feeding ``ewm`` the
      series would start it at the first RSV instead.
    - The loop is over rows, and each step is vectorised across symbols. A loop
      over rows *and* columns would be the thing that takes minutes on a
      market-wide panel.
    """
    lowest = low.rolling(n, min_periods=n).min()
    highest = high.rolling(n, min_periods=n).max()
    span = highest - lowest
    # a limit-up lock makes high == low; there is no position within a zero range
    rsv = ((close - lowest) / span.where(span > 0)) * 100

    k_rows: list[pd.Series] = []
    d_rows: list[pd.Series] = []
    previous_k = pd.Series(50.0, index=close.columns)
    previous_d = pd.Series(50.0, index=close.columns)
    for _, row in rsv.iterrows():
        # a suspended day carries no RSV: hold the previous value rather than
        # letting NaN swallow the recursion
        today = row.where(row.notna(), previous_k)
        previous_k = previous_k * (m1 - 1) / m1 + today / m1
        previous_d = previous_d * (m2 - 1) / m2 + previous_k / m2
        k_rows.append(previous_k.copy())
        d_rows.append(previous_d.copy())

    k = pd.concat(k_rows, axis=1).T
    d = pd.concat(d_rows, axis=1).T
    k.index = d.index = close.index
    k.columns = d.columns = close.columns
    # the first n-1 bars have no RSV yet, so KDJ does not exist for them
    k.iloc[: n - 1] = np.nan
    d.iloc[: n - 1] = np.nan
    return KdjLines(k=k, d=d, j=3 * k - 2 * d)


def williams_r(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14
) -> pd.DataFrame:
    """Williams %R, on a -100 (lowest) to 0 (highest) scale."""
    highest = high.rolling(n, min_periods=n).max()
    lowest = low.rolling(n, min_periods=n).min()
    span = highest - lowest
    return ((highest - close) / span.where(span > 0)) * -100


def cci(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """CCI. The denominator is the **mean absolute deviation**, not the std dev."""
    typical = (high + low + close) / 3
    mean = typical.rolling(n, min_periods=n).mean()
    deviation = (typical - mean).abs().rolling(n, min_periods=n).mean()
    return (typical - mean) / (0.015 * deviation.where(deviation > 0))


# --- volatility ------------------------------------------------------------


def true_range(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """True range: the day's span, widened to include any gap from the close."""
    previous_close = close.shift(1)
    # stacked, then grouped by date: ``groupby(axis=1)`` is gone in pandas 2.x
    return (
        pd.concat([high - low, (high - previous_close).abs(), (low - previous_close).abs()])
        .groupby(level=0)
        .max()
    )


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Average true range, Wilder-smoothed. Wider gap-sensitive than a std dev."""
    return true_range(high, low, close).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def realized_volatility(
    close: pd.DataFrame, n: int = 20, trading_days: int = TRADING_DAYS_PER_YEAR
) -> pd.DataFrame:
    """Annualised realised volatility.

    ``ddof=1`` here, unlike Bollinger's: this *estimates* a population parameter,
    where the band *describes* the spread of these particular prices.
    """
    return close.pct_change().rolling(n, min_periods=n).std(ddof=1) * math.sqrt(trading_days)


@dataclass(frozen=True)
class BollBands:
    mid: pd.DataFrame
    upper: pd.DataFrame
    lower: pd.DataFrame
    width: pd.DataFrame


def boll(close: pd.DataFrame, n: int = 20, k: float = 2.0) -> BollBands:
    """Bollinger bands, on the population standard deviation (``ddof=0``).

    pandas defaults to ``ddof=1``, which is 2.6% wider at n=20 — enough to move
    every breakout later, and nothing in the output looks broken.
    """
    mid = close.rolling(n, min_periods=n).mean()
    spread = close.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * spread
    lower = mid - k * spread
    return BollBands(mid=mid, upper=upper, lower=lower, width=(upper - lower) / mid)


# --- volume ----------------------------------------------------------------


def obv(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """On-balance volume. Cumulative, so only its direction is meaningful."""
    direction = close.diff().fillna(0.0)
    signed = pd.DataFrame(
        np.sign(direction.to_numpy(dtype=float)), index=close.index, columns=close.columns
    )
    return (volume * signed).cumsum()


def vwap(amount: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """Volume-weighted average price.

    ``volume`` is stored in **lots** and ``amount`` in **yuan**, hence the 100.
    Getting it wrong shifts the level by 100x while leaving the shape perfectly
    plausible, which is what makes it the worst kind of bug.
    """
    shares = volume * 100
    return amount / shares.where(shares > 0)


def volume_ratio(volume: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """量比的日线近似：当日量 / 前 n 日均量。

    行情软件的量比按分钟量计算，日线数据只能用日量近似——数值在盘中小会
    偏高，收盘后两者一致。>1 放量，<1 缩量；倍数本身比阈值更有信息量。
    """
    return volume / volume.shift(1).rolling(n).mean()


def vr(close: pd.DataFrame, volume: pd.DataFrame, n: int = 26) -> pd.DataFrame:
    """成交量比率：上涨日量与下跌日量之比，平盘日的量对半分摊。

    量在价先：价格没动而 VR 抬升，说明有人在低位接货；反过来，价升量不升
    说明推动上涨的资金在退场。
    """
    diff = close.diff()
    flat = volume.where(diff == 0, 0.0)
    up = (volume.where(diff > 0, 0.0) + flat / 2).rolling(n).sum()
    down = (volume.where(diff < 0, 0.0) + flat / 2).rolling(n).sum()
    return (up / down.where(down > 0)) * 100


def mfi(
    high: pd.DataFrame,
    low: pd.DataFrame,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    n: int = 14,
) -> pd.DataFrame:
    """资金流量指标：带价格的 OBV，0–100。

    用典型价（H+L+C）/3 加权成交量，比纯收盘价更能反映资金进出。负流量
    合计为零时（罕见的持续一字板）返回 100 而不是无穷。
    """
    typical = (high + low + close) / 3
    flow = typical * volume
    positive = flow.where(typical > typical.shift(1), 0.0).rolling(n).sum()
    negative = flow.where(typical < typical.shift(1), 0.0).rolling(n).sum()
    ratio = positive / negative.where(negative > 0)
    result = 100 - 100 / (1 + ratio)
    # 负流量消失而正流量存在（持续一字板之类的极端）：分母没了，指标顶格，
    # 而不是变成 NaN 让下游把「封死涨停」当成「没有数据」
    return result.where(~((negative == 0) & (positive > 0)), 100.0)


# --- price relative --------------------------------------------------------


def bias(close: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """乖离率：(收盘价 − MA_n) / MA_n × 100。

    回答「价格偏离均线多远」：正值说明高于均线，负值低于。与 MA 不同，
    它是无量纲的百分比，跨价格水平的票可以放在一起比较。
    """
    average = sma(close, n)
    return (close - average) / average * 100


def roc(close: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """变动率：n 日前到现在的百分比变化，最朴素的动量。

    与 pct_change 相同的数，区别只在用途：作为指标列，它和 RSI、BIAS 一样
    进截面排名，回答「谁最近动得猛」，而不是逐日画线。
    """
    return (close / close.shift(n) - 1) * 100


def psy(close: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """心理线：过去 n 天里上涨天数的占比，0–100。

    只数天数不看幅度——这是它与 RSI 的全部区别：RSI 按涨跌幅度加权，
    PSY 按天数。>75 说明连涨居多但未必健康，<25 连跌居多但未必见底。
    """
    up = (close.diff() > 0).astype(float)
    return up.rolling(n, min_periods=n).mean() * 100


# --- channels --------------------------------------------------------------


@dataclass(frozen=True)
class DonchianBands:
    upper: pd.DataFrame
    lower: pd.DataFrame


def donchian(high: pd.DataFrame, low: pd.DataFrame, n: int = 20) -> DonchianBands:
    """唐奇安通道：前 n 日（不含当日）的最高与最低。

    不含当日是刻意的：突破判定的参照系必须是「别人到过的位置」，把当日算进
    参照系，创新高的日子自己和自己比较，永远不会突破。
    """
    return DonchianBands(
        upper=high.shift(1).rolling(n, min_periods=n).max(),
        lower=low.shift(1).rolling(n, min_periods=n).min(),
    )


def sar(
    high: pd.DataFrame,
    low: pd.DataFrame,
    af_step: float = 0.02,
    af_max: float = 0.2,
) -> pd.DataFrame:
    """抛物线 SAR（Wilder 递推，逐列）。

    反转日 SAR 取前一段极值点（经典定义）；加速度因子随新高/新低累加到
    ``af_max`` 封顶。递推无法向量化，逐列循环——几十毫秒，不值得为它
    牺牲正确性。
    """
    out = pd.DataFrame(np.nan, index=high.index, columns=high.columns)
    for column in high.columns:
        highs, lows = high[column], low[column]
        count = len(highs)
        if count < 2:
            continue
        values = np.full(count, np.nan)
        trend_up = bool(highs.iloc[0] >= lows.iloc[0])
        sar_value = float(lows.iloc[0] if trend_up else highs.iloc[0])
        extreme = float(highs.iloc[0] if trend_up else lows.iloc[0])
        af = af_step
        for i in range(1, count):
            sar_value = sar_value + af * (extreme - sar_value)
            if trend_up:
                if lows.iloc[i] < sar_value:  # 跌破停损点：反转向下
                    trend_up = False
                    sar_value = extreme
                    extreme, af = float(lows.iloc[i]), af_step
                elif highs.iloc[i] > extreme:
                    extreme, af = float(highs.iloc[i]), min(af + af_step, af_max)
            elif highs.iloc[i] > sar_value:  # 升破停损点：反转向上
                trend_up = True
                sar_value = extreme
                extreme, af = float(highs.iloc[i]), af_step
            elif lows.iloc[i] < extreme:
                extreme, af = float(lows.iloc[i]), min(af + af_step, af_max)
            values[i] = sar_value
        out[column] = values
    return out


# --- trend strength --------------------------------------------------------


@dataclass(frozen=True)
class AdxLines:
    plus_di: pd.DataFrame
    minus_di: pd.DataFrame
    adx: pd.DataFrame


def adx(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> AdxLines:
    """Directional movement. ADX gives the strength of a trend, not its sign."""
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.DataFrame(
        np.where((up > down) & (up > 0), up.to_numpy(dtype=float), 0.0),
        index=high.index,
        columns=high.columns,
    )
    minus_dm = pd.DataFrame(
        np.where((down > up) & (down > 0), down.to_numpy(dtype=float), 0.0),
        index=high.index,
        columns=high.columns,
    )

    alpha = 1 / n
    smoothing = {"alpha": alpha, "adjust": False, "min_periods": n}
    average_range = true_range(high, low, close).ewm(**smoothing).mean()
    plus_di = 100 * plus_dm.ewm(**smoothing).mean() / average_range
    minus_di = 100 * minus_dm.ewm(**smoothing).mean() / average_range

    denominator = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / denominator.where(denominator > 0)
    return AdxLines(plus_di=plus_di, minus_di=minus_di, adx=dx.ewm(**smoothing).mean())


# --- the whole set ---------------------------------------------------------


@dataclass(frozen=True)
class IndicatorBundle:
    """Every computed indicator, keyed by a name that carries its parameters."""

    frames: dict[str, pd.DataFrame]
    params: IndicatorParams = field(default_factory=IndicatorParams)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self.frames)


def compute_all(panel: Any, params: IndicatorParams | None = None) -> IndicatorBundle:
    """Compute every indicator the panel has the fields for.

    A panel of closes alone still yields the close-only indicators; high/low
    adds the range-based ones; volume and amount add the volume ones. Degrading
    quietly is deliberate — a CSV of closes is a perfectly ordinary input, and
    refusing to compute anything would be less useful than computing some.
    """
    params = params or IndicatorParams()
    close = panel.prices
    frames: dict[str, pd.DataFrame] = {}

    for period in params.ma_periods:
        frames[f"ma_{period}"] = sma(close, period)
    for period in params.ema_periods:
        frames[f"ema_{period}"] = ema(close, period)

    lines = macd(close, params.macd_fast, params.macd_slow, params.macd_signal)
    frames["macd_dif"] = lines.dif
    frames["macd_dea"] = lines.dea
    frames["macd_hist"] = lines.hist

    frames[f"rsi_{params.rsi_period}"] = rsi(close, params.rsi_period)

    bands = boll(close, params.boll_period, params.boll_std)
    frames["boll_mid"] = bands.mid
    frames["boll_upper"] = bands.upper
    frames["boll_lower"] = bands.lower
    frames["boll_width"] = bands.width

    frames[f"volatility_{params.volatility_period}"] = realized_volatility(
        close, params.volatility_period
    )

    if panel.has_field("high") and panel.has_field("low"):
        high, low = panel.field("high"), panel.field("low")
        stochastics = kdj(high, low, close, params.kdj_period, params.kdj_smooth)
        frames["kdj_k"] = stochastics.k
        frames["kdj_d"] = stochastics.d
        frames["kdj_j"] = stochastics.j
        frames[f"atr_{params.atr_period}"] = atr(high, low, close, params.atr_period)
        frames[f"wr_{params.williams_period}"] = williams_r(
            high, low, close, params.williams_period
        )
        frames[f"cci_{params.cci_period}"] = cci(high, low, close, params.cci_period)
        directional = adx(high, low, close, params.adx_period)
        frames[f"plus_di_{params.adx_period}"] = directional.plus_di
        frames[f"minus_di_{params.adx_period}"] = directional.minus_di
        frames[f"adx_{params.adx_period}"] = directional.adx

    # 换手率来自日线接口本身（baostock turn / akshare 换手率），不是算出来的；
    # 这里转发并给出滚动均值作为流动性代理。旧采集数据没有这一列，安静降级。
    if panel.has_field("turnover"):
        turnover = panel.field("turnover")
        frames["turnover"] = turnover
        frames[f"turnover_ma_{params.turnover_ma_period}"] = turnover.rolling(
            params.turnover_ma_period, min_periods=params.turnover_ma_period
        ).mean()

    # 收盘价就能算的派生指标，不依赖额外字段
    frames[f"bias_{params.bias_period}"] = bias(close, params.bias_period)
    frames[f"roc_{params.roc_period}"] = roc(close, params.roc_period)
    frames[f"psy_{params.psy_period}"] = psy(close, params.psy_period)

    if panel.has_field("volume"):
        volume = panel.field("volume")
        if panel.has_field("amount"):
            frames["vwap"] = vwap(panel.field("amount"), volume)
        frames["obv"] = obv(close, volume)
        frames[f"volume_ratio_{params.volume_ratio_period}"] = volume_ratio(
            volume, params.volume_ratio_period
        )
        frames[f"vr_{params.vr_period}"] = vr(close, volume, params.vr_period)
        if panel.has_field("high") and panel.has_field("low"):
            frames[f"mfi_{params.mfi_period}"] = mfi(
                panel.field("high"), panel.field("low"), close, volume, params.mfi_period
            )

    if panel.has_field("high") and panel.has_field("low"):
        high, low = panel.field("high"), panel.field("low")
        channels = donchian(high, low, params.donchian_period)
        frames[f"donchian_high_{params.donchian_period}"] = channels.upper
        frames[f"donchian_low_{params.donchian_period}"] = channels.lower
        frames["sar"] = sar(high, low)

    return IndicatorBundle(frames=frames, params=params)


PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def panel_from_frames(
    frames: Mapping[str, pd.DataFrame], source: str = "unknown", adjust: str = "unknown"
) -> Any:
    """Stack per-symbol frames, one wide panel per field.

    This is what turns a provider's ``{symbol: frame}`` into the shape every
    indicator here expects. Fields a source does not carry are simply absent —
    ``compute_all`` degrades rather than demanding them.
    """
    panels: dict[str, pd.DataFrame] = {}
    # 接口自带的标准列之外（如 turnover），只要全票都有也一并拼入——
    # 把它丢在这里，换手率指标就只对 web 缓存链路可见，CLI 链路永远缺位
    wanted = list(PRICE_FIELDS)
    if frames:
        shared = set.intersection(*(set(frame.columns) for frame in frames.values()))
        wanted += sorted(shared - set(PRICE_FIELDS))
    for name in wanted:
        if frames and all(name in frame.columns for frame in frames.values()):
            panels[name] = pd.concat(
                {symbol: frame[name] for symbol, frame in frames.items()}, axis=1
            ).sort_index()
    if "close" not in panels:
        raise ValueError("the provider returned no 'close' column to compute indicators from")

    prices = panels["close"]
    return PricePanel(
        prices=prices,
        extra={name: frame for name, frame in panels.items() if name != "close"},
        meta=DataMeta(
            source=source,
            start=prices.index.min(),
            end=prices.index.max(),
            price_adjustment=adjust,
            symbols=tuple(frames),
        ),
    )


def params_from_settings(settings: Any) -> IndicatorParams:
    """Build the parameters from the ``indicators`` section of loaded settings.

    It lives here rather than in ``config`` so the configuration layer stays
    free of pandas.
    """
    section = settings.indicators
    return IndicatorParams(
        ma_periods=tuple(section.ma_periods),
        ema_periods=tuple(section.ema_periods),
        macd_fast=section.macd_fast,
        macd_slow=section.macd_slow,
        macd_signal=section.macd_signal,
        rsi_period=section.rsi_period,
        boll_period=section.boll_period,
        boll_std=section.boll_std,
        kdj_period=section.kdj_period,
        kdj_smooth=section.kdj_smooth,
        atr_period=section.atr_period,
        williams_period=section.williams_period,
        cci_period=section.cci_period,
        adx_period=section.adx_period,
        volatility_period=section.volatility_period,
        bias_period=section.bias_period,
        roc_period=section.roc_period,
        psy_period=section.psy_period,
        mfi_period=section.mfi_period,
        volume_ratio_period=section.volume_ratio_period,
        donchian_period=section.donchian_period,
        vr_period=section.vr_period,
        turnover_ma_period=section.turnover_ma_period,
    )


# --- storage ---------------------------------------------------------------


def _bundle_directory(directory: Path, provider: str, adjust: str) -> Path:
    return Path(directory) / f"{provider}_{adjust}"


def store_bundle(
    bundle: IndicatorBundle,
    directory: str | Path,
    provider: str,
    adjust: str,
    fmt: str = "parquet",
) -> Path:
    """Write one file per indicator plus a ``meta.json`` that records the params.

    Storing the parameters next to the numbers matters more than it sounds: a
    bundle computed with RSI(14) and one with RSI(21) look exactly alike on
    disk, and the only way to tell them apart later is this file.
    """
    if fmt not in ("parquet", "csv"):
        raise ValueError(f"format must be 'parquet' or 'csv', got {fmt!r}")

    target = _bundle_directory(directory, provider, adjust)
    target.mkdir(parents=True, exist_ok=True)
    for name, frame in bundle.frames.items():
        destination = target / f"{name}.{fmt}"
        if fmt == "parquet":
            frame.to_parquet(destination)
        else:
            frame.to_csv(destination)

    rows = 0
    symbols = 0
    if bundle.frames:
        sample = next(iter(bundle.frames.values()))
        rows, symbols = len(sample.index), len(sample.columns)
    meta = {
        "provider": provider,
        "adjust": adjust,
        "format": fmt,
        "created_at": datetime.now().astimezone().isoformat(),
        "rows": rows,
        "symbols": symbols,
        "start": str(sample.index.min().date()) if rows else None,
        "end": str(sample.index.max().date()) if rows else None,
        "params": bundle.params.to_dict(),
        "fields": list(bundle.frames),
    }
    meta.update(bundle.meta)
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load_bundle(directory: str | Path, provider: str, adjust: str) -> IndicatorBundle | None:
    """Read back what :func:`store_bundle` wrote, or ``None`` if it is not there.

    A bundle whose stored parameters predate the current parameter schema is
    treated as absent: the indicator set grew, the old files know nothing of
    the new fields, and serving them would look like a working indicator page
    that is silently half-empty. Recomputing is what a stale cache is for.
    """
    target = _bundle_directory(Path(directory), provider, adjust)
    meta_path = target / "meta.json"
    if not meta_path.is_file():
        return None

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    fmt = meta.get("format", "parquet")
    frames: dict[str, pd.DataFrame] = {}
    for name in meta.get("fields", []):
        path = target / f"{name}.{fmt}"
        if not path.is_file():
            continue
        frames[name] = (
            pd.read_parquet(path)
            if fmt == "parquet"
            else pd.read_csv(path, index_col=0, parse_dates=True)
        )

    stored = meta.get("params") or {}
    known = {key: value for key, value in stored.items() if key in _PARAM_KEYS}
    if set(known) != _PARAM_KEYS:
        # the parameter schema has grown since this bundle was computed;
        # report a miss so the caller recomputes with the current set
        return None
    if "ma_periods" in known:
        known["ma_periods"] = tuple(known["ma_periods"])
    if "ema_periods" in known:
        known["ema_periods"] = tuple(known["ema_periods"])
    return IndicatorBundle(
        frames=frames, params=IndicatorParams(**known), meta={k: v for k, v in meta.items()}
    )


@dataclass(frozen=True)
class IndicatorDoc:
    """What an interface needs to *explain* an indicator, not compute it.

    Kept next to the code so the sentence describing RSI and the arithmetic
    producing it cannot drift apart. ``label`` carries the parameters, ``unit``
    and ``typical_range`` are for the axis and the tooltip, and ``how_to_use``
    is deliberately one sentence — anything longer stops being read.
    """

    label: str
    unit: str
    typical_range: str
    description: str
    how_to_use: str
    caution: str = ""


INDICATOR_CATALOG: dict[str, IndicatorDoc] = {
    "ma": IndicatorDoc(
        label="MA（均线）",
        unit="价格",
        typical_range="与价格同量级",
        description="最近 n 根收盘价的算术平均，滞后于价格。",
        how_to_use="看方向与斜率：价格在均线上方且均线上行，说明这段趋势还在。",
        caution="金叉死叉在震荡市里会反复触发，先看交易次数，再看收益。",
    ),
    "ema": IndicatorDoc(
        label="EMA（指数均线）",
        unit="价格",
        typical_range="与价格同量级",
        description="按 2/(n+1) 加权递推的均线，对新价格反应比 MA 快。",
        how_to_use="与同周期 MA 并排看，两者距离扩大说明近期价格在加速。",
        caution="初始值处理影响前几十根，跨软件对比前先确认 adjust 口径。",
    ),
    "macd_dif": IndicatorDoc(
        label="MACD·DIF",
        unit="价格",
        typical_range="与价格波动同量级",
        description="快慢两条 EMA 的差，>0 表示短期均线在长期之上。",
        how_to_use="看与零轴的关系判断中期动能方向，别单独用来择时。",
        caution="依赖 EMA(26)，前 26 根是预热期，历史不足时不要解读。",
    ),
    "macd_dea": IndicatorDoc(
        label="MACD·DEA",
        unit="价格",
        typical_range="与价格波动同量级",
        description="DIF 的 9 日 EMA，是 DIF 的平滑版本，即信号线。",
        how_to_use="与 DIF 交叉是常见的动能转折提示，但在趋势末段滞后明显。",
        caution="交叉天然滞后于价格：它确认的是已经发生的转折，不是将要发生的。",
    ),
    "macd_hist": IndicatorDoc(
        label="MACD·柱",
        unit="价格",
        typical_range="随波动放大",
        description="2×(DIF−DEA)，国内软件画 2 倍，国外文献多为 1 倍。",
        how_to_use="看柱体的长短变化比看正负更有用：缩短说明动能衰减。",
        caution="阈值不可跨软件照抄，2 倍与 1 倍的尺度差一倍。",
    ),
    "rsi": IndicatorDoc(
        label="RSI（相对强弱）",
        unit="0–100",
        typical_range="20–80 常见",
        description="一段时间内上涨幅度占涨跌总幅度的比例，Wilder 平滑。",
        how_to_use="当作温度计而不是开关：>80 说明近期几乎只涨，<20 说明几乎只跌。",
        caution="强趋势里会长期钝化在超买区，用它做反转信号是最常见的误用。",
    ),
    "boll_mid": IndicatorDoc(
        label="BOLL·中轨",
        unit="价格",
        typical_range="与价格同量级",
        description="20 日均线，布林带的中枢。",
        how_to_use="作为短期均衡价，价格反复穿越说明在震荡而非趋势。",
        caution="中轨就是同周期均线，别把它当成独立的支撑位。",
    ),
    "boll_upper": IndicatorDoc(
        label="BOLL·上轨",
        unit="价格",
        typical_range="中轨 + 2σ",
        description="中轨加 2 倍总体标准差，波动越大越宽。",
        how_to_use="触碰上轨只说明价格偏高，不说明要跌。",
        caution="标准差用总体（ddof=0），与 pandas 默认的样本标准差差 2.6%（n=20）。",
    ),
    "boll_lower": IndicatorDoc(
        label="BOLL·下轨",
        unit="价格",
        typical_range="中轨 − 2σ",
        description="中轨减 2 倍总体标准差。",
        how_to_use="与上轨一起构成通道，位置比突破更有信息量。",
        caution="下跌趋势里价格会贴着下轨走很久，那不是超跌信号。",
    ),
    "boll_width": IndicatorDoc(
        label="BOLL·带宽",
        unit="比例",
        typical_range="0.02–0.3",
        description="（上轨−下轨）/中轨，衡量波动收敛程度。",
        how_to_use="带宽收窄到历史低位说明波动在压缩，但方向仍未知。",
        caution="收窄只描述状态，不预示突破方向。",
    ),
    "volatility": IndicatorDoc(
        label="已实现波动率",
        unit="年化比例",
        typical_range="0.1–0.6",
        description="日收益标准差 × √252，样本标准差（ddof=1）。",
        how_to_use="用于仓位与风险预算：波动越高，同样的金额对应的风险越大。",
        caution="年化系数 252 是惯例近似，跨市场比较时统一即可。",
    ),
    "kdj_k": IndicatorDoc(
        label="KDJ·K",
        unit="0–100",
        typical_range="10–90 常见",
        description="RSV 的平滑值，K 与 D 的初值都是 50。",
        how_to_use="看 K 与 D 的相对位置，J 值用来放大极值。",
        caution="一字板当天区间为 0，指标沿用前值，不应解读为横盘。",
    ),
    "kdj_d": IndicatorDoc(
        label="KDJ·D",
        unit="0–100",
        typical_range="10–90 常见",
        description="K 再平滑一次，比 K 更钝。",
        how_to_use="K 上穿 D 是常见买点提示，但在低波动区间噪声很大。",
        caution="D 比 K 更钝，交叉更慢；横盘时两者会反复缠绕，别逐次响应。",
    ),
    "kdj_j": IndicatorDoc(
        label="KDJ·J",
        unit="可超出 0–100",
        typical_range="−20–120",
        description="3K−2D，放大 K 与 D 的偏离。",
        how_to_use="只用于观察极端，不作为独立信号。",
        caution="J 超出 0–100 是设计如此，不是数据错误。",
    ),
    "atr": IndicatorDoc(
        label="ATR（真实波幅）",
        unit="价格",
        typical_range="价格的 1%–5%",
        description="含跳空的真实波幅的 Wilder 平滑，衡量单日波动幅度。",
        how_to_use="用来定止损距离与仓位：止损放在 2×ATR 之外比固定百分比更贴合个股。",
        caution="ATR 是波动幅度，不是方向，也不含滑点。",
    ),
    "wr": IndicatorDoc(
        label="威廉 %R",
        unit="−100–0",
        typical_range="−80–−20 常见",
        description="收盘价在 n 日区间中的位置，坐标与 KDJ 的 RSV 相反。",
        how_to_use="与 RSI 同看：两者背离时先别急着动手。",
        caution="区间为 0 时无定义，输出为缺失值。",
    ),
    "cci": IndicatorDoc(
        label="CCI（顺势指标）",
        unit="无单位",
        typical_range="−200–200",
        description="典型价格偏离其均值的程度，分母是平均绝对偏差。",
        how_to_use="±100 是常用的观察阈值，突破后持续走强比单点穿越更值得看。",
        caution="分母是平均绝对偏差而非标准差，口径不同数值尺度就不同。",
    ),
    "plus_di": IndicatorDoc(
        label="DMI·+DI",
        unit="0–100",
        typical_range="5–40",
        description="上升方向的平滑动向占真实波幅的比例。",
        how_to_use="与 −DI 比较判断多空力量对比，不看绝对值看相对位置。",
        caution="+DI 的绝对值随波动率放大，跨标的比较没有意义。",
    ),
    "minus_di": IndicatorDoc(
        label="DMI·−DI",
        unit="0–100",
        typical_range="5–40",
        description="下降方向的平滑动向占真实波幅的比例。",
        how_to_use="+DI 在上方说明这段以涨为主，反转需等交叉确认。",
        caution="−DI 上升只说明下跌动能在增强，不等于价格一定在跌。",
    ),
    "adx": IndicatorDoc(
        label="ADX（趋势强度）",
        unit="0–100",
        typical_range="10–50",
        description="DX 的平滑值，只反映趋势强弱，不含方向。",
        how_to_use="ADX 上行说明趋势在增强，此时顺势比逆势更安全；下行则相反。",
        caution="ADX 本身滞后，且不说方向，不要单独解读。",
    ),
    "bias": IndicatorDoc(
        label="BIAS（乖离率）",
        unit="百分比",
        typical_range="−15%–+15%",
        description="（收盘价 − n 日均线）/ n 日均线 × 100，衡量价格偏离均线的程度。",
        how_to_use="正值高于均线、负值低于；与自身历史比较比绝对阈值可靠——每只票的常态乖离不同。",
        caution="强趋势里乖离可以长期停在高位，用「超买就回来」的思路逆势抄底是最常见的误用。",
    ),
    "roc": IndicatorDoc(
        label="ROC（变动率）",
        unit="百分比",
        typical_range="与个股波动相关",
        description="现价相对 n 日前的百分比变化，最朴素的动量指标。",
        how_to_use="主要用于截面排名：谁最近动得猛。时序上过零点确认动能转向。",
        caution="n 日前的价格是基准，基准日恰好暴涨/暴跌时数值会跳变，属正常。",
    ),
    "psy": IndicatorDoc(
        label="PSY（心理线）",
        unit="0–100",
        typical_range="25–75 常见",
        description="过去 n 天里上涨天数的占比，只数天数不看幅度。",
        how_to_use="与 RSI 互补：RSI 按幅度加权、PSY 按天数；两者背离时以幅度为准。",
        caution="阴跌或阴涨的缓慢趋势里，PSY 可能停在极端区很久，不代表转折将至。",
    ),
    "volume_ratio": IndicatorDoc(
        label="量比（5 日基准）",
        unit="倍数",
        typical_range="0.5–3",
        description="当日成交量 / 前 5 日平均成交量，衡量今日相对近期的活跃度。",
        how_to_use=">2 明显放量、配合价格方向解读；<0.5 缩量，突破的可靠性下降。",
        caution="日线近似，盘中的量比偏低（行情软件按分钟折算）；停牌复牌日数值会异常。",
    ),
    "vr": IndicatorDoc(
        label="VR（成交量比率）",
        unit="百分比",
        typical_range="40–250",
        description="n 日内上涨日成交合计与下跌日成交合计之比（平盘量对半分摊）。",
        how_to_use="低价区 VR 抬升而价格未动，常是资金试探性建仓；高价区 VR 下滑说明追价资金退场。",
        caution="持续单边市的 VR 会钝化在一端，与 RSI 的钝化同源。",
    ),
    "mfi": IndicatorDoc(
        label="MFI（资金流量）",
        unit="0–100",
        typical_range="20–80 常见",
        description="用典型价（H+L+C）/3 加权成交量的振荡指标，带量的 RSI。",
        how_to_use="价格新高而 MFI 不创新高（顶背离）是资金撤出的早期线索。",
        caution="成交量造假与大宗交易会扭曲它；小盘股的 MFI 噪声大于大盘股。",
    ),
    "donchian_high": IndicatorDoc(
        label="唐奇安通道·上轨",
        unit="价格",
        typical_range="近期高点区域",
        description="前 n 日（不含当日）的最高价，海龟交易法则的突破参照。",
        how_to_use="收盘价上穿上轨是突破信号；n 越大信号越少越可靠。",
        caution="不含当日是刻意设计；突破信号在震荡市里假信号多，先看交易次数。",
    ),
    "donchian_low": IndicatorDoc(
        label="唐奇安通道·下轨",
        unit="价格",
        typical_range="近期低点区域",
        description="前 n 日（不含当日）的最低价，与上轨构成通道。",
        how_to_use="跌破下轨是趋势走坏的参照；也常用于海龟法则的离场判定。",
        caution="与布林带不同，唐奇安只用极值不用波动率，通道宽度即真实波动范围。",
    ),
    "sar": IndicatorDoc(
        label="SAR（抛物线转向）",
        unit="价格",
        typical_range="价格下方（升势）或上方（跌势）",
        description="Wilder 递推的停损转向点，加速度因子随新极值累加（0.02 → 0.2）。",
        how_to_use="价格在 SAR 之上看多、之下看空；SAR 紧贴趋势行走，适合做移动止损参照。",
        caution="横盘时 SAR 会频繁翻转；递推不可向量化，数值与个别软件的初值约定可能有差异。",
    ),
    "turnover": IndicatorDoc(
        label="换手率",
        unit="百分比",
        typical_range="0.5%–15%",
        description="当日成交量占流通股本的比例，来自日线接口本身（baostock turn / akshare 换手率）。",
        how_to_use="衡量活跃度的最诚实口径——已经除掉了盘子大小，跨票可比；配合价格方向解读放量。",
        caution="只对新采集的数据可用（旧落盘文件没有这一列）；限售解禁、送转股本变动会让换手率跳变。",
    ),
    "turnover_ma": IndicatorDoc(
        label="换手率均值",
        unit="百分比",
        typical_range="0.3%–10%",
        description="n 日平均换手率，作为流动性的稳定口径（比单日值噪声小）。",
        how_to_use="流动性过滤用均值不用单日：单日放大可能是事件，均值才是常态盘子。",
        caution="均值周期越长越平滑也越滞后；小盘股与大盘股的常态区间差一个数量级。",
    ),
    "obv": IndicatorDoc(
        label="OBV（能量潮）",
        unit="累计成交量",
        typical_range="无界",
        description="涨日加量、跌日减量的累计成交量。",
        how_to_use="看趋势与价格是否同向：量价背离比单日数值更有意义。",
        caution="累计量取决于起点，跨标的不可比。",
    ),
    "vwap": IndicatorDoc(
        label="VWAP（成交均价）",
        unit="价格",
        typical_range="与收盘价同量级",
        description="成交额 ÷ 成交量（手×100），当日的平均成交成本。",
        how_to_use="收盘高于 VWAP 说明当日买方占优，适合做执行质量的参照。",
        caution="单位：volume 存手、amount 存元，少乘 100 会让均价高 100 倍而形状正常。",
    ),
}


def family_of(field: str) -> str:
    """``rsi_21`` → ``rsi``: the period is a parameter, not part of the family.

    Kept in one place because both the catalogue lookup and the chart panels ask
    the same question, and two copies of this rule would eventually disagree.
    """
    return field.rstrip("0123456789").rstrip("_")


def doc_for(field: str) -> IndicatorDoc | None:
    """Look up the description for a computed field (``rsi_14`` → ``rsi``)."""
    return INDICATOR_CATALOG.get(family_of(field))


def catalog() -> dict[str, dict[str, str]]:
    """The whole catalogue as plain dicts, for an API or a table."""
    return {
        name: {
            "label": doc.label,
            "unit": doc.unit,
            "typical_range": doc.typical_range,
            "description": doc.description,
            "how_to_use": doc.how_to_use,
            "caution": doc.caution,
        }
        for name, doc in INDICATOR_CATALOG.items()
    }


_PARAM_KEYS = frozenset(
    {
        "ma_periods",
        "ema_periods",
        "macd_fast",
        "macd_slow",
        "macd_signal",
        "rsi_period",
        "boll_period",
        "boll_std",
        "kdj_period",
        "kdj_smooth",
        "atr_period",
        "williams_period",
        "cci_period",
        "adx_period",
        "volatility_period",
        "bias_period",
        "roc_period",
        "psy_period",
        "mfi_period",
        "volume_ratio_period",
        "donchian_period",
        "vr_period",
        "turnover_ma_period",
    }
)
