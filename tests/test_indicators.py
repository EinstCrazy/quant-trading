"""自定义指标：算术 DSL 派生指标，定义顺序敏感，除零归「不知道」。

用户输入不进 eval——tokenize + 递归下降，错误带字符位置。定义只能
引用内置指标或更早定义的自定义指标（不许前向引用，顺序敏感）；
除以零的 inf 归为 NaN——「算不出来」在比较里不触发，与「窗口不足
为不知道」同语义，不编 0。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.strategies.custom import validate_expr
from quant_research.strategies.indicators import (
    IndicatorRegistry,
    evaluate_indicator,
    validate_indicator_expr,
    validate_name,
)


class TestGrammar:
    def test_arithmetic_expressions_pass(self):
        available = {"close": "d", "ma20": "d", "high20": "d", "low20": "d"}
        for expr in [
            "(close - ma20) / ma20 * 100",
            "close / (high20 - low20)",
            "close + 1.5",
            "ma20 * 1.05",
        ]:
            assert validate_indicator_expr(expr, available) == [], expr

    def test_unknown_metric_is_named(self):
        errors = validate_indicator_expr("ma50 + 1", {"close": "d"})

        assert "ma50" in errors[0].message

    def test_comparison_operators_are_not_arithmetic(self):
        errors = validate_indicator_expr("close > ma20", {"close": "d", "ma20": "d"})

        assert errors  # 比较是策略层的事，指标定义里不出现

    def test_unbalanced_paren_is_reported(self):
        errors = validate_indicator_expr("(close - ma20", {"close": "d", "ma20": "d"})

        assert any("括号" in e.message for e in errors)

    def test_name_rules(self):
        assert validate_name("bias") is None
        assert "撞名" in (validate_name("ma20") or "")
        assert "保留字" in (validate_name("AND") or "")
        assert validate_name("9x") is not None
        assert validate_name("") is not None


class TestEvaluation:
    def test_metric_against_metric_and_constants(self):
        index = pd.bdate_range("2024-01-01", periods=30)
        close = pd.DataFrame(10.0, index=index, columns=["S0"])
        metrics = {"close": close, "ma20": close * 1.1}

        bias = evaluate_indicator("(close - ma20) / ma20 * 100", metrics)

        assert bias.iloc[0, 0] == pytest.approx(-100 / 11)

    def test_division_by_zero_is_nan_not_inf(self):
        """除零归「不知道」（NaN）——inf 在比较里是假信号，不编。"""
        index = pd.bdate_range("2024-01-01", periods=5)
        close = pd.DataFrame(10.0, index=index, columns=["S0"])
        metrics = {"close": close, "flat": close * 0}

        result = evaluate_indicator("close / flat", metrics)

        assert bool(pd.isna(result.iloc[0, 0]))


class TestRegistry:
    def test_definition_is_order_sensitive(self, tmp_path):
        """只能引用内置与更早定义——前向引用如实拒绝。"""
        registry = IndicatorRegistry(tmp_path / "indicators.json")

        first = registry.define("bias", "(close - ma20) / ma20 * 100", "乖离率%")
        second = registry.define("double", "bias * 2", "乖离率翻倍")
        forward = registry.define("early", "later + 1", "引用还没定义的")

        assert first["ok"] and second["ok"]
        assert not forward["ok"]

    def test_redefine_cannot_reference_itself(self, tmp_path):
        registry = IndicatorRegistry(tmp_path / "indicators.json")
        registry.define("bias", "(close - ma20) / ma20 * 100", "乖离率%")

        result = registry.define("bias", "bias + 1", "自引用")

        assert not result["ok"]

    def test_name_clash_with_builtin_is_refused(self, tmp_path):
        registry = IndicatorRegistry(tmp_path / "indicators.json")

        result = registry.define("ma20", "close * 2", "撞名")

        assert not result["ok"]

    def test_persistence_round_trip(self, tmp_path):
        registry = IndicatorRegistry(tmp_path / "indicators.json")
        registry.define("bias", "close - ma20", "乖离")

        reloaded = IndicatorRegistry(tmp_path / "indicators.json")

        assert [item["name"] for item in reloaded.list()] == ["bias"]

    def test_delete_missing_is_false(self, tmp_path):
        registry = IndicatorRegistry(tmp_path / "indicators.json")

        assert registry.remove("ghost") is False

    def test_evaluate_all_chains_definitions_in_order(self, tmp_path):
        registry = IndicatorRegistry(tmp_path / "indicators.json")
        registry.define("bias", "(close - ma20) / ma20 * 100", "乖离率%")
        registry.define("double", "bias * 2", "翻倍")
        index = pd.bdate_range("2024-01-01", periods=30)
        close = pd.DataFrame(10.0, index=index, columns=["S0"])
        metrics = {"close": close, "ma20": close * 1.1}

        values = registry.evaluate_all(metrics)

        assert values["double"].iloc[0, 0] == pytest.approx(values["bias"].iloc[0, 0] * 2)


class TestStrategyIntegration:
    def test_strategy_expression_can_use_custom_indicators(self):
        """定义乖离率指标后，策略表达式里直接引用——按定义顺序求值。"""
        from quant_research.data.loader import PricePanel
        from quant_research.strategies import get_signal

        index = pd.bdate_range("2024-01-01", periods=70)
        base = 10.0 - 0.005 * np.arange(70)
        prices = pd.DataFrame({"S0": base}, index=index)
        prices.iloc[-3] = prices.iloc[-4] * 1.6  # 跳升 → 乖离率飙升
        prices.iloc[-2] = prices.iloc[-3] * 1.02
        prices.iloc[-1] = prices.iloc[-2] * 1.03
        amount = pd.DataFrame(1e6, index=index, columns=["S0"])
        amount.iloc[-3] = 3e6
        panel = PricePanel(prices=prices, extra={"amount": amount})

        indicators = [{"name": "bias", "expr": "(close - ma20) / ma20 * 100", "desc": "乖离率%"}]
        signal = get_signal("custom")(
            panel.prices, amount=amount, expr="bias > 5", indicators=indicators
        )

        assert signal.iloc[-1].iloc[0] == 1  # 跳升日乖离率超阈值触发
        assert signal.iloc[:-3].to_numpy().sum() == 0  # 阴跌段乖离为负不触发

    def test_strategy_expression_rejects_undefined_indicator(self):
        """指标没了（被删）语法检测如实点名——文本引用不追踪，靠检测兜底。"""
        errors = validate_expr("bias > 5")

        assert any("bias" in e.message for e in errors)


class TestIndicatorsAPI:
    @pytest.fixture()
    def ci_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

            class indicators:
                output_directory = tmp_path / "indicators"
                format = "parquet"

            class _BacktestSettings:
                initial_cash = 100000.0
                commission_bps = 5.0
                slippage_bps = 2.0
                allow_short = False

            backtest = _BacktestSettings()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_define_list_delete_round_trip(self, ci_client):
        payload = ci_client.post(
            "/api/strategies/custom/indicators",
            json={"name": "bias", "expr": "(close - ma20) / ma20 * 100", "desc": "乖离率%"},
        ).json()

        assert payload["ok"] is True
        assert (
            ci_client.get("/api/strategies/custom/indicators").json()["custom"][0]["name"] == "bias"
        )
        removed = ci_client.delete("/api/strategies/custom/indicators/bias")
        assert removed.status_code == 200
        assert ci_client.get("/api/strategies/custom/indicators").json()["custom"] == []

    def test_define_refuses_bad_name_with_reason(self, ci_client):
        payload = ci_client.post(
            "/api/strategies/custom/indicators",
            json={"name": "ma20", "expr": "close * 2", "desc": "撞名"},
        )

        assert payload.status_code == 400
        assert "撞名" in payload.json()["detail"]

    def test_define_refuses_unknown_reference(self, ci_client):
        payload = ci_client.post(
            "/api/strategies/custom/indicators",
            json={"name": "x", "expr": "ma50 + 1", "desc": ""},
        )

        assert payload.status_code == 400 and "ma50" in payload.json()["detail"]

    def test_delete_missing_is_404(self, ci_client):
        assert ci_client.delete("/api/strategies/custom/indicators/ghost").status_code == 404

    def test_strategy_validate_accepts_defined_indicator(self, ci_client):
        ci_client.post(
            "/api/strategies/custom/indicators",
            json={"name": "bias", "expr": "(close - ma20) / ma20 * 100", "desc": "乖离率%"},
        )

        payload = ci_client.post(
            "/api/strategies/custom/validate",
            json={"expr": "bias > 5"},
        ).json()

        assert payload["ok"] is True

    def test_spec_lists_custom_indicators(self, ci_client):
        ci_client.post(
            "/api/strategies/custom/indicators",
            json={"name": "bias", "expr": "close - ma20", "desc": "乖离"},
        )

        payload = ci_client.get("/api/strategies/custom/spec").json()

        assert "bias" in payload["metrics"] and payload["custom_indicators"]
