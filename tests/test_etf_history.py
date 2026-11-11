"""ETF 历史切片：蜡烛、均线、成交额与区间统计。

守住三条：统计口径算对（涨跌/波动/回撤）、暖机期的均线给 None 而不是
0（0 会被画成一条贴地的假线）、缺缓存如实说「先采集」而不是空图。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data import etf


def _frame(rows: int = 80, start: float = 1.0, end: float = 1.5) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=rows)
    close = np.linspace(start, end, rows)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1e6,
            "amount": 1e8,
        },
        index=index,
    )


class TestHistory:
    def test_window_slices_candles_and_computes_stats(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(80))

        data = etf.history(tmp_path, "510300.SH", days=30)

        assert data["symbol"] == "510300"
        assert data["name"] == "沪深300ETF"
        assert len(data["candles"]) == 30
        assert data["stats"]["rows"] == 80  # 落盘总行数
        assert data["stats"]["window_rows"] == 30
        window = _frame(80)["close"].iloc[-30:]
        assert data["stats"]["change"] == pytest.approx(
            window.iloc[-1] / window.iloc[0] - 1, abs=1e-4
        )
        assert data["stats"]["last_close"] == pytest.approx(float(window.iloc[-1]), abs=1e-3)

    def test_moving_averages_are_none_during_warmup(self, tmp_path):
        """均线暖机期必须是 None：0 会被画成贴地的假线。"""
        etf.store(tmp_path, "510300", _frame(80))

        data = etf.history(tmp_path, "510300", days=30)

        values = [point[1] for point in data["ma20"]]
        assert values[:19] == [None] * 19
        assert all(value is not None and value > 0 for value in values[19:])
        assert all(point[1] is None for point in data["ma60"])  # 窗口不够 60 日

    def test_max_drawdown_matches_the_series(self, tmp_path):
        """人造一个回撤：先涨到 2.0 再跌到 1.2，回撤应是 -40%。"""
        index = pd.bdate_range("2024-01-01", periods=60)
        close = np.concatenate([np.linspace(1.0, 2.0, 30), np.linspace(2.0, 1.2, 30)])
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1e6,
                "amount": 1e8,
            },
            index=index,
        )
        etf.store(tmp_path, "510300", frame)

        data = etf.history(tmp_path, "510300", days=60)

        assert data["stats"]["max_drawdown"] == pytest.approx(-0.4, abs=1e-3)
        assert data["stats"]["high"]["value"] == pytest.approx(2.0, abs=1e-3)
        assert data["stats"]["high"]["date"] == str(index[29].date())

    def test_amount_is_reported_in_yi(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(80))

        data = etf.history(tmp_path, "510300", days=30)

        assert data["amounts"][-1][1] == pytest.approx(1.0)  # 1e8 → 1 亿
        assert data["stats"]["avg_amount_20"] == pytest.approx(1.0, abs=0.05)

    def test_missing_cache_says_how_to_fix_it(self, tmp_path):
        with pytest.raises(ValueError, match="刷新"):
            etf.history(tmp_path, "510300")

    def test_share_block_is_honest_when_only_one_snapshot(self, tmp_path):
        """只有一份份额快照：share_change 为空，不编造变化。"""
        etf.store(tmp_path, "510300", _frame(80))
        etf.store_spot(tmp_path, {"510300": {"share": 3e8, "mktcap": 4e10}}, "2026-11-10")

        data = etf.history(tmp_path, "510300", days=30)

        assert data["share"]["share"] == pytest.approx(3e8)
        assert data["share"]["share_change"] is None


class TestHistoryAPI:
    def test_unknown_etf_is_404_with_guidance(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())

        response = TestClient(create_app()).get("/api/etf/510300/history")

        assert response.status_code == 404
        assert "刷新" in response.json()["detail"]

    def test_payload_shape(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        etf.store(tmp_path, "510300", _frame(80))

        payload = TestClient(create_app()).get("/api/etf/510300/history?days=40").json()

        assert payload["days"] == 40
        assert len(payload["candles"]) == 40
        assert payload["candles"][0][0] < payload["candles"][-1][0]  # 日期递增
        assert all(len(point) == 5 for point in payload["candles"])  # date/o/h/l/c
