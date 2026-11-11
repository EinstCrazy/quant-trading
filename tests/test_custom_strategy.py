"""自定义策略 DSL：语法检测先挡错，回测只回答「本样本轮廓」。

用户的表达式进不了 eval——那是注入面。tokenize + 递归下降，每类
错误（未知指标、悬挂运算符、括号不配对、缺连接词）逐条带字符位置
报告。语法检测不挡语义：「close > ma20 AND vol_ratio > 2」语法对但
可能从未触发，那是回测要如实回答的问题。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data.loader import PricePanel
from quant_research.strategies import get_signal
from quant_research.strategies.custom import validate_expr


def _panel(rows: int = 70, symbols: int = 2, with_amount: bool = True) -> PricePanel:
    """阴跌横盘 + 倒数第二日跳升的确定性面板：跳升前不创新高，
    跳升后留最后一天让等权回测有持仓收益可算。"""
    index = pd.bdate_range("2024-01-01", periods=rows)
    base = 10.0 - 0.005 * np.arange(rows)  # 缓慢阴跌：收盘始终低于前 20 日高，不触发突破
    prices = pd.DataFrame(
        np.tile(base[:, None], symbols),
        index=index,
        columns=[f"S{i}" for i in range(symbols)],
    )
    prices.iloc[-3] = prices.iloc[-4] * 1.6  # 倒数第三日跳升：突破全部与前高
    prices.iloc[-2] = prices.iloc[-3] * 1.02  # 跳升后小涨：入场后有持仓收益可算
    prices.iloc[-1] = prices.iloc[-2] * 1.03
    extra = {
        "amount": pd.DataFrame(np.full((rows, symbols), 1e6), index=index, columns=prices.columns)
    }
    extra["amount"].iloc[-3] = 3e6  # 跳升日放量 3 倍
    if not with_amount:
        extra = {}
    return PricePanel(prices=prices, extra=extra)


class TestGrammar:
    def test_valid_expressions_pass(self):
        for expr in [
            "close > ma20",
            "close > ma20 AND vol_ratio > 1.5",
            "close > high20 OR (chg > 5 AND vol_ratio > 2)",
            "close < low20 AND chg < -2",  # 负数
            "close > ma60 and chg > 0",  # 小写逻辑词
        ]:
            assert validate_expr(expr) == [], expr

    def test_unknown_metric_is_named(self):
        errors = validate_expr("ma50 > 1")

        assert len(errors) == 1
        assert "ma50" in errors[0].message and "close" in errors[0].message  # 列出可用指标

    def test_dangling_operator_is_reported_with_position(self):
        errors = validate_expr("close > ")

        assert errors and errors[0].pos >= 0

    def test_unbalanced_paren_is_reported(self):
        errors = validate_expr("(close > ma20")

        assert any("括号" in err.message for err in errors)

    def test_missing_connective_is_reported(self):
        errors = validate_expr("close > 1 close > 2")

        assert any("AND" in err.message for err in errors)

    def test_empty_expression_says_so(self):
        assert "空" in validate_expr("")[0].message

    def test_metric_against_metric_compares(self):
        assert validate_expr("close > ma20 AND chg > 0") == []


class TestSignalSemantics:
    def test_entry_then_exit_state_machine(self):
        """进场条件成立持牌、出场条件成立清仓——0/1 面板协议一致。"""
        signal = get_signal("custom")(
            _panel().prices,
            amount=_panel().extra["amount"],
            expr="close > high20",
            exit_expr="close < low20",
        )
        assert signal.iloc[-1].iloc[0] == 1  # 跳升后一直持有
        assert signal.iloc[:-3].to_numpy().sum() == 0  # 跳升前从未入场

    def test_no_exit_means_hold_to_the_end(self):
        """出场留空 = 持有到样本结束：如实语义，进回测 note。"""
        panel = _panel()
        signal = get_signal("custom")(
            panel.prices, amount=panel.extra["amount"], expr="close > high20"
        )

        assert signal.iloc[-1].all()

    def test_missing_amount_refuses_vol_ratio_honestly(self):
        """vol_ratio 要成交额：面板没有就拒绝执行，不静默放行量条件。"""
        panel = _panel(with_amount=False)

        with pytest.raises(ValueError, match="vol_ratio"):
            get_signal("custom")(panel.prices, expr="vol_ratio > 1")

    def test_syntactically_valid_but_never_triggered_is_a_zero_panel(self):
        """语法对但太严：全 0 信号面板——「未触发」由调用方如实报告。"""
        signal = get_signal("custom")(_panel().prices, expr="close > 999")

        assert not signal.to_numpy().any()


class TestCustomAPI:
    @pytest.fixture()
    def cs_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

            class _BacktestSettings:
                initial_cash = 100000.0
                commission_bps = 5.0
                slippage_bps = 2.0
                allow_short = False

            backtest = _BacktestSettings()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: _panel())
        return TestClient(create_app())

    def test_spec_lists_metrics_and_notes(self, cs_client):
        payload = cs_client.get("/api/strategies/custom/spec").json()

        assert "close" in payload["metrics"] and "vol_ratio" in payload["metrics"]
        assert any("出场" in note for note in payload["notes"])

    def test_validate_reports_errors_per_field(self, cs_client):
        payload = cs_client.post(
            "/api/strategies/custom/validate",
            json={"expr": "ma50 > 1", "exit_expr": "close > "},
        ).json()

        assert payload["ok"] is False
        fields = {error["field"] for error in payload["errors"]}
        assert fields == {"expr", "exit_expr"}

    def test_backtest_refuses_syntax_errors_before_running(self, cs_client):
        """语法错别浪费一次整面板回测：400 + 错误信息。"""
        payload = cs_client.post(
            "/api/strategies/custom/backtest",
            json={"expr": "close > "},
        )

        assert payload.status_code == 400

    def test_backtest_reports_the_sample_profile(self, cs_client):
        payload = cs_client.post(
            "/api/strategies/custom/backtest",
            json={"expr": "close > high20", "exit_expr": "close < low20"},
        ).json()

        assert payload["available"] is True
        assert payload["metrics"]["cumulative_return"] > 0  # 末日跳升的确定性轮廓
        assert payload["n_symbols_with_signal"] >= 1

    def test_backtest_says_when_never_triggered(self, cs_client):
        payload = cs_client.post(
            "/api/strategies/custom/backtest",
            json={"expr": "close > 999"},
        ).json()

        assert payload["available"] is False
        assert "从未触发" in payload["reason"]
