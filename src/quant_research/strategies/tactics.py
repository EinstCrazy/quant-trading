"""三条可编码战法的日线回测信号：turtle / breakout-20d-high / pullback-ma20。

编码口径（诚实优先）：战法文案里的很多细节（金字塔加仓、ATR 定仓、
「收红或十字星」、盘中盯盘）在「0/1 信号 × 等权持有」的日线回测里无法
表达，这里只编码规则的可计算子集，逐条说明近似方式：

- turtle：收盘 > 前 entry_window 日高（不含当日）入场；收盘 < 前
  exit_window 日低出场。与战法文案一一对应。
- breakout-20d-high：突破日量比 ≥ vol_ratio_min 才入场；出场编码为
  「收盘跌回前高下方连续 exit_days 天」。金字塔加仓不编码。
- pullback-ma20：MA20 走平或向上（ma20 > 3 日前的 ma20）、收盘落在
  MA20 ±band 带内、当日不跌于昨收（近似「收红或十字星」）且缩量
  （量比 < 1）入场；「放量跌破 MA20 且 2 天收不回」编码为收盘 < MA20
  且量比 > exit_vol_ratio 连续 exit_days 天。

面板没有成交额时，依赖量的条件**如实降级**（量比条件自动放行）并在
docstring 标注——不编造一个假装有量的信号。

所有入场/出场都只用当日及之前的数据：rolling 窗口以 shift(1) 排除
当日，量比基准同样不含当日（与 volume._window_stats 的约定一致）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_research.strategies.base import register

__all__ = ["breakout_signal", "ma20_pullback_signal", "turtle_signal"]

State = pd.DataFrame


def _vol_ratio(amount: pd.DataFrame | None, window: int) -> pd.DataFrame | None:
    """当日成交额 / 前 window 日均额（不含当日）；缺失时返回 None。"""
    if amount is None:
        return None
    base = amount.shift(1).rolling(window, min_periods=window).mean()
    return amount / base


def _hold_state(entry: pd.DataFrame, leave: pd.DataFrame) -> State:
    """状态机向量化：entry 置 1、leave 置 0，其余维持前值；初始为空仓。

    同一天既触发入场又触发出场时，入场优先——突破日不会同时满足
    「跌破出场位」，这个优先级只在构造极端测试数据时才起作用。
    """
    values = np.where(entry, 1.0, np.where(leave, 0.0, np.nan))
    frame = pd.DataFrame(values, index=entry.index, columns=entry.columns)
    return frame.ffill().fillna(0).astype("int8")


def _rolled_count(condition: pd.DataFrame, days: int) -> pd.DataFrame:
    """条件连续成立 days 天（含当日）的判定，窗口不足时为 False。"""
    return condition.rolling(days, min_periods=days).sum() == days


@register("turtle")
def turtle_signal(
    close: pd.DataFrame,
    entry_window: int = 20,
    exit_window: int = 10,
) -> State:
    """海龟突破（唐奇安通道）：收盘破前 entry_window 日高入场，
    收盘破前 exit_window 日低无条件出场。"""
    if entry_window <= 0 or exit_window <= 0:
        raise ValueError("entry_window 与 exit_window 必须为正")
    upper = close.shift(1).rolling(entry_window, min_periods=entry_window).max()
    lower = close.shift(1).rolling(exit_window, min_periods=exit_window).min()
    return _hold_state(close > upper, close < lower)


@register("breakout-20d-high")
def breakout_signal(
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    entry_window: int = 20,
    vol_ratio_min: float = 1.5,
    exit_days: int = 2,
) -> State:
    """放量突破 20 日高：收盘破前高且量比达标才入场；收盘跌回前高
    下方连续 exit_days 天判假突破出场。"""
    if entry_window <= 0 or exit_days <= 0 or vol_ratio_min <= 0:
        raise ValueError("entry_window/exit_days 必须为正，vol_ratio_min 必须大于 0")
    upper = close.shift(1).rolling(entry_window, min_periods=entry_window).max()
    ratio = _vol_ratio(amount, entry_window)
    if ratio is None:
        entry = close > upper  # 没量数据：只按价格编码，不假装量比达标
    else:
        entry = (close > upper) & (ratio >= vol_ratio_min)
    leave = _rolled_count(close < upper, exit_days)
    return _hold_state(entry, leave)


@register("pullback-ma20")
def ma20_pullback_signal(
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    band: float = 0.02,
    exit_days: int = 2,
    exit_vol_ratio: float = 1.5,
) -> State:
    """回踩 MA20 不破：趋势向上时缩量回踩生命线低吸；放量破位出场。"""
    if band <= 0 or exit_days <= 0 or exit_vol_ratio <= 0:
        raise ValueError("band/exit_days/exit_vol_ratio 必须为正")
    ma20 = close.rolling(20, min_periods=20).mean()
    rising = ma20 > ma20.shift(3)  # 走平或向上：与 3 日前比，钝化毛刺
    in_band = (close / ma20 - 1).abs() <= band
    not_down = close >= close.shift(1)  # 近似「收红或十字星」：当日不跌
    ratio = _vol_ratio(amount, 20)
    if ratio is None:
        entry = rising & in_band & not_down  # 没量数据：缩量条件放行
    else:
        entry = rising & in_band & not_down & (ratio < 1.0)
    if ratio is None:
        leave = _rolled_count(close < ma20, exit_days)
    else:
        leave = _rolled_count((close < ma20) & (ratio > exit_vol_ratio), exit_days)
    return _hold_state(entry, leave)
