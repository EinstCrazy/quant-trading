"""复盘加强：情景预案、组合纪律、存档与对比。

守住的三条：预案必须带具体价位（没有数字的「跌破支撑」等于没说）；
纪律检查全过时给一条明确的通过项，而不是沉默；存档是显式动作，
对比的对象是上一次而不是自己。
"""

from __future__ import annotations

import re

import pytest

from quant_research.review import advise


def _verdict(**overrides):
    base = {
        "close": 100.0,
        "cost": 100.0,
        "rsi": 50.0,
        "ma20": 95.0,
        "ma60": 90.0,
        "atr": 2.0,
        "high20": 101.0,
        "low20": 90.0,
    }
    base.update(overrides)
    return advise(**base)


# --- 情景预案 -----------------------------------------------------------------


class TestPlans:
    def test_every_stance_gets_plans_with_concrete_numbers(self):
        """预案必须带上具体价位：没有数字的「跌破支撑」等于没说。"""
        cases = {
            "stop": {"close": 85.0, "cost": 100.0, "ma20": 90.0},
            "reduce_loss": {"close": 85.0, "cost": 100.0, "ma20": 80.0},
            "reduce_overbought": {"close": 99.0, "cost": 100.0, "rsi": 74.0, "high20": 100.0},
            "reduce_downtrend": {"close": 80.0, "cost": 85.0, "ma20": 88.0, "ma60": 84.0},
            "take": {"close": 130.0, "cost": 100.0, "rsi": 85.0, "ma20": 120.0, "high20": 131.0},
            "watch": {"close": 92.0, "cost": 100.0, "ma20": 95.0, "ma60": 90.0},
            "hold": {"close": 110.0, "cost": 100.0, "rsi": 55.0, "ma20": 105.0},
        }
        for name, overrides in cases.items():
            verdict = _verdict(**overrides)
            assert len(verdict.plans) >= 2, name
            joined = " ".join(f"{p.when} {p.then}" for p in verdict.plans)
            assert any(ch.isdigit() for ch in joined), f"{name} 的预案没有数字"
            assert not re.search(r"(MA20|MA60|ATR 止损参考|20 日[高低]|成本)\s*—", joined), (
                f"{name} 的预案里出现了缺失值占位符"
            )

    def test_nodata_has_no_plans(self):
        assert _verdict(close=None).plans == ()

    def test_plans_shrink_when_averages_are_missing(self):
        """预热期没有 MA/ATR：少几条预案，而不是生成「跌破 MA20 —」。"""
        verdict = _verdict(close=110.0, cost=100.0, ma20=None, ma60=None, atr=None, high20=101.0)

        assert all("MA20" not in p.when + p.then for p in verdict.plans)
        assert all("止损参考 —" not in p.when + p.then for p in verdict.plans)


# --- 组合纪律检查 -------------------------------------------------------------


class TestDiscipline:
    def _rows(self, *stances, closes=None):
        rows = []
        for index, stance in enumerate(stances):
            close = (closes or [100.0] * len(stances))[index]
            rows.append(
                {
                    "symbol": f"S{index}.SH",
                    "stance": stance,
                    "shares": 100,
                    "cost": 100.0,
                    "close": close,
                }
            )
        return rows

    def test_a_concentrated_book_fails_the_first_line_of_defence(self):
        from quant_research.review import discipline, summarize

        rows = [
            {"symbol": "A.SH", "stance": "hold", "shares": 1000, "cost": 10.0, "close": 10.0},
            {"symbol": "B.SH", "stance": "hold", "shares": 10, "cost": 10.0, "close": 10.0},
        ]
        items = discipline(rows, summarize(rows))

        assert any(item["item"] == "单一持仓集中度" and item["status"] == "fail" for item in items)

    def test_risk_heavy_book_warns_and_fails_at_the_doubled_line(self):
        from quant_research.review import discipline, summarize

        rows = self._rows("stop", "reduce", "hold")
        items = discipline(rows, summarize(rows))
        assert any(item["item"] == "风险档占比" and item["status"] == "fail" for item in items)

        rows = self._rows("stop", "hold", "hold")
        items = discipline(rows, summarize(rows))
        risk = next(item for item in items if item["item"] == "风险档占比")
        assert risk["status"] == "warn"  # 33% 超过 30% 参考线，未到 45%

    def test_stop_zone_positions_are_named_one_by_one(self):
        from quant_research.review import discipline, summarize

        rows = self._rows("hold", "stop", closes=[105.0, 85.0])
        items = discipline(rows, summarize(rows))

        stop = next(item for item in items if item["item"] == "止损区仍有仓位")
        assert stop["status"] == "warn"
        assert "S1.SH" in stop["detail"]

    def test_heavy_losses_get_a_hindsight_check(self):
        from quant_research.review import discipline, summarize

        rows = self._rows("hold", "hold", closes=[70.0, 105.0])
        items = discipline(rows, summarize(rows))

        heavy = next(item for item in items if item["item"] == "重亏仓位")
        assert "20%" in heavy["detail"]

    def test_a_clean_book_gets_one_explicit_pass_not_silence(self):
        from quant_research.review import discipline, summarize

        rows = self._rows("hold", "hold", "hold")
        items = discipline(rows, summarize(rows))

        assert [item["status"] for item in items] == ["pass"]
        assert "没有触发" in items[0]["detail"]

    def test_missing_quotes_warn_that_the_review_is_incomplete(self):
        from quant_research.review import discipline, summarize

        rows = self._rows("hold") + [
            {"symbol": "X.SH", "stance": "nodata", "shares": 100, "cost": 100.0, "close": None}
        ]
        items = discipline(rows, summarize(rows))

        assert any(item["item"] == "数据完整性" and item["status"] == "warn" for item in items)


