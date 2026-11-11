"""三条可编码战法的信号：规则要按战法原文的子集编码，且不许偷看未来。

守住的几条：入场/出场只用当日及之前的数据（rolling 全部 shift(1) 排除
当日）；面板没有成交额时量比条件如实降级并在行为上可观察（不是悄悄
等于纯价格版）；从未触发就返回全 0，不编信号。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_research.strategies import (
    STRATEGY_REGISTRY,
    breakout_signal,
    get_signal,
    ma20_pullback_signal,
    turtle_signal,
)


def _frame(values: dict[str, list[float]], dates: int | None = None) -> pd.DataFrame:
    length = dates or len(next(iter(values.values())))
    index = pd.bdate_range("2024-01-01", periods=length)
    return pd.DataFrame(
        {
            symbol: pd.Series(row, index=index[: len(row)] if len(row) != length else index)
            for symbol, row in values.items()
        }
    )


def _amount_frame(close: pd.DataFrame, base: float = 1e6) -> pd.DataFrame:
    return close.apply(lambda col: col * 0 + base)


class TestRegistry:
    def test_three_tactics_are_registered(self):
        for name in ("turtle", "breakout-20d-high", "pullback-ma20"):
            assert name in STRATEGY_REGISTRY
            assert callable(get_signal(name))

    def test_output_is_zero_one_int8(self):
        rng = np.random.default_rng(11)
        index = pd.bdate_range("2024-01-01", periods=80)
        close = pd.DataFrame(
            {f"S{step}": 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.01, 80)) for step in range(4)},
            index=index,
        )
        for signal in (turtle_signal(close), breakout_signal(close), ma20_pullback_signal(close)):
            assert set(np.unique(signal)) <= {0, 1}
            assert signal.index.equals(close.index) and signal.columns.equals(close.columns)


class TestTurtle:
    def test_entry_at_the_close_breaking_the_prior_high(self):
        """30 天横盘 100，第 31 天收 120：恰在突破日翻仓，且当天之前为零。"""
        close = _frame({"A": [100.0] * 30 + [120.0]})
        signal = turtle_signal(close, entry_window=20, exit_window=10)

        assert signal["A"].iloc[:30].eq(0).all()  # 窗口不足 + 未突破
        assert signal["A"].iloc[30] == 1  # 收盘破前 20 日高，当日生效

    def test_exit_when_the_close_breaks_the_prior_low(self):
        close = _frame({"A": [100.0] * 30 + [120.0, 90.0]})
        signal = turtle_signal(close, entry_window=20, exit_window=10)

        assert signal["A"].iloc[30] == 1  # 突破日入场
        assert signal["A"].iloc[31] == 0  # 次日收盘跌破前 10 日低，出场

    def test_no_lookahead_the_signal_ignores_the_future(self):
        """改掉突破日之后的价格，突破日之前的信号必须逐点不变。"""
        base = [100.0] * 30 + [120.0] + [110.0] * 10
        close_a = _frame({"A": base})
        modified = list(base)
        modified[35:] = [50.0] * 5  # 未来暴跌
        close_b = _frame({"A": modified})

        pd.testing.assert_frame_equal(
            turtle_signal(close_a).iloc[:35], turtle_signal(close_b).iloc[:35]
        )

    def test_short_panel_never_enters(self):
        """15 天面板连 20 日窗口都不够：信号全零，不编一个入场。"""
        close = _frame({"A": [100.0] * 15})
        assert turtle_signal(close)["A"].eq(0).all()

    def test_invalid_windows_are_rejected(self):
        close = _frame({"A": [100.0] * 5})
        with pytest.raises(ValueError):
            turtle_signal(close, entry_window=0)


class TestBreakout:
    def test_volume_gate_blocks_a_thin_breakout(self):
        """突破日量比 0.5：价格条件满足但量比不达标，不入场。"""
        dates = pd.bdate_range("2024-01-01", periods=30)
        close = pd.DataFrame({"A": [100.0] * 29 + [120.0]}, index=dates)
        amount = pd.DataFrame({"A": [1e6] * 29 + [5e5]}, index=dates)  # 当日缩到一半

        signal = breakout_signal(close, amount=amount)

        assert signal["A"].iloc[29] == 0

    def test_volume_gate_admits_a_heavy_breakout(self):
        dates = pd.bdate_range("2024-01-01", periods=30)
        close = pd.DataFrame({"A": [100.0] * 29 + [120.0]}, index=dates)
        amount = pd.DataFrame({"A": [1e6] * 29 + [3e6]}, index=dates)  # 量比 3

        signal = breakout_signal(close, amount=amount)

        assert signal["A"].iloc[29] == 1

    def test_without_amount_the_price_rule_still_works(self):
        """没量数据：如实降级为纯价格突破——不因为缺列而全零。"""
        dates = pd.bdate_range("2024-01-01", periods=30)
        close = pd.DataFrame({"A": [100.0] * 29 + [120.0]}, index=dates)

        assert breakout_signal(close, amount=None)["A"].iloc[29] == 1

    def test_exit_after_days_below_the_high(self):
        dates = pd.bdate_range("2024-01-01", periods=33)
        close = pd.DataFrame({"A": [100.0] * 29 + [120.0, 110.0, 110.0, 110.0]}, index=dates)
        signal = breakout_signal(close, amount=None, exit_days=2)

        assert signal["A"].iloc[29] == 1  # 突破日入场
        assert signal["A"].iloc[30] == 1  # 跌回前高下第 1 天：未满确认天数
        assert signal["A"].iloc[31] == 0  # 连续 2 天：假突破出场


class TestPullback:
    def _trend_panel(self, dates: int = 40) -> pd.DataFrame:
        """先涨后横：MA20 向上、价格横在均线附近——回踩条件成立。"""
        steps = np.concatenate([np.linspace(80.0, 120.0, 25), np.full(dates - 25, 118.0)])
        return pd.DataFrame({"A": steps}, index=pd.bdate_range("2024-01-01", periods=dates))

    def test_entry_inside_the_band_on_a_flat_day(self):
        close = self._trend_panel()
        signal = ma20_pullback_signal(close, amount=None, band=0.02)

        assert signal["A"].iloc[-1] == 1  # 横盘段：均线向上、贴线、当日不跌

    def test_shrink_volume_required_when_amount_is_given(self):
        close = self._trend_panel()
        amount = pd.DataFrame(
            {"A": [2e6] * 39 + [4e6]},
            index=close.index,  # 回踩日放量
        )
        signal = ma20_pullback_signal(close, amount=amount)

        assert signal["A"].iloc[-1] == 0  # 「缩量回踩」的缩量条件挡住放量日

    def test_exit_on_consecutive_breaks_below_ma20(self):
        close = self._trend_panel(40)
        close.iloc[-3:, 0] = [100.0, 99.0, 98.0]  # 连续 3 天深破均线
        signal = ma20_pullback_signal(close, amount=None, exit_days=2)

        assert signal["A"].iloc[-3] == 1  # 深破第一天：确认窗口未满，仍在场
        assert signal["A"].iloc[-2] == 0  # 连续 2 天破位：趋势破坏出场
        assert signal["A"].iloc[-1] == 0

    def test_band_boundary_is_inclusive(self):
        close = self._trend_panel()
        ma20 = close["A"].rolling(20, min_periods=20).mean()
        # 构造恰好落在带宽边缘的收盘价
        close.iloc[-1, 0] = float(ma20.iloc[-2] * 1.02)
        signal = ma20_pullback_signal(close, amount=None, band=0.02)

        assert signal["A"].iloc[-1] == 1  # ±2% 是闭区间
