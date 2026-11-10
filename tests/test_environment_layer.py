"""指数日线、行业资金流、基本面红旗：环境与核对层的落盘与 API 约定。

守住的几条：指数与资金流 GET 只读缓存不联网（沿用外围快照的约定）；
资金流不与板块表做跨口径匹配——东财分类和证监会分类对不上，宁可独立
成卡；基本面「没查」和「没红旗」是两回事——季报缺失返回 400 或「未知」，
不编一个干净结论。
"""

from __future__ import annotations

import pathlib
from datetime import timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research import sector_flow as sector_flow_mod
from quant_research.data import index as index_mod
from quant_research.web import api as api_module


def _make_client(tmp_path, monkeypatch) -> TestClient:
    from quant_research.web import api as api_module
    from quant_research.web import create_app

    class _Data:
        directory = tmp_path

    class _Collection:
        provider = "baostock"
        adjust = "qfq"

    class _Settings:
        data = _Data()
        collection = _Collection()

    monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
    return TestClient(create_app())


@pytest.fixture()
def env_client(tmp_path, monkeypatch) -> TestClient:
    return _make_client(tmp_path, monkeypatch)


class TestIndexStore:
    def test_refresh_fetches_and_stores_with_meta(self, tmp_path, monkeypatch):
        frame = pd.DataFrame(
            {"close": [3000.0, 3050.0], "amount": [3.0e11, 3.2e11]},
            index=[pd.Timestamp("2026-11-07"), pd.Timestamp("2026-11-10")],
        )
        monkeypatch.setattr(index_mod, "fetch", lambda symbol: frame)

        result = index_mod.refresh(tmp_path, "sh000001")
        meta = index_mod.meta(tmp_path, "sh000001")

        assert result["amount"].iloc[-1] == 3.2e11
        assert meta["as_of"] == "2026-11-10"
        assert meta["rows"] == 2
        assert index_mod.load(tmp_path, "sh000001") is not None

    def test_a_broken_cache_file_reads_as_absent(self, tmp_path):
        target = tmp_path / index_mod.DIRECTORY / "sh000001.parquet"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not a parquet")

        assert index_mod.load(tmp_path, "sh000001") is None

    def test_fetch_rejects_a_frame_without_amount(self, monkeypatch):
        import akshare as ak

        frame = pd.DataFrame({"date": ["2026-11-10"], "close": [3000.0]})
        monkeypatch.setattr(ak, "stock_zh_index_daily_em", lambda symbol: frame)

        with pytest.raises(ValueError, match="amount"):
            index_mod.fetch("sh000001")

    def test_index_market_volume_uses_the_real_amounts(self):
        from quant_research.volume import market_from_index

        frame = pd.DataFrame(
            {"close": [3000.0] * 24 + [3060.0], "amount": [3.0e11] * 24 + [3.6e11]},
            index=[pd.Timestamp("2026-01-01") + timedelta(days=step) for step in range(25)],
        )

        row = market_from_index(frame)

        assert row.amount == pytest.approx(3600.0, abs=1.0)  # 3.6e11 元 → 3600 亿
        assert row.ratio == pytest.approx(1.2, abs=0.01)
        assert row.n_symbols == 1  # 指数不是样本，如实标注


class TestSectorFlow:
    def _snapshot(self) -> dict:
        return {
            "fetched_at": "2026-11-10T01:00:00+00:00",
            "sectors": [
                {
                    "name": "电子",
                    "main_net": 92.0,
                    "main_net_pct": 2.5,
                    "change_pct": 3.3,
                    "leader": "瑞芯微",
                },
                {
                    "name": "铝",
                    "main_net": -13.0,
                    "main_net_pct": -1.2,
                    "change_pct": -0.8,
                    "leader": "",
                },
            ],
            "top_in": [],
            "top_out": [],
            "missing": [],
        }

    def test_roundtrip(self, tmp_path):
        sector_flow_mod.save(tmp_path, self._snapshot())

        loaded = sector_flow_mod.load(tmp_path)
        assert loaded["sectors"][0]["name"] == "电子"

    def test_a_broken_file_reads_as_absent(self, tmp_path):
        target = tmp_path / sector_flow_mod.DIRECTORY / "latest.json"
        target.parent.mkdir(parents=True)
        target.write_text("{broken", encoding="utf-8")

        assert sector_flow_mod.load(tmp_path) is None

    def test_a_broken_interface_becomes_a_missing_note(self, monkeypatch):
        import akshare as ak

        monkeypatch.setattr(
            ak,
            "stock_sector_fund_flow_rank",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("network down")),
        )

        snapshot = sector_flow_mod.fetch()

        assert snapshot["sectors"] == []
        assert "行业资金流" in snapshot["missing"][0]
        assert "ConnectionError" in snapshot["missing"][0]

    def test_rows_with_a_missing_net_value_are_skipped(self, monkeypatch):
        import akshare as ak

        frame = pd.DataFrame(
            {
                "名称": ["电子", "铝"],
                "今日主力净流入-净额": [9.2e9, float("nan")],
                "今日主力净流入-净占比": [2.5, 1.0],
                "今日涨跌幅": [3.3, -0.8],
                "今日主力净流入最大股": ["瑞芯微", "云铝股份"],
            }
        )
        monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", lambda **kwargs: frame)

        snapshot = sector_flow_mod.fetch()

        assert [row["name"] for row in snapshot["sectors"]] == ["电子"]
        assert snapshot["sectors"][0]["main_net"] == pytest.approx(92.0)  # 元 → 亿