# --- 复盘存档与对比 -----------------------------------------------------------


def _payload(as_of="2024-02-23", stance="hold"):
    return {
        "as_of": as_of,
        "provider": "baostock",
        "summary": {"note": "结构健康"},
        "positions": [
            {"symbol": "600000.SH", "stance": stance, "close": 100.0, "pnl_pct": 0.0},
        ],
    }


class TestArchive:
    def test_save_then_latest_roundtrip(self, tmp_path):
        from quant_research.review_archive import latest, save

        save(tmp_path, _payload("2024-02-22"))
        save(tmp_path, _payload("2024-02-23"))

        stored = latest(tmp_path)
        assert stored["date"] == "2024-02-23"
        assert stored["positions"]["600000.SH"]["stance"] == "hold"
        assert stored["positions"]["600000.SH"]["label"] == "持有"

    def test_latest_can_skip_today(self, tmp_path):
        from quant_research.review_archive import latest, save

        save(tmp_path, _payload("2024-02-22"))
        save(tmp_path, _payload("2024-02-23"))

        assert latest(tmp_path, exclude="2024-02-23")["date"] == "2024-02-22"
        assert latest(tmp_path, exclude="2024-02-22")["date"] == "2024-02-23"

    def test_resaving_the_same_day_replaces_it(self, tmp_path):
        from quant_research.review_archive import load, save

        save(tmp_path, _payload("2024-02-23", stance="hold"))
        save(tmp_path, _payload("2024-02-23", stance="stop"))

        assert load(tmp_path, "2024-02-23")["positions"]["600000.SH"]["stance"] == "stop"

    def test_a_broken_archive_is_skipped_not_fatal(self, tmp_path):
        from quant_research.review_archive import directory, latest, save

        save(tmp_path, _payload("2024-02-22"))
        (directory(tmp_path) / "2024-02-23.json").write_text("{broken", encoding="utf-8")

        assert latest(tmp_path)["date"] == "2024-02-22"

    def test_no_date_means_no_archive(self, tmp_path):
        from quant_research.review_archive import save

        with pytest.raises(ValueError):
            save(tmp_path, {"as_of": None, "positions": []})


# --- API：存档与对比、纪律、预案 ---------------------------------------------


@pytest.fixture()
def hold_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.config import ReviewSettings
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
        review = ReviewSettings()

    settings = _Settings()
    monkeypatch.setattr(api_module, "load_settings", lambda *args, **kwargs: settings)
    return TestClient(create_app())


def _mock_panel(monkeypatch):
    import numpy as np
    import pandas as pd

    from quant_research.data.loader import PricePanel
    from quant_research.web import api as api_module

    index = pd.bdate_range("2024-01-01", periods=40, name="date")
    wave = np.sin(np.linspace(0, 6, 40)) * 0.5
    closes = {
        "600000.SH": np.linspace(100.0, 70.0, 40) + wave,
        "000001.SZ": np.linspace(100.0, 130.0, 40) + wave,
    }
    prices = pd.DataFrame(closes, index=index)
    panel = PricePanel(
        prices=prices,
        extra={
            "open": prices * 0.999,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "volume": prices * 0 + 1000.0,
        },
    )
    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)
    return panel


class TestReviewAPIEnhanced:
    def test_save_then_review_reports_the_previous_archive(self, hold_client, monkeypatch):
        _mock_panel(monkeypatch)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})

        saved = hold_client.post("/api/portfolio/review/save").json()
        assert saved["date"] == "2024-02-23"

        payload = hold_client.get("/api/portfolio/review").json()
        assert payload["previous"]["date"] == "2024-02-23"
        assert payload["changes"] == []  # 档位没有变化：不编一条变化

    def test_discipline_travels_with_the_review(self, hold_client, monkeypatch):
        _mock_panel(monkeypatch)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})

        payload = hold_client.get("/api/portfolio/review").json()

        assert payload["discipline"]
        assert {item["status"] for item in payload["discipline"]} <= {"pass", "warn", "fail"}

    def test_saving_without_positions_is_a_clear_400(self, hold_client):
        assert hold_client.post("/api/portfolio/review/save").status_code == 400

    def test_plans_ride_along_with_each_position(self, hold_client, monkeypatch):
        _mock_panel(monkeypatch)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})

        row = hold_client.get("/api/portfolio/review").json()["positions"][0]

        assert row["plans"]
        assert {"when", "then"} <= set(row["plans"][0])
