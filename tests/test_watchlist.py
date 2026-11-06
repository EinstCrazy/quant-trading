"""自选股与分类：用户标注的集合，不是行情的缓存。

要守住的三条：代码归一化后才入库；删分类连引用一起摘，不留悬空标签；
API 层不存价格——涨跌每次从面板现读，面板不在就留空而不是编一个数。
"""

from __future__ import annotations

import pytest

# --- 存储层 -------------------------------------------------------------------


class TestStore:
    def test_add_normalises_the_symbol(self, tmp_path):
        from quant_research.watchlist import add_stock, load, path

        add_stock(tmp_path, "600519", categories=["白酒"])
        data = load(tmp_path)

        assert "600519.SH" in data["stocks"]
        assert data["stocks"]["600519.SH"]["categories"] == ["白酒"]
        assert path(tmp_path).is_file()

    def test_upsert_keeps_note_and_added_at(self, tmp_path):
        from quant_research.watchlist import add_stock, load

        add_stock(tmp_path, "600519.SH", categories=["白酒"])
        first = load(tmp_path)["stocks"]["600519.SH"]

        add_stock(
            tmp_path, "600519.SH", name="贵州茅台", categories=["白酒", "核心"], note="观察估值"
        )

        updated = load(tmp_path)["stocks"]["600519.SH"]
        assert updated["name"] == "贵州茅台"
        assert updated["note"] == "观察估值"
        assert updated["added_at"] == first["added_at"]
        assert updated["categories"] == ["白酒", "核心"]

    def test_removing_a_category_takes_the_references_with_it(self, tmp_path):
        """A deleted group must not linger as a tag on stocks it once held."""
        from quant_research.watchlist import add_stock, load, remove_category

        add_stock(tmp_path, "600519.SH", categories=["白酒", "核心"])
        add_stock(tmp_path, "000858.SZ", categories=["白酒"])

        data = remove_category(tmp_path, "白酒")

        assert data["categories"] == ["核心"]
        assert all("白酒" not in s["categories"] for s in data["stocks"].values())
        assert load(tmp_path)["stocks"]["600519.SH"]["categories"] == ["核心"]

    def test_a_corrupt_file_reads_as_empty_not_as_a_crash(self, tmp_path):
        from quant_research.watchlist import empty, load

        (tmp_path / "watchlist.json").write_text("{not json", encoding="utf-8")

        assert load(tmp_path) == empty()

    def test_remove_an_absent_stock_is_a_noop(self, tmp_path):
        from quant_research.watchlist import empty, remove_stock

        assert remove_stock(tmp_path, "600519.SH") == empty()


# --- API 层 -------------------------------------------------------------------


@pytest.fixture()
def wl_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import api as api_module
    from quant_research.web import create_app

    class _Collection:
        provider = "baostock"
        adjust = "qfq"
        output_directory = tmp_path / "raw"

    class _Data:
        directory = tmp_path / "data"

    class _Settings:
        collection = _Collection()
        data = _Data()

    settings = _Settings()
    monkeypatch.setattr(api_module, "load_settings", lambda *args, **kwargs: settings)
    return TestClient(create_app())


class TestAPI:
    def test_add_then_get_returns_the_stock(self, wl_client):
        added = wl_client.post("/api/watchlist", json={"symbol": "600519", "categories": ["白酒"]})
        assert added.status_code == 200

        payload = wl_client.get("/api/watchlist").json()
        assert [s["symbol"] for s in payload["stocks"]] == ["600519.SH"]
        assert payload["categories"] == ["白酒"]
        # 没有行情数据时不编造报价
        assert payload["stocks"][0]["last_close"] is None
        assert payload["stocks"][0]["change_pct"] is None

    def test_unknown_code_is_a_clear_400(self, wl_client):
        response = wl_client.post("/api/watchlist", json={"symbol": "999999.HZ"})
        assert response.status_code == 422

    def test_remove_stock(self, wl_client):
        wl_client.post("/api/watchlist", json={"symbol": "600519.SH"})

        payload = wl_client.delete("/api/watchlist/600519.SH").json()

        assert payload["stocks"] == []

    def test_category_lifecycle(self, wl_client):
        wl_client.post("/api/watchlist/categories", json={"name": "科技"})

        payload = wl_client.delete("/api/watchlist/categories/科技").json()

        assert payload["categories"] == []

    def test_blank_category_name_is_rejected(self, wl_client):
        assert wl_client.post("/api/watchlist/categories", json={"name": "  "}).status_code == 422


# --- 分析与大盘 ---------------------------------------------------------------


class _Indicators:
    """params_from_settings 所需的完整 indicators 段。"""

    ma_periods = (5, 20)
    ema_periods = (12, 26)
    macd_fast = 12
    macd_slow = 26
    macd_signal = 9
    rsi_period = 14
    boll_period = 20
    boll_std = 2.0
    kdj_period = 9
    kdj_smooth = 3
    atr_period = 14
    williams_period = 14
    cci_period = 14
    adx_period = 14
    volatility_period = 20


