"""ETF：独立清单、独立落盘、红旗豁免——三个「不是股票」的约定都要有测试。

ETF 刻意不进股票面板目录（否则 `_discover_datasets` 把它当成股票数据集，
污染「样本合计」大盘口径）；fundamentals 核对豁免 ETF（没季报可查，
送去查只会失败）；快照把「没采过」如实列进 missing 而不是悄悄消失。
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data import etf as etf_mod
from quant_research.data.providers.base import normalize_symbol


def _bars(rows: int = 30, last_amount: float = 3e6) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=rows)
    frame = pd.DataFrame(
        {
            "open": np.full(rows, 1.0),
            "close": np.linspace(1.0, 1.2, rows),
            "high": np.full(rows, 1.2),
            "low": np.full(rows, 0.9),
            "volume": np.full(rows, 1e8),
            "amount": np.full(rows, 1e6, dtype=float),
            "turnover": np.full(rows, 1.0),
        },
        index=index,
    )
    frame.loc[index[-1], "amount"] = last_amount
    return frame


class TestSymbolRules:
    def test_etf_prefixes_are_recognised(self):
        assert etf_mod.is_etf("510300.SH")
        assert etf_mod.is_etf("159915.SZ")
        assert etf_mod.is_etf("518880")  # 裸代码也认
        assert not etf_mod.is_etf("600519.SH")
        assert not etf_mod.is_etf("000006.SZ")

    def test_normalize_maps_etf_codes(self):
        assert normalize_symbol("510300") == "510300.SH"
        assert normalize_symbol("159915") == "159915.SZ"


class TestStore:
    def test_store_writes_parquet_and_meta(self, tmp_path):
        frame = _bars()
        path = etf_mod.store(tmp_path, "510300", frame)

        assert path.is_file()
        meta = etf_mod.meta(tmp_path, "510300")
        assert meta["symbol"] == "510300"
        assert meta["name"] == "沪深300ETF"
        assert meta["rows"] == 30
        assert etf_mod.load(tmp_path, "510300") is not None

    def test_a_broken_cache_file_reads_as_absent(self, tmp_path):
        target = tmp_path / etf_mod.DIRECTORY / "510300.parquet"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not a parquet")

        assert etf_mod.load(tmp_path, "510300") is None
        assert etf_mod.meta(tmp_path, "510300") is None

    def test_etf_data_does_not_land_in_the_stock_dataset_scanner(self, tmp_path):
        """ETF 落盘在 processed/etf，不在 collection 目录——股票数据集
        扫描（*/daily）永远扫不到它，样本合计口径不被污染。"""
        etf_mod.store(tmp_path, "510300", _bars())

        assert not (tmp_path / "akshare").exists()
        assert not list(tmp_path.glob("*/daily/510300*"))


class TestFetch:
    def test_maps_the_cn_columns_to_standard_bars(self, monkeypatch):
        import akshare as ak

        raw = pd.DataFrame(
            {
                "日期": ["2026-11-10", "2026-11-11"],
                "开盘": [3.9, 4.0],
                "收盘": [4.0, 4.1],
                "最高": [4.05, 4.2],
                "最低": [3.85, 3.95],
                "成交量": [1e8, 1.5e8],
                "成交额": [4e8, 6e8],
                "换手率": [1.0, 1.5],
            }
        )
        monkeypatch.setattr(ak, "fund_etf_hist_em", lambda **kwargs: raw)

        frame = etf_mod.fetch("510300")

        assert set(frame.columns) >= {"open", "close", "high", "low", "volume", "amount"}
        assert frame["close"].iloc[-1] == 4.1

    def test_rejects_a_frame_without_the_essentials(self, monkeypatch):
        import akshare as ak

        raw = pd.DataFrame({"日期": ["2026-11-10"], "收盘": [4.0]})
        monkeypatch.setattr(ak, "fund_etf_hist_em", lambda **kwargs: raw)

        with pytest.raises(ValueError, match="缺少列"):
            etf_mod.fetch("510300")


class TestSnapshot:
    def test_volume_ratio_uses_the_shifted_baseline(self, tmp_path):
        """量比基线不含当日：末日放量 3 倍就报 3.0，不是 3/29 那种稀释。"""
        etf_mod.store(tmp_path, "510300", _bars(rows=30, last_amount=3e6))

        payload = etf_mod.snapshot(tmp_path)

        assert payload["available"] is True
        row = next(row for row in payload["rows"] if row["symbol"] == "510300")
        assert row["vol_ratio"] == pytest.approx(3.0)
        assert row["level"] == pytest.approx(1.0)  # 末日即 250 日高：水位 100%

    def test_untracked_codes_read_as_missing_not_silent(self, tmp_path):
        payload = etf_mod.snapshot(tmp_path)

        assert payload["available"] is False
        assert len(payload["missing"]) == len(etf_mod.ETF_UNIVERSE)
        assert payload["rows"] == []

    def test_short_history_leaves_metrics_null_not_invented(self, tmp_path):
        """10 天历史：量比（需要 21 天）如实为 None，不编一个数。"""
        etf_mod.store(tmp_path, "510300", _bars(rows=10, last_amount=3e6))

        row = etf_mod.snapshot(tmp_path)["rows"][0]

        assert row["vol_ratio"] is None
        assert row["day_change"] is not None  # 两天就有涨跌
        assert row["level"] == pytest.approx(1.0)

    def test_meta_survives_a_roundtrip(self, tmp_path):
        etf_mod.store(tmp_path, "510300", _bars())
        raw = json.loads((tmp_path / etf_mod.DIRECTORY / "510300.json").read_text(encoding="utf-8"))
        assert raw["as_of"] is not None


class TestAPI:
    """端点纪律：GET 只读缓存不联网；红旗核对豁免 ETF 并如实标注豁免数。"""

    @pytest.fixture()
    def etf_client(self, tmp_path, monkeypatch) -> TestClient:
        from fastapi.testclient import TestClient

        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Data:
            directory = tmp_path

        class _Collection:
            provider = "baostock"
            adjust = "qfq"
            output_directory = tmp_path / "akshare"

        class _Settings:
            data = _Data()
            collection = _Collection()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_snapshot_is_read_only_and_reports_missing(self, etf_client):
        payload = etf_client.get("/api/etf/snapshot").json()

        assert payload["available"] is False
        assert len(payload["missing"]) == len(etf_mod.ETF_UNIVERSE)
        assert payload["note"]  # 口径说明随响应返回

    def test_snapshot_after_a_store(self, etf_client, tmp_path):
        etf_mod.store(tmp_path, "510300", _bars())

        payload = etf_client.get("/api/etf/snapshot").json()

        assert payload["available"] is True
        assert payload["rows"][0]["symbol"] == "510300"
        assert payload["rows"][0]["name"] == "沪深300ETF"

    def test_refresh_calls_the_provider_and_survives_failures(
        self, etf_client, tmp_path, monkeypatch
    ):
        called: list[str] = []

        def fake_refresh(root, symbol, adjust="qfq"):
            called.append(symbol)
            if symbol == "513100":
                raise RuntimeError("跨境接口超时")
            return _bars()

        monkeypatch.setattr(etf_mod, "refresh", fake_refresh)
        monkeypatch.setattr(
            etf_mod, "refresh_spot", lambda root: {"as_of": "2026-11-11", "n_rows": 0}
        )

        payload = etf_client.post("/api/etf/refresh").json()

        assert len(called) == len(etf_mod.ETF_UNIVERSE)  # 一只失败不拖垮整批
        assert any(row["symbol"] == "513100" for row in payload["failed"])
        assert any(row["symbol"] == "510300" for row in payload["fetched"])
        assert payload["available"] is True

    def test_watch_symbols_exclude_etf(self, tmp_path, monkeypatch):
        """红旗核对只看股票：ETF 进自选也不会被送去查财报。"""
        from quant_research.web import api as api_module

        class _Stores:
            @staticmethod
            def load(directory):
                if not directory.name:
                    return {}

        # 直接走模块函数：写两个 store 再断言
        from quant_research import holdings as holdings_store
        from quant_research import watchlist as watchlist_store

        class _Data:
            directory = tmp_path

        class _Collection:
            provider = "baostock"
            adjust = "qfq"

        class _Settings:
            data = _Data()
            collection = _Collection()

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        watchlist_store.save(
            tmp_path,
            {
                "stocks": {"510300.SH": {"added_at": "x"}, "600519.SH": {"added_at": "y"}},
                "categories": [],
            },
        )
        holdings_store.save(tmp_path, {"positions": {"000006.SZ": {}}, "cash": 0})

        assert api_module._watch_symbols(_Settings()) == ["000006.SZ", "600519.SH"]

    def test_risk_view_labels_the_etf_exemption(self, etf_client, tmp_path, monkeypatch):
        """有股票也有 ETF：红旗卡必须标注「豁免 N 只」，不是「未覆盖」。"""
        import pandas as pd

        from quant_research.fundamentals.fetch import ReportPanel
        from quant_research.web import api as api_module

        index = pd.MultiIndex.from_tuples([("600519.SH", "2025Q4")], names=["symbol", "period"])
        frame = pd.DataFrame({"published": ["2026-04-17"]}, index=index)
        panel = ReportPanel(frame=frame, provider="baostock")
        monkeypatch.setattr(api_module.fundamentals_fetch, "load", lambda directory: panel)
        monkeypatch.setattr(
            api_module.universe, "load", lambda directory: pd.DataFrame({"symbol": [], "name": []})
        )

        from quant_research import watchlist as watchlist_store

        watchlist_store.save(
            tmp_path,
            {"stocks": {"510300.SH": {}, "600519.SH": {}}, "categories": []},
        )

        payload = etf_client.get("/api/fundamentals/risk").json()

        assert payload["available"] is True
        assert payload["etf_skipped"] == 1
        assert [row["symbol"] for row in payload["risks"]] == ["600519.SH"]


class TestRefreshFallback:
    def test_short_window_retry_when_qfq_goes_negative(self, tmp_path, monkeypatch):
        """超长历史 qfq 把早期价格折成负数：refresh 缩窗重试而不是拒绝整个标的。"""
        attempts: list[date | None] = []

        def fake_fetch(symbol, adjust="qfq", start=None, end=None):
            attempts.append(start)
            if start is None:
                raise ValueError("returned non-positive closes")
            return _bars(rows=10, last_amount=1e6)

        monkeypatch.setattr(etf_mod, "fetch", fake_fetch)

        frame = etf_mod.refresh(tmp_path, "510050")

        assert frame is not None
        assert attempts[0] is None and attempts[1] == date(2015, 1, 1)
        assert etf_mod.load(tmp_path, "510050") is not None


class TestSpot:
    """份额快照：单日只有「是多少」，两次之间才有「在流入还是流出」。"""

    def test_fetch_spot_filters_to_the_watchlist(self, monkeypatch):
        import akshare as ak

        raw = pd.DataFrame(
            {
                "代码": ["510300", "511990", "159915"],
                "最新价": [4.12, 100.0, 2.52],
                "成交额": [1.5e9, 3e8, 4.2e9],
                "最新份额": [4.6e10, 1e9, 4.5e10],
                "总市值": [1.9e11, 1e11, 1.1e11],
                "数据日期": ["2026-11-11"] * 3,
            }
        )
        monkeypatch.setattr(ak, "fund_etf_spot_em", lambda: raw)

        rows, as_of = etf_mod.fetch_spot()

        assert as_of == "2026-11-11"
        assert set(rows) == {"510300", "159915"}  # 清单外的 511990 不进快照
        assert rows["510300"]["share"] == pytest.approx(4.6e10)

    def test_single_snapshot_has_no_fake_change(self, tmp_path):
        etf_mod.store_spot(tmp_path, {"510300": {"share": 4.6e10, "mktcap": 1.9e11}}, "2026-11-10")

        delta = etf_mod.share_delta(tmp_path)

        assert delta["510300"]["share_change"] is None  # 同比对象不存在就是 None
        assert delta["510300"]["spot_prev_as_of"] is None

    def test_change_is_measured_against_the_previous_snapshot(self, tmp_path):
        etf_mod.store_spot(tmp_path, {"510300": {"share": 4.6e10, "mktcap": 1.9e11}}, "2026-11-10")
        etf_mod.store_spot(tmp_path, {"510300": {"share": 4.83e10, "mktcap": 2.0e11}}, "2026-11-11")

        delta = etf_mod.share_delta(tmp_path)

        assert delta["510300"]["share_change"] == pytest.approx(0.05)  # 4.83/4.6-1
        assert delta["510300"]["spot_prev_as_of"] == "2026-11-10"
        assert delta["510300"]["spot_as_of"] == "2026-11-11"

    def test_snapshot_merges_share_info(self, tmp_path):
        etf_mod.store(tmp_path, "510300", _bars())
        etf_mod.store_spot(tmp_path, {"510300": {"share": 4.6e10, "mktcap": 1.9e11}}, "2026-11-11")

        row = next(r for r in etf_mod.snapshot(tmp_path)["rows"] if r["symbol"] == "510300")

        assert row["share"] == pytest.approx(4.6e10)
        assert row["mktcap"] == pytest.approx(1.9e11)
        assert row["spot_as_of"] == "2026-11-11"

    def test_broken_spot_file_is_skipped(self, tmp_path):
        directory = tmp_path / etf_mod.SPOT_DIRECTORY
        directory.mkdir(parents=True)
        (directory / "2026-11-11.json").write_text("{not json", encoding="utf-8")

        assert etf_mod.load_spot(tmp_path) == []
        assert etf_mod.share_delta(tmp_path) == {}

    def test_refresh_spot_failure_does_not_sink_the_daily_bars(self, tmp_path, monkeypatch):
        """份额快照挂了，日线结果照常返回，失败在 failed 里逐条可见。"""
        from quant_research.web import api as api_module

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _FakeSettings(tmp_path))
        monkeypatch.setattr(etf_mod, "refresh", lambda root, symbol, adjust="qfq": _bars())
        monkeypatch.setattr(
            etf_mod,
            "refresh_spot",
            lambda root: (_ for _ in ()).throw(RuntimeError("现货行情超时")),
        )

        payload = api_module._etf_refresh_view("qfq")

        assert len(payload["fetched"]) == len(etf_mod.ETF_UNIVERSE)
        assert any(row["symbol"] == "spot" for row in payload["failed"])
        assert payload["available"] is True


class _FakeSettings:
    def __init__(self, directory):
        class _Data:
            pass

        self.data = _Data()
        self.data.directory = directory
