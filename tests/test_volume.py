"""量能分析：大盘 / 板块 / 个股三层，以及量价组合的判定。

守住的几条：量比一律和自己的 20 日均量比（今日不参与自己的基准）；放量
不等于看涨——四象限各有各的话术，缩量上涨不能说成「资金流入」；缺数据
时报错或降级，不编一个 0 出来。
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.volume import VolumeRules, market, sector, stock


def _panel(
    amounts: dict[str, list[float]],
    prices: dict[str, list[float]] | None = None,
    with_amount: bool = True,
) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=len(next(iter(amounts.values()))))
    frames = {symbol: pd.Series(values, index=index) for symbol, values in amounts.items()}
    amount = pd.DataFrame(frames)
    prices_frame = (
        pd.DataFrame(prices, index=index)
        if prices
        else pd.DataFrame({symbol: [10.0] * len(index) for symbol in amounts}, index=index)
    )
    extra = {"amount": amount, "volume": amount / 10}
    if not with_amount:
        extra.pop("amount")
    return PricePanel(prices=prices_frame, extra=extra)


def _spike(base: float = 100.0, today: float = 200.0, days: int = 25) -> list[float]:
    """``days`` bars at ``base``, the last one at ``today``."""
    return [base] * (days - 1) + [today]


class TestRules:
    def test_shrink_must_sit_below_surge(self):
        with pytest.raises(ValueError):
            VolumeRules(surge=0.8, shrink=1.2)

    def test_huge_and_low_must_be_outside_the_band(self):
        with pytest.raises(ValueError):
            VolumeRules(surge=2.5, huge=1.2)
        with pytest.raises(ValueError):
            VolumeRules(shrink=0.5, low=0.8)


class TestMarket:
    def test_ratio_compares_today_with_the_trailing_mean(self):
        panel = _panel({"600000.SH": _spike(base=100.0, today=200.0)})

        row = market(panel)

        # 前 24 天 100，今日 200；基准是前 20 天（不含今天）= 100
        assert row.ratio == pytest.approx(2.0, abs=0.01)
        assert row.level == "放量"
        assert row.n_symbols == 1

    def test_amount_is_summed_across_symbols_in_yi(self):
        panel = _panel(
            {"A.SH": _spike(100e6, 200e6), "B.SH": _spike(100e6, 200e6)},
        )

        row = market(panel)

        assert row.amount == pytest.approx(4.0, abs=0.01)  # 两只各 2 亿 = 4 亿
        assert row.base_long == pytest.approx(2.0, abs=0.01)

    def test_rising_on_volume_reads_as_the_bullish_quadrant(self):
        prices = {"A.SH": [10.0] * 24 + [11.0]}  # 今日 +10%
        panel = _panel({"A.SH": _spike(100.0, 200.0)}, prices)

        row = market(panel)

        assert row.quadrant == "放量上涨"
        assert "跟随资金" in row.advice
        assert "2.00" in row.advice  # 建议里带具体倍数

    def test_falling_on_volume_does_not_become_a_buy_signal(self):
        prices = {"A.SH": [10.0] * 24 + [9.0]}  # 今日 -10%
        panel = _panel({"A.SH": _spike(100.0, 200.0)}, prices)

        row = market(panel)

        assert row.quadrant == "放量下跌"
        assert "别在放量下跌里抄底" in row.advice

    def test_rising_on_shrinking_volume_is_not_called_inflow(self):
        prices = {"A.SH": [10.0] * 24 + [10.5]}
        panel = _panel({"A.SH": _spike(100.0, 40.0)}, prices)

        row = market(panel)

        assert row.quadrant == "缩量上涨"
        assert "没人跟" in row.advice
        assert "资金在流入" not in row.advice

    def test_streak_counts_consecutive_heavy_days(self):
        panel = _panel({"A.SH": [100.0] * 22 + [200.0, 200.0, 200.0]})

        row = market(panel)

        assert row.streak == 3

    def test_a_short_panel_is_an_error_not_a_guess(self):
        panel = _panel({"A.SH": [100.0] * 10})

        with pytest.raises(ValueError):
            market(panel)

    def test_no_amount_falls_back_to_lots(self):
        panel = _panel({"A.SH": _spike(1000.0, 2000.0)}, with_amount=False)

        row = market(panel)

        assert row.unit == "volume"
        assert row.ratio == pytest.approx(2.0, abs=0.01)


class TestSector:
    def _sectors(self) -> dict[str, str]:
        return {"A.SH": "银行", "B.SH": "银行", "C.SH": "白酒"}

    def test_shares_sum_to_a_hundred_percent(self):
        panel = _panel(
            {
                "A.SH": _spike(100e6, 200e6),
                "B.SH": _spike(100e6, 100e6),
                "C.SH": _spike(100e6, 100e6),
            }
        )

        rows = sector(panel, self._sectors())

        assert sum(row.share_pct for row in rows) == pytest.approx(100.0, abs=0.1)

    def test_sector_ratio_uses_its_own_baseline(self):
        panel = _panel(
            {
                "A.SH": _spike(100e6, 300e6),
                "B.SH": _spike(100e6, 300e6),
                "C.SH": _spike(100e6, 100e6),
            }
        )

        rows = {row.sector: row for row in sector(panel, self._sectors())}

        assert rows["银行"].ratio == pytest.approx(3.0, abs=0.01)  # (300+300)/(100+100)
        assert rows["白酒"].ratio == pytest.approx(1.0, abs=0.01)

    def test_heaviest_volume_sector_comes_first(self):
        panel = _panel(
            {
                "A.SH": _spike(100e6, 500e6),
                "B.SH": _spike(100e6, 500e6),
                "C.SH": _spike(100e6, 100e6),
            }
        )

        rows = sector(panel, self._sectors())

        assert rows[0].sector == "银行"

    def test_symbols_outside_the_panel_are_ignored(self):
        panel = _panel({"A.SH": _spike(100e6, 200e6)})

        rows = sector(panel, {"A.SH": "银行", "Z.SH": "银行"})

        assert rows[0].n == 1


class TestStock:
    def test_only_requested_symbols_come_back(self):
        panel = _panel(
            {"A.SH": _spike(100e6, 200e6), "B.SH": _spike(100e6, 100e6)},
        )

        rows = stock(panel, ["A.SH", "MISSING.SH"])

        assert [row.symbol for row in rows] == ["A.SH"]

    def test_cross_stock_comparison_belongs_to_the_sector_layer(self):
        """个股层不做跨票排序：同一板块两只票的量比各自独立，不该被对方影响。"""
        panel = _panel(
            {"A.SH": _spike(100e6, 200e6), "B.SH": _spike(100e6, 100e6)},
        )

        rows = {row.symbol: row for row in stock(panel, ["A.SH", "B.SH"])}

        assert rows["A.SH"].ratio == pytest.approx(2.0, abs=0.01)
        assert rows["B.SH"].ratio == pytest.approx(1.0, abs=0.01)

    def test_a_huge_volume_day_is_flagged_as_divergence(self):
        prices = {"A.SH": [10.0] * 24 + [10.2]}
        panel = _panel({"A.SH": _spike(100e6, 300e6)}, prices)

        row = stock(panel, ["A.SH"])[0]

        assert row.level == "巨量"
        assert "分歧" in row.advice

    def test_a_dry_day_is_flagged_as_waiting_for_direction(self):
        panel = _panel({"A.SH": _spike(100e6, 30e6)})

        row = stock(panel, ["A.SH"])[0]

        assert row.level == "地量"
        assert "变盘" in row.advice