class _S:
    indicators = _Indicators()


def _mock_research(wl_client, monkeypatch):
    """一只确定性的三票面板 + 由它算出的指标束。"""
    import numpy as np
    import pandas as pd

    from quant_research.data.loader import PricePanel
    from quant_research.indicators import compute_all, params_from_settings
    from quant_research.web import api as api_module

    index = pd.bdate_range("2024-01-01", periods=40, name="date")
    rng = np.random.default_rng(3)
    prices = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, (40, 3)), axis=0),
        index=index,
        columns=["600000.SH", "000001.SZ", "300001.SZ"],
    )
    panel = PricePanel(
        prices=prices,
        extra={
            k: prices * f
            for k, f in (
                ("open", 0.999),
                ("high", 1.002),
                ("low", 0.998),
                ("volume", 1000.0),
                ("amount", 1e6),
            )
        },
    )
    bundle = compute_all(panel, params_from_settings(_S()))
    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)
    monkeypatch.setattr(api_module, "_load_indicators", lambda *a, **k: bundle)
    return panel


class TestAnalysis:
    def test_breadth_and_cross_sections(self, wl_client, monkeypatch):
        _mock_research(wl_client, monkeypatch)
        wl_client.post("/api/watchlist", json={"symbol": "600000.SH"})

        payload = wl_client.get("/api/watchlist/analysis").json()

        market = payload["market"]
        assert market["n_symbols"] == 3
        assert set(market) >= {
            "up",
            "down",
            "flat",
            "median_change",
            "above_ma20_ratio",
            "new_high_20d",
            "new_low_20d",
        }
        # 等权指数归一化到首日 = 100
        assert len(payload["market_curve"]) == 40
        assert payload["market_curve"][0][1] == 100.0

        stock = payload["stocks"][0]
        assert stock["symbol"] == "600000.SH"
        assert stock["last_close"] is not None
        assert stock["rsi_14"] is not None
        assert stock["ma20_position"] in ("above", "below")
        assert payload["summary"]["watch_count"] == 1

    def test_the_market_survives_an_empty_watchlist(self, wl_client, monkeypatch):
        """大盘卡不该等人先加自选——空表时宽度照算，自选列为空。"""
        _mock_research(wl_client, monkeypatch)

        payload = wl_client.get("/api/watchlist/analysis").json()

        assert payload["market"]["n_symbols"] == 3
        assert payload["stocks"] == []
        assert payload["summary"] is None

    def test_without_stored_data_it_says_why_not(self, wl_client):
        payload = wl_client.get("/api/watchlist/analysis").json()

        assert payload["market"] is None
        assert "落盘" in payload["unavailable"]


# --- 板块关联与异动 ------------------------------------------------------------


class TestSectorMovers:
    def _write_sectors(self, wl_client):
        """板块表写进 fixture 的 data 目录，API 从同一目录读。"""
        from quant_research.data.universe import save_sectors
        from quant_research.web import api as api_module

        settings = api_module.load_settings()
        save_sectors(
            {"600000.SH": "银行", "000001.SZ": "银行", "300001.SZ": "半导体"},
            settings.data.directory,
        )

    def test_movers_rank_relative_moves(self, wl_client, monkeypatch):
        _mock_research(wl_client, monkeypatch)
        self._write_sectors(wl_client)

        payload = wl_client.get("/api/sectors/movers?days=5").json()

        assert payload["n_sectors"] == 2
        assert {s["sector"] for s in payload["leading"]} <= {"银行", "半导体"}
        assert all("change" in s and "up_ratio" in s for s in payload["all"])
        # 领涨是相对排名的两端，不与全体中位混为一谈
        changes = [s["change"] for s in payload["all"]]
        assert payload["leading"][0]["change"] == max(changes)

    def test_missing_sector_table_is_a_clear_400(self, wl_client, monkeypatch):
        _mock_research(wl_client, monkeypatch)

        response = wl_client.get("/api/sectors/movers")

        assert response.status_code == 400
        assert "--sectors" in response.json()["detail"]

    def test_watchlist_carries_the_sector(self, wl_client, monkeypatch):
        _mock_research(wl_client, monkeypatch)
        self._write_sectors(wl_client)
        wl_client.post("/api/watchlist", json={"symbol": "600000.SH"})

        payload = wl_client.get("/api/watchlist").json()

        assert payload["stocks"][0]["sector"] == "银行"

    def test_detail_carries_the_sector(self, wl_client, monkeypatch):
        _mock_research(wl_client, monkeypatch)
        self._write_sectors(wl_client)

        payload = wl_client.get("/api/indicators/600000").json()

        assert payload["sector"] == "银行"
