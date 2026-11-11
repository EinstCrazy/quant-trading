"""明日仓位预案：开盘位置 × 量能 → 仓位动作。

纪律三条：预案是条件表不是预测（动作里全是「如果……就……」，价位
全部来自今天的盘面）；没有现金数据就不编「总仓位几成」——仓位用
「留几成」表达，整体影响用市值区间表达；量能出现在条件里（放量则…/
缩量则…），不出现在结论里——明天的量能没人知道。
"""

from __future__ import annotations

from quant_research.position_plan import plan_positions


def _row(**overrides):
    base = {
        "symbol": "600519.SH",
        "account": "默认",
        "name": "贵州茅台",
        "stance": "hold",
        "label": "持有",
        "close": 1345.0,
        "volume_ratio": 0.82,
        "market_value": 134500.0,
        "shares": 100,
        "cost": 1500.0,
        "levels": {
            "cost": 1500.0,
            "close": 1345.0,
            "ma20": 1340.0,
            "atr_stop": 1290.5,
            "high20": 1420.0,
        },
    }
    base.update(overrides)
    return base


class TestBranches:
    def test_hold_low_open_carries_the_stop_price(self):
        plan = plan_positions([_row()])
        gap_down = plan["positions"][0]["branches"]["gap_down"]

        assert "1290.5" in gap_down  # ATR 止损价来自今天的盘面
        assert "减一半" in gap_down
        assert "缩量" in gap_down  # 量能只出现在条件里

    def test_hold_flat_open_references_ma20(self):
        plan = plan_positions([_row()])
        flat = plan["positions"][0]["branches"]["flat"]

        assert "1340" in flat
        assert "减" in flat

    def test_take_profit_branch_is_about_locking_gains(self):
        plan = plan_positions([_row(stance="take", label="止盈区")])
        gap_up = plan["positions"][0]["branches"]["gap_up"]

        assert "止盈" in gap_up
        assert "1420" in gap_up  # 20 日高

    def test_exit_stance_does_not_hesitate(self):
        """离场档不看量能手软：低开直接减半，收盘仍低清仓。"""
        plan = plan_positions([_row(stance="exit", label="离场")])
        gap_down = plan["positions"][0]["branches"]["gap_down"]

        assert "减半" in gap_down
        assert "清仓" in gap_down

    def test_missing_price_degrades_to_discipline_line(self):
        """没有 MA20 也没关系：动作降级，不编造价位。"""
        row = _row()
        row["levels"] = {"cost": 1500.0, "close": 1345.0}
        plan = plan_positions([row])

        branches = plan["positions"][0]["branches"]
        assert all(text.strip() for text in branches.values())


class TestOverall:
    def test_worst_value_is_half_for_hold(self):
        plan = plan_positions([_row()])
        overall = plan["overall"]

        assert overall["current_value"] == 134500.0
        assert overall["worst_value"] == 67250.0
        assert overall["worst_drawdown_pct"] == -50.0

    def test_exit_worst_case_is_zero(self):
        plan = plan_positions([_row(stance="exit")])
        overall = plan["overall"]

        assert overall["worst_value"] == 0.0
        assert overall["worst_drawdown_pct"] == -100.0

    def test_near_stop_flag_within_five_pct(self):
        plan = plan_positions([_row()])
        assert plan["positions"][0]["near_stop"] is True

        plan = plan_positions([_row(close=1400.0)])
        assert plan["positions"][0]["near_stop"] is False

    def test_no_cash_number_is_invented(self):
        """没有现金数据 → 整体只有市值区间，没有「总仓位几成」。"""
        plan = plan_positions([_row()])

        assert "总仓位" not in str(plan["overall"])
        assert "留几成" in plan["note"] or "市值" in plan["note"]


class TestEdgeCases:
    def test_unpriced_rows_are_skipped(self):
        plan = plan_positions([_row(close=None, market_value=None)])

        assert plan["positions"] == []
        assert plan["overall"]["n_managed"] == 0

    def test_volume_note_buckets(self):
        assert "放量" in plan_positions([_row(volume_ratio=1.8)])["positions"][0]["volume_note"]
        assert "缩量" in plan_positions([_row(volume_ratio=0.5)])["positions"][0]["volume_note"]
        assert "中性" in plan_positions([_row(volume_ratio=1.0)])["positions"][0]["volume_note"]
        assert "未知" in plan_positions([_row(volume_ratio=None)])["positions"][0]["volume_note"]
