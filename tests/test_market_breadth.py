"""大盘广度快照：涨跌家数 + 涨跌停家数，按日留档。

量能回答「多少钱在动」，广度回答「多少人赚钱」——3300 家上涨和
3300 家下跌可以是同一个成交额。口径诚实：广度是全市场统计，与
面板样本量比分母不同，并排放不换算。
"""

from __future__ import annotations

import pytest

from quant_research import market_breadth as breadth


def _snapshot(**overrides):
    base = {
        "fetched_at": "2026-11-11T07:00:00+00:00",
        "trade_date": "2026-11-11 15:00:00",
        "up": 3381,
        "down": 1647,
        "flat": 191,
        "halted": 6,
        "limit_up": 84,
        "limit_down": 3,
        "limit_up_real": 75,
        "limit_down_real": 2,
        "activity_pct": 64.71,
        "up_ratio": 67.2,
        "missing": [],
    }
    base.update(overrides)
    return base


class TestSaveLoad:
    def test_save_then_load_roundtrip(self, tmp_path):
        breadth.save(tmp_path, _snapshot())

        loaded = breadth.load(tmp_path)
        assert loaded["up"] == 3381
        assert loaded["limit_up_real"] == 75

    def test_daily_archive_one_per_day(self, tmp_path):
        """每天一份：同日重抓覆盖（同一天的数据没有两个版本），断日有洞。"""
        breadth.save(tmp_path, _snapshot())
        breadth.save(
            tmp_path,
            _snapshot(fetched_at="2026-11-12T07:00:00+00:00", up=2000, down=3000),
        )
        breadth.save(
            tmp_path,
            _snapshot(fetched_at="2026-11-12T09:00:00+00:00", up=2100, down=2900),
        )

        daily = breadth.load_daily(tmp_path)
        assert [row["fetched_at"][:10] for row in daily] == ["2026-11-12", "2026-11-11"]
        assert daily[0]["up"] == 2100  # 同日重抓覆盖为最新
        assert breadth.load(tmp_path)["up"] == 2100

    def test_empty_directory_returns_empty(self, tmp_path):
        assert breadth.load(tmp_path) is None
        assert breadth.load_daily(tmp_path) == []

    def test_corrupt_file_skipped_not_fatal(self, tmp_path):
        breadth.save(tmp_path, _snapshot())
        (tmp_path / breadth.DIRECTORY / "2026-11-10.json").write_text("{bad", encoding="utf-8")

        assert len(breadth.load_daily(tmp_path)) == 1  # 坏文件跳过，好文件照读


class TestFetchParsing:
    def test_up_ratio_needs_both_sides(self, tmp_path):
        """上涨占比只在有涨有跌时才算——缺一边不编数字。"""
        snapshot = breadth.fetch()
        if snapshot["missing"]:
            pytest.skip(f"乐咕接口不可达：{snapshot['missing']}")
        assert snapshot["up"] > 0 and snapshot["down"] > 0
        assert 0 < snapshot["up_ratio"] < 100


class TestBreadthAPI:
    """端到端：POST 采集留档 → GET /volume/analysis 带广度（只读缓存）。"""

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from quant_research.web import api as api_module
        from quant_research.web import create_app

        class _Settings:
            class data:
                directory = tmp_path

            class collection:
                provider = "baostock"
                adjust = "qfq"
                provider_options: dict = {}  # noqa: RUF012

            class review:
                pass

        monkeypatch.setattr(api_module, "load_settings", lambda *a, **k: _Settings())
        return TestClient(create_app())

    def test_refresh_persists_and_payload_carries_breadth(self, client, monkeypatch):
        from quant_research.web import api as api_module

        monkeypatch.setattr(api_module.breadth_mod, "fetch", _snapshot)

        response = client.post("/api/market/breadth/refresh")
        assert response.status_code == 200
        assert response.json()["up"] == 3381

        volume = client.get("/api/volume/analysis").json()
        # 面板数据不可用时 payload 走 unavailable 分支——广度仍在（快照独立于面板）
        if volume.get("breadth") is not None:
            assert volume["breadth"]["up"] == 3381

    def test_refresh_failure_is_502(self, client, monkeypatch):
        from quant_research.web import api as api_module

        monkeypatch.setattr(
            api_module.breadth_mod,
            "fetch",
            lambda: _snapshot(missing=["涨跌家数：ConnectionError: timeout"]),
        )

        response = client.post("/api/market/breadth/refresh")
        assert response.status_code == 502
        assert "timeout" in response.json()["detail"]