class TestSectorFlowAPI:
    def test_without_a_snapshot_the_get_says_how_to_get_one(self, env_client):
        payload = env_client.get("/api/sector-flow").json()

        assert payload["available"] is False
        assert "刷新" in payload["reason"]

    def test_get_never_fetches(self, env_client, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(
            sector_flow_mod,
            "fetch",
            lambda: calls.update(n=calls["n"] + 1) or {"sectors": []},
        )

        env_client.get("/api/sector-flow")

        assert calls["n"] == 0  # GET 不联网

    def test_refresh_fetches_saves_and_returns_the_snapshot(
        self, env_client, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            sector_flow_mod,
            "fetch",
            lambda: {
                "fetched_at": "2026-11-10T01:00:00+00:00",
                "sectors": [{"name": "电子", "main_net": 92.0}],
                "top_in": [],
                "top_out": [],
                "missing": [],
            },
        )

        payload = env_client.post("/api/sector-flow/refresh").json()

        assert payload["available"] is True
        assert payload["saved"] is True
        assert sector_flow_mod.load(tmp_path)["sectors"][0]["name"] == "电子"


class TestFundamentalsRiskAPI:
    @pytest.fixture()
    def fr_client(self, tmp_path, monkeypatch) -> tuple[TestClient, object]:
        """带持仓与自选的 client：季报核对的主体是「自己关心的票」。"""
        client = _make_client(tmp_path, monkeypatch)
        client.post("/api/portfolio", json={"symbol": "600519", "shares": 100, "cost": 1400})
        client.post("/api/watchlist", json={"symbol": "000001", "note": ""})
        return client, tmp_path

    def test_without_positions_or_watchlist_the_400_names_the_prerequisite(self, env_client):
        response = env_client.post("/api/fundamentals/refresh")

        assert response.status_code == 400
        assert "持仓与自选" in response.json()["detail"]

    def test_without_any_reports_the_400_names_the_prerequisite_not_a_clean_bill(self, fr_client):
        """没查过的票不该得到干净的结论——空季报表就是 400，不是「没有红旗」。"""
        client, _ = fr_client
        response = client.get("/api/fundamentals/risk")

        assert response.status_code == 400
        assert "季报" in response.json()["detail"]

    def test_uncovered_symbols_read_as_unknown_not_low_risk(self, fr_client):
        """季报表里只有别的票：覆盖不到的返回「未知」，不编一个「低风险」。"""

        from quant_research.fundamentals import fetch as fundamentals_fetch

        client, tmp_path = fr_client
        frame = pd.DataFrame(
            {
                "symbol": ["999999.SZ", "999999.SZ"],
                "pubDate": ["2026-10-30", "2026-08-30"],
                "statDate": ["2026-09-30", "2026-06-30"],
                "roeAvg": [5.0, 4.0],
                "npMargin": [10.0, 9.0],
                "gpMargin": [20.0, 19.0],
                "netProfit": [1e8, 0.9e8],
                "epsTTM": [1.0, 0.9],
                "MBRevenue": [5e8, 4.5e8],
                "totalShare": [1e9, 1e9],
                "liabilityToAsset": [40.0, 41.0],
                "currentRatio": [2.0, 2.1],
                "CFOtoOR": [0.1, 0.1],
                "assetToEquity": [1.6, 1.6],
            }
        )
        target = tmp_path / "baostock" / "reports.parquet"
        target.parent.mkdir(parents=True)
        frame.to_parquet(target)
        assert target.is_file()
        assert not fundamentals_fetch.load(tmp_path).frame.empty

        payload = client.get("/api/fundamentals/risk").json()

        assert payload["available"] is True
        assert payload["n_covered"] == 0  # 600519/000001 都没覆盖
        assert {row["level"] for row in payload["risks"]} == {"未知"}

    def test_refresh_collects_merges_and_assesses(self, fr_client, monkeypatch):
        """采集 → 合并旧表 → 落盘 → 评估，一条链走完。"""

        from quant_research.fundamentals.fetch import ReportPanel

        client, tmp_path = fr_client
        frame = pd.DataFrame(
            {
                "symbol": ["600519.SH", "600519.SH"],
                "pubDate": ["2026-10-30", "2026-08-30"],
                "statDate": ["2026-09-30", "2026-06-30"],
                "roeAvg": [5.0, 4.0],
                "npMargin": [10.0, 9.0],
                "gpMargin": [20.0, 19.0],
                "netProfit": [1e8, 0.9e8],
                "epsTTM": [1.0, 0.9],
                "MBRevenue": [5e8, 4.5e8],
                "totalShare": [1e9, 1e9],
                "liabilityToAsset": [40.0, 41.0],
                "currentRatio": [2.0, 2.1],
                "CFOtoOR": [0.1, 0.1],
                "assetToEquity": [1.6, 1.6],
            }
        )
        panel = ReportPanel(frame=frame, provider="baostock")
        monkeypatch.setattr(api_module, "fundamentals_fetch", _StubFetch(panel))

        payload = client.post("/api/fundamentals/refresh").json()

        assert payload["available"] is True
        assert payload["n_symbols"] == 2
        levels = {row["symbol"]: row["level"] for row in payload["risks"]}
        assert set(levels.values()) <= {"低", "中", "高", "未知"}
        assert (tmp_path / "fundamentals" / "baostock" / "reports.parquet").is_file()


class _StubFetch:
    """fundamentals_fetch 的替身：collect 返回预置面板，load 返回空表。"""

    def __init__(self, panel: object) -> None:
        self._panel = panel

    def collect(self, symbols, quarters=8, pause=0.0, today=None):
        return self._panel

    def load(self, directory, provider="baostock"):
        from quant_research.fundamentals.fetch import ReportPanel

        return ReportPanel(frame=pd.DataFrame(), provider=provider)

    def merge(self, old, new):
        return new

    def store(self, panel, directory):
        path = pathlib.Path(directory) / "fundamentals" / "baostock" / "reports.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stub")
        return path
