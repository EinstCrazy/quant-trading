"""外围盘：快照读写、情绪分、传导路径，以及 API 的「GET 不联网」约定。

守住的几条：外围只影响开盘环境，每条传导必须带前提；美元指数反向计入情绪
分（走强是收紧）；缺数据进 missing，不写成「持平」；GET 只读缓存，联网抓取
只在显式 POST 里发生。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from quant_research import overseas
from quant_research.overseas import analyze, fetch, latest_path, load, save


def _quote(code: str, name: str, change: float, group: str, price: float = 100.0) -> dict:
    return {
        "code": code,
        "name": name,
        "group": group,
        "price": price,
        "change_pct": change,
        "as_of": "2026-11-09 05:00:00",
    }


def _snapshot(
    quotes: list[dict],
    rates: dict | None = None,
    fetched_at: str | None = None,
    missing: list[str] | None = None,
) -> dict:
    return {
        "fetched_at": fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "quotes": quotes,
        "rates": rates or {},
        "missing": missing or [],
    }


def _backdrop(us: float = 1.0, hk: float = 0.0, usd: float = 0.0) -> list[dict]:
    return [
        _quote("DJIA", "道琼斯", us, "美股"),
        _quote("SPX", "标普500", us, "美股"),
        _quote("NDX", "纳斯达克", us, "美股"),
        _quote("HSI", "恒生指数", hk, "港股"),
        _quote("HSCEI", "国企指数", hk, "港股"),
        _quote("UDI", "美元指数", usd, "汇率与商品", price=101.9),
        _quote("CRB", "路透CRB商品指数", 0.0, "汇率与商品"),
    ]


class TestStore:
    def test_roundtrip(self, tmp_path):
        snapshot = _snapshot(_backdrop())

        save(tmp_path, snapshot)

        assert load(tmp_path)["quotes"] == snapshot["quotes"]
        assert latest_path(tmp_path).is_file()

    def test_same_day_refresh_overwrites(self, tmp_path):
        save(tmp_path, _snapshot(_backdrop(us=1.0)))
        save(tmp_path, _snapshot(_backdrop(us=-2.0)))

        assert load(tmp_path)["quotes"][0]["change_pct"] == -2.0

    def test_a_broken_file_reads_as_absent(self, tmp_path):
        target = latest_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{broken", encoding="utf-8")

        assert load(tmp_path) is None

    def test_no_snapshot_yet(self, tmp_path):
        assert load(tmp_path) is None


class TestAnalyze:
    def test_groups_keep_the_watch_list_order(self):
        snapshot = _snapshot(_backdrop() + [_quote("GC", "COMEX黄金", 0.5, "大宗商品")])

        payload = analyze(snapshot)

        assert [group["name"] for group in payload["groups"]] == [
            "美股",
            "港股",
            "汇率与商品",
            "大宗商品",
        ]
        assert payload["n_quotes"] == 8

    def test_group_average_ignores_missing_quotes(self):
        snapshot = _snapshot(
            [
                _quote("DJIA", "道琼斯", 1.0, "美股"),
                _quote("SPX", "标普500", None, "美股"),
            ]
        )

        payload = analyze(snapshot)

        assert payload["groups"][0]["avg_change"] == pytest.approx(1.0, abs=0.01)

    def test_a_broad_rally_reads_as_risk_on(self):
        payload = analyze(_snapshot(_backdrop(us=1.5, hk=1.2)))

        assert payload["tone"]["label"] == "风险偏好回升"
        assert payload["tone"]["score"] > 0.5

    def test_a_broad_selloff_reads_as_risk_off(self):
        payload = analyze(_snapshot(_backdrop(us=-1.5, hk=-1.2)))

        assert payload["tone"]["label"] == "风险偏好回落"

    def test_a_stronger_dollar_pulls_the_score_down(self):
        base = analyze(_snapshot(_backdrop(us=1.0, usd=0.0)))["tone"]["score"]
        stronger = analyze(_snapshot(_backdrop(us=1.0, usd=1.0)))["tone"]["score"]

        assert stronger < base

    def test_mixed_markets_read_as_divergent(self):
        snapshot = _snapshot(
            [
                _quote("DJIA", "道琼斯", 0.1, "美股"),
                _quote("SPX", "标普500", -0.1, "美股"),
                _quote("HSI", "恒生指数", 0.05, "港股"),
            ]
        )

        assert analyze(snapshot)["tone"]["label"] == "分化"

    def test_an_empty_snapshot_says_so_instead_of_guessing(self):
        payload = analyze(
            {"fetched_at": None, "quotes": [], "rates": {}, "missing": ["全球指数：timeout"]}
        )

        assert payload["tone"]["label"] == "数据不足"
        assert "一项都没拿到" in payload["advice"]
        assert payload["groups"] == []

    def test_missing_data_is_named_in_the_advice(self):
        payload = analyze(
            _snapshot(_backdrop(), missing=["美债收益率：ConnectionError", "外盘商品：timeout"])
        )

        assert "2 项数据没抓到" in payload["advice"]

    def test_age_and_staleness(self):
        old = (datetime.now(UTC) - timedelta(hours=20)).isoformat(timespec="seconds")
        fresh = analyze(_snapshot(_backdrop(), fetched_at=old))

        assert fresh["stale"] is True
        assert fresh["age_hours"] >= 19

        assert analyze(_snapshot(_backdrop()))["stale"] is False

    def test_a_broken_timestamp_is_not_fatal(self):
        assert analyze(_snapshot(_backdrop(), fetched_at="昨天"))["age_hours"] is None


class TestSignals:
    def _codes(self, payload: dict) -> set[str]:
        return {item["market"] for item in payload["signals"]}

    def test_a_nasdaq_drop_warns_about_growth_at_the_open(self):
        snapshot = _snapshot([_quote("NDX", "纳斯达克", -1.6, "美股")])

        signal = analyze(snapshot)["signals"][0]

        assert "科技成长" in signal["impact"]
        assert "缩量" in signal["condition"]  # 前提里必须有 A 股自身的配合条件

    def test_a_nasdaq_rally_is_not_called_a_buy(self):
        snapshot = _snapshot([_quote("NDX", "纳斯达克", 1.4, "美股")])

        signal = analyze(snapshot)["signals"][0]

        assert "情绪" in signal["impact"]
        assert "买" not in signal["impact"]

    def test_a_weaker_dollar_eases_pressure(self):
        snapshot = _snapshot([_quote("UDI", "美元指数", -0.5, "汇率与商品")])

        assert "人民币压力缓解" in analyze(snapshot)["signals"][0]["impact"]

    def test_rising_long_yields_pressure_duration_assets(self):
        snapshot = _snapshot(
            [_quote("DJIA", "道琼斯", 0.1, "美股")],
            rates={"us10y": 4.9, "us10y_change_bp": 7.0},
        )

        assert "美债 10 年期" in self._codes(analyze(snapshot))
        assert "久期" in analyze(snapshot)["signals"][0]["impact"]

    def test_small_moves_trigger_nothing(self):
        snapshot = _snapshot(
            [
                _quote("NDX", "纳斯达克", 0.3, "美股"),
                _quote("HSI", "恒生指数", -0.2, "港股"),
                _quote("UDI", "美元指数", 0.1, "汇率与商品"),
            ],
            rates={"us10y": 4.9, "us10y_change_bp": 1.0},
        )

        assert analyze(snapshot)["signals"] == []

    def test_oil_moves_are_mapped_both_ways(self):
        up = analyze(_snapshot([_quote("CL", "NYMEX原油", 3.0, "大宗商品")]))
        down = analyze(_snapshot([_quote("CL", "NYMEX原油", -3.0, "大宗商品")]))

        assert "石油石化" in up["signals"][0]["impact"]
        assert "成本端" in up["signals"][0]["impact"]
        assert "石油石化承压" in down["signals"][0]["impact"]

    def test_gold_strength_is_flagged_as_both_a_theme_and_a_warning(self):
        payload = analyze(_snapshot([_quote("GC", "COMEX黄金", 1.5, "大宗商品")]))

        impact = payload["signals"][0]["impact"]
        assert "黄金股" in impact
        assert "避险" in impact


class TestAPI:
    @pytest.fixture()
    def os_client(self, tmp_path, monkeypatch) -> TestClient:
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

    def test_without_a_snapshot_the_get_says_how_to_get_one(self, os_client):
        payload = os_client.get("/api/overseas/analysis").json()

        assert payload["available"] is False
        assert "刷新" in payload["reason"]

    def test_get_never_fetches(self, os_client, monkeypatch):
        calls = {"n": 0}

        def fake_fetch() -> dict:
            calls["n"] += 1
            return _snapshot(_backdrop())

        monkeypatch.setattr(overseas, "fetch", fake_fetch)

        os_client.get("/api/overseas/analysis")

        assert calls["n"] == 0  # GET 不联网

    def test_refresh_fetches_saves_and_returns_the_read(self, os_client, monkeypatch, tmp_path):
        monkeypatch.setattr(
            overseas, "fetch", lambda: _snapshot(_backdrop(us=1.2), rates={"us10y": 4.9})
        )

        payload = os_client.post("/api/overseas/refresh").json()

        assert payload["available"] is True
        assert payload["saved"] is True
        assert latest_path(tmp_path).is_file()
        # 之后的 GET 读的是刚落盘的快照
        again = os_client.get("/api/overseas/analysis").json()
        assert again["groups"][0]["avg_change"] == pytest.approx(1.2, abs=0.01)

    def test_a_refresh_that_gets_nothing_is_a_503_not_an_empty_page(self, os_client, monkeypatch):
        monkeypatch.setattr(
            overseas,
            "fetch",
            lambda: {
                "fetched_at": None,
                "quotes": [],
                "rates": {},
                "missing": ["全球指数：timeout"],
            },
        )

        response = os_client.post("/api/overseas/refresh")

        assert response.status_code == 503
        assert "一项都没抓到" in response.json()["detail"]

    def test_fetch_swallows_broken_interfaces_and_reports_them(self, monkeypatch):
        """三个接口各自失败，缺的进 missing，而不是整个 fetch 抛异常。"""

        class Boom(Exception):
            pass

        def broken(*args, **kwargs):
            raise Boom("network down")

        import akshare as ak

        monkeypatch.setattr(ak, "index_global_spot_em", broken)
        monkeypatch.setattr(ak, "futures_foreign_commodity_realtime", broken)
        monkeypatch.setattr(ak, "bond_zh_us_rate", broken)

        snapshot = fetch()

        assert len(snapshot["missing"]) == 3
        assert snapshot["quotes"] == []
        assert json.dumps(snapshot)  # 可序列化（能落盘）
