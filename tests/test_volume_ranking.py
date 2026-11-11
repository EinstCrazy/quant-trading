"""全样本量比排行：口径与 volume._window_stats 一致（shift(1) 基线）。

排行端点的诚实约定：缺成交额退回成交量并标注；不足 21 天如实拒绝
（不编一个基线）；ETF 不在股票样本排行里——它们在 ETF 视图有自己的
量比，混排会让人误以为是同一口径的同一批标的。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data.loader import PricePanel


def _panel(rows: int = 25, symbols: int = 4) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=rows)
    amounts = np.full((rows, symbols), 1e6)
    amounts[-1, 0] = 3e6  # S0 放量 3 倍
    amounts[-1, 1] = 0.3e6  # S1 缩量到 0.3
    amount = pd.DataFrame(amounts, index=index, columns=[f"S{step}" for step in range(symbols)])
    closes = np.linspace(1.0, 1.1, rows)[:, None] * np.ones(symbols)
    prices = pd.DataFrame(closes, index=index, columns=[f"S{step}" for step in range(symbols)])
    return PricePanel(prices=prices, extra={"amount": amount, "volume": amount / 10})


class TestVolumeRankingAPI:
    @pytest.fixture()
    def vr_client(self, tmp_path, monkeypatch) -> TestClient:
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: _panel())
        monkeypatch.setattr(
            api_module.universe,
            "load",
            lambda directory: pd.DataFrame({"symbol": [], "name": []}),
        )
        return TestClient(create_app())

    def test_ranking_uses_the_shifted_baseline(self, vr_client):
        payload = vr_client.get("/api/volume/ranking").json()

        assert payload["available"] is True
        assert payload["metric"] == "amount"
        top = payload["top_in"][0]
        bottom = payload["top_out"][0]
        assert top["symbol"] == "S0" and top["vol_ratio"] == pytest.approx(3.0)
        assert bottom["symbol"] == "S1" and bottom["vol_ratio"] == pytest.approx(0.3)

    def test_day_change_accompanies_every_row(self, vr_client):
        payload = vr_client.get("/api/volume/ranking").json()

        for row in payload["top_in"] + payload["top_out"]:
            assert row["day_change"] is not None

    def test_short_history_is_refused_not_invented(self, tmp_path, monkeypatch):
        """不到 21 天：没有基线可算，如实拒绝而不是编排行。"""
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: _panel(rows=10))

        client = TestClient(create_app())
        payload = client.get("/api/volume/ranking").json()

        assert payload["available"] is False
        assert "21 天" in payload["reason"]
