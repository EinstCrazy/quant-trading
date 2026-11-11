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


class TestCompare:
    """多只对比：先对齐再归一化，画面与统计必须同一段数据。"""

    def test_curves_start_at_100_and_share_dates(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(80, 1.0, 1.5))
        etf.store(tmp_path, "518880", _frame(80, 7.0, 6.3))  # 量级差 7 倍、方向相反

        data = etf.compare(tmp_path, ["510300", "518880"], days=40)

        assert data["days"] == 40
        for item in data["series"]:
            assert item["points"][0][1] == pytest.approx(100.0, abs=0.01)
            assert len(item["points"]) == 40
        # 同一组日期：不齐就会画出假交叉
        assert [p[0] for p in data["series"][0]["points"]] == [
            p[0] for p in data["series"][1]["points"]
        ]

    def test_change_matches_the_normalised_curve_end(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(60, 1.0, 1.2))
        etf.store(tmp_path, "518880", _frame(60, 7.0, 6.3))

        data = etf.compare(tmp_path, ["510300", "518880"], days=60)

        for row, item in zip(data["stats"], data["series"], strict=True):
            assert item["points"][-1][1] == pytest.approx(100 * (1 + row["change"]), abs=0.05)

    def test_only_common_trading_days_are_compared(self, tmp_path):
        """一只早上市、一只晚上市：共同日期才是可比的窗口。"""
        etf.store(tmp_path, "510300", _frame(80))
        short = _frame(80).iloc[-40:]
        etf.store(tmp_path, "518880", short)

        data = etf.compare(tmp_path, ["510300", "518880"], days=250)

        assert data["days"] == 40
        assert data["stats"][0]["rows"] == 40

    def test_missing_is_reported_not_fatal(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(60))
        etf.store(tmp_path, "518880", _frame(60))

        data = etf.compare(tmp_path, ["510300", "518880", "515000"], days=30)

        assert len(data["series"]) == 2
        assert any("515000" in item for item in data["missing"])

    def test_correlation_matrix_is_symmetric_with_unit_diagonal(self, tmp_path):
        etf.store(tmp_path, "510300", _frame(80))
        etf.store(tmp_path, "518880", _frame(80, 7.0, 6.3))

        data = etf.compare(tmp_path, ["510300", "518880"], days=60)

        matrix = data["correlation"]["matrix"]
        assert matrix[0][0] == pytest.approx(1.0)
        assert matrix[1][1] == pytest.approx(1.0)
        assert matrix[0][1] == pytest.approx(matrix[1][0])

    def test_all_missing_says_how_to_fix_it(self, tmp_path):
        with pytest.raises(ValueError, match="刷新"):
            etf.compare(tmp_path, ["510300", "518880"])

    def test_api_rejects_too_many_curves(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())

        response = TestClient(create_app()).get(
            "/api/etf/compare?symbols=510300,518880,513100,513500,510050,510500,512880"
        )

        assert response.status_code == 400
        assert "6 只" in response.json()["detail"]


class TestRefreshOne:
    """详情页「更新这只」：单只联网采集，失败如实点名。

    图与统计读缓存，采集只在用户点按钮时发生——这个端点就是那个按钮
    的后端。不联网跑测试：monkeypatch 掉 refresh，只验契约。
    """

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_single_refresh_reports_rows_and_snapshot(self, client, monkeypatch):
        calls: list[tuple[str, str]] = []

        def fake_refresh(root, symbol, adjust="qfq"):
            calls.append((symbol, adjust))
            frame = _frame(50)
            etf.store(root, symbol, frame)
            return frame

        monkeypatch.setattr(etf, "refresh", fake_refresh)

        payload = client.post("/api/etf/510300/refresh").json()

        assert calls == [("510300", "qfq")]
        assert payload["symbol"] == "510300"
        assert payload["name"] == "沪深300ETF"
        assert payload["rows"] == 50
        assert payload["as_of"]
        # 顺手带回快照：前端不用再打一次 /etf/snapshot
        assert any(row["symbol"] == "510300" for row in payload["snapshot"]["rows"])

    def test_failure_is_400_with_the_reason(self, client, monkeypatch):
        def boom(root, symbol, adjust="qfq"):
            raise RuntimeError("东财超时")

        monkeypatch.setattr(etf, "refresh", boom)

        response = client.post("/api/etf/515000/refresh")

        assert response.status_code == 400
        assert "515000" in response.json()["detail"]
        assert "东财超时" in response.json()["detail"]

    def test_symbol_with_suffix_is_normalised(self, client, monkeypatch):
        seen: list[str] = []

        def fake_refresh(root, symbol, adjust="qfq"):
            seen.append(symbol)
            frame = _frame(30)
            etf.store(root, symbol, frame)
            return frame

        monkeypatch.setattr(etf, "refresh", fake_refresh)

        payload = client.post("/api/etf/510300.SH/refresh").json()

        assert seen == ["510300"]
        assert payload["symbol"] == "510300"
