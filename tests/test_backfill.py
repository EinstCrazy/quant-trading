"""一键补采：持仓 ∪ 自选里缺面板数据的票，一次补齐。

「缺什么补什么」是这条端点的全部价值：已有数据的不碰（不重采、不
覆写）、股票走面板采集、ETF 走 ETF 采集（ETF 独立落盘，不拿股票
面板判缺）、单只失败不拖垮整批——失败的票如实留在 failed 里，不让
「补齐了」变成一句自我安慰。
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data.loader import PricePanel


def _panel() -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=30)
    return PricePanel(prices=pd.DataFrame({"600519.SH": 1500.0}, index=index))


def _settings(tmp_path):
    class _Settings:
        class data:
            directory = tmp_path

        class collection:
            provider = "akshare"
            adjust = "qfq"
            lookback_days = 60
            provider_options: dict = {}  # noqa: RUF012

        class indicators:
            output_directory = tmp_path / "indicators"
            format = "parquet"

        class _BacktestSettings:
            initial_cash = 100000.0
            commission_bps = 5.0
            slippage_bps = 2.0
            allow_short = False

        backtest = _BacktestSettings()

    return _Settings()


@pytest.fixture()
def bf_client(tmp_path, monkeypatch):
    from quant_research import holdings as holdings_store
    from quant_research import watchlist as watchlist_store
    from quant_research.web import api as api_module
    from quant_research.web import create_app

    monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _settings(tmp_path))
    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: _panel())
    holdings_store.upsert(tmp_path, "600519.SH", shares=100, cost=1400)
    holdings_store.upsert(tmp_path, "600435.SH", shares=200, cost=12)
    watchlist_store.add_stock(tmp_path, "000559.SZ", name="测试")
    client = TestClient(create_app())
    client._tmp = tmp_path
    return client


def test_backfills_only_missing_symbols(bf_client, monkeypatch):
    """600519 在面板不碰；600435/000559 缺 → 都要补。"""
    from quant_research.web import api as api_module

    requested: list[str] = []

    def fake_collect(request, **kwargs):
        requested.extend(request.symbols)
        return {
            "updated": {s: 100 for s in request.symbols},
            "added": {s: 100 for s in request.symbols},
            "failed": {},
            "skipped": {},
        }

    monkeypatch.setattr(api_module, "_collect_report", fake_collect)
    payload = bf_client.post("/api/portfolio/backfill").json()

    assert sorted(requested) == ["000559.SZ", "600435.SH"]
    assert set(payload["updated"]) == {"000559.SZ", "600435.SH"}
    assert payload["skipped"] == ["600519.SH"]


def test_reports_per_symbol_failures(bf_client, monkeypatch):
    """单只失败不拖垮：000559 挂了不影响 600435 入库。"""
    from quant_research.web import api as api_module

    def fake_collect(request, **kwargs):
        updated = {s: 100 for s in request.symbols if s != "000559.SZ"}
        failed = {s: "下载失败" for s in request.symbols if s == "000559.SZ"}
        return {"updated": updated, "added": updated, "failed": failed, "skipped": {}}

    monkeypatch.setattr(api_module, "_collect_report", fake_collect)
    payload = bf_client.post("/api/portfolio/backfill").json()

    assert "600435.SH" in payload["updated"]
    assert "000559.SZ" in payload["failed"]
    assert "失败 1" in payload["note"]


def test_nothing_missing_is_said_plainly(tmp_path, monkeypatch):
    """全部都有数据：如实说「都齐了」，不空跑一次采集。"""
    from quant_research import holdings as holdings_store
    from quant_research.web import api as api_module
    from quant_research.web import create_app

    index = pd.bdate_range("2024-01-01", periods=30)
    panel = PricePanel(prices=pd.DataFrame({"600519.SH": 1500.0, "600435.SH": 12.0}, index=index))
    monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _settings(tmp_path))
    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)
    holdings_store.upsert(tmp_path, "600519.SH", shares=100, cost=1400)
    holdings_store.upsert(tmp_path, "600435.SH", shares=200, cost=12)

    def no_collect(request, **kwargs):
        raise AssertionError("没有缺的票却调了采集")

    monkeypatch.setattr(api_module, "_collect_report", no_collect)
    payload = TestClient(create_app()).post("/api/portfolio/backfill").json()

    assert payload["updated"] == []
    assert "都齐了" in payload["note"]


def test_empty_book_is_refused(tmp_path, monkeypatch):
    from quant_research.web import api as api_module
    from quant_research.web import create_app

    monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _settings(tmp_path))
    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: _panel())

    response = TestClient(create_app()).post("/api/portfolio/backfill")

    assert response.status_code == 400
