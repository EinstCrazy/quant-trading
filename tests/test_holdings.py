"""持仓账本与复盘规则。

要守住的三条：成本与股数是用户录的、价格永远现读；规则只描述状态不下
指令（档位互斥、先风险后机会）；数据缺一块就少一条依据，而不是整段失败。
"""

from __future__ import annotations

import pytest

# --- 存储层 -------------------------------------------------------------------


class TestStore:
    def test_upsert_normalises_the_symbol(self, tmp_path):
        from quant_research.holdings import load, path, upsert

        upsert(tmp_path, "600519", shares=100, cost=1500.0)

        data = load(tmp_path)
        assert "600519.SH" in data["positions"]
        assert data["positions"]["600519.SH"]["shares"] == 100
        assert data["positions"]["600519.SH"]["cost"] == 1500.0
        assert path(tmp_path).name == "holdings.json"

    def test_upsert_keeps_added_at_and_note(self, tmp_path):
        from quant_research.holdings import load, upsert

        upsert(tmp_path, "600519.SH", shares=100, cost=1500.0, note="底仓")
        first = load(tmp_path)["positions"]["600519.SH"]

        upsert(tmp_path, "600519.SH", shares=200, cost=1450.0)

        updated = load(tmp_path)["positions"]["600519.SH"]
        assert (updated["shares"], updated["cost"]) == (200, 1450.0)
        assert updated["note"] == "底仓"  # 没给就沿用，不是清空
        assert updated["added_at"] == first["added_at"]

    def test_negative_or_zero_numbers_are_refused(self, tmp_path):
        """融券与零成本这里表达不了：拒绝写入，而不是存一个算错的数。"""
        from quant_research.holdings import upsert

        with pytest.raises(ValueError):
            upsert(tmp_path, "600519.SH", shares=-100, cost=1500.0)
        with pytest.raises(ValueError):
            upsert(tmp_path, "600519.SH", shares=100, cost=0)

    def test_a_corrupt_file_reads_as_empty_not_as_a_crash(self, tmp_path):
        from quant_research.holdings import empty, load

        (tmp_path / "holdings.json").write_text("{not json", encoding="utf-8")

        assert load(tmp_path) == empty()

    def test_remove_an_absent_symbol_is_a_noop(self, tmp_path):
        from quant_research.holdings import empty, remove

        assert remove(tmp_path, "600519.SH") == empty()


# --- 规则层：档位互斥、先风险后机会 -------------------------------------------


def _verdict(**overrides):
    from quant_research.review import advise

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


class TestRules:
    def test_loss_beyond_threshold_below_ma20_is_the_stop_zone(self):
        verdict = _verdict(close=85.0, cost=100.0, ma20=90.0)

        assert verdict.stance == "stop"
        assert "-15" in verdict.headline
        assert verdict.levels["atr_stop"] == pytest.approx(85.0 - 2 * 2.0)

    def test_loss_beyond_threshold_above_ma20_is_a_warning_not_a_stop(self):
        """趋势没坏时，风险在盈亏与仓位，不在价格——话术必须不同。"""
        verdict = _verdict(close=85.0, cost=100.0, ma20=80.0)

        assert verdict.stance == "reduce"
        assert "MA20 上方" in verdict.headline

    def test_thick_profit_plus_extreme_rsi_is_the_take_profit_zone(self):
        verdict = _verdict(close=130.0, cost=100.0, rsi=85.0, ma20=120.0, high20=131.0)

        assert verdict.stance == "take"
        assert "+30" in verdict.headline

    def test_overbought_near_the_20_day_high_is_a_reduce(self):
        verdict = _verdict(close=99.0, cost=100.0, rsi=74.0, high20=100.0)

        assert verdict.stance == "reduce"
        assert "20 日高点" in verdict.headline

    def test_below_both_averages_points_at_ma20_as_the_line_to_reclaim(self):
        # 成本 85 → 浮亏 5.9%，不触发止损线，才轮得到「双均线下方」这条规则
        verdict = _verdict(close=80.0, cost=85.0, ma20=88.0, ma60=84.0)

        assert verdict.stance == "reduce"
        assert "MA20 与 MA60" in verdict.headline
        assert "88" in "".join(verdict.reasons)

    def test_below_ma20_only_is_a_watch(self):
        verdict = _verdict(close=92.0, cost=100.0, ma20=95.0, ma60=90.0)

        assert verdict.stance == "watch"
        assert "MA20" in verdict.headline

    def test_healthy_trend_holds(self):
        verdict = _verdict(close=110.0, cost=100.0, rsi=55.0, ma20=105.0)

        assert verdict.stance == "hold"
        assert verdict.levels["cost"] == 100.0

    def test_no_price_is_nodata_not_a_guess(self):
        verdict = _verdict(close=None)

        assert verdict.stance == "nodata"
        assert verdict.levels == {}

    def test_levels_carry_every_price_worth_watching_tomorrow(self):
        verdict = _verdict(close=100.0, cost=95.0, ma20=98.0, ma60=92.0, atr=3.0)

        assert set(verdict.levels) == {
            "cost",
            "close",
            "ma20",
            "ma60",
            "atr_stop",
            "high20",
            "low20",
        }


class TestSummary:
    def _rows(self):
        return [
            {"symbol": "A.SH", "stance": "stop", "shares": 100, "cost": 100.0, "close": 85.0},
            {"symbol": "B.SH", "stance": "hold", "shares": 100, "cost": 100.0, "close": 110.0},
            {"symbol": "C.SH", "stance": "hold", "shares": 100, "cost": 100.0, "close": None},
        ]

    def test_totals_skip_positions_without_a_quote(self):
        from quant_research.review import summarize

        summary = summarize(self._rows())

        assert summary["n_positions"] == 3
        assert summary["n_valued"] == 2  # 没有行情的票不进市值，也不编一个 0
        assert summary["total_cost"] == 100 * 100.0 + 100 * 100.0
        assert summary["total_value"] == 100 * 85.0 + 100 * 110.0
        assert summary["total_pnl"] == -500.0

    def test_the_note_leads_with_risk_not_with_the_best_performer(self):
        from quant_research.review import summarize

        summary = summarize(self._rows())

        assert "止损区" in summary["note"]
        assert summary["worst"]["symbol"] == "A.SH"
        assert summary["worst"]["pnl_pct"] == pytest.approx(-15.0)

    def test_concentration_is_measured_on_market_value(self):
        from quant_research.review import summarize

        summary = summarize(
            [
                {"symbol": "A.SH", "stance": "hold", "shares": 100, "cost": 10.0, "close": 10.0},
                {"symbol": "B.SH", "stance": "hold", "shares": 10, "cost": 10.0, "close": 10.0},
            ]
        )

        assert summary["concentration_pct"] == pytest.approx(1000 / 1100 * 100, abs=0.1)


# --- API 层 -------------------------------------------------------------------


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
    """三只走势明显不同的票：下跌 / 上涨 / 横盘——档位必须各不相同。"""
    import numpy as np
    import pandas as pd

    from quant_research.data.loader import PricePanel
    from quant_research.web import api as api_module

    index = pd.bdate_range("2024-01-01", periods=40, name="date")
    wave = np.sin(np.linspace(0, 6, 40)) * 0.5
    closes = {
        "600000.SH": np.linspace(100.0, 70.0, 40) + wave,  # 单边下跌
        "000001.SZ": np.linspace(100.0, 130.0, 40) + wave,  # 单边上涨
        "300001.SZ": np.linspace(100.0, 100.5, 40) + wave,  # 横盘
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


class TestPortfolioAPI:
    def test_positions_roundtrip_with_live_quotes(self, hold_client, monkeypatch):
        _mock_panel(monkeypatch)

        added = hold_client.post(
            "/api/portfolio", json={"symbol": "600000", "shares": 100, "cost": 100.0}
        )
        assert added.status_code == 200
        row = added.json()["positions"][0]
        assert row["symbol"] == "600000.SH"
        assert row["last_close"] == pytest.approx(70.0, abs=1.0)
        assert row["market_value"] is not None
        assert row["pnl"] < 0

    def test_bad_numbers_are_rejected_before_they_reach_the_book(self, hold_client):
        assert (
            hold_client.post(
                "/api/portfolio", json={"symbol": "600000.SH", "shares": 0, "cost": 100}
            ).status_code
            == 422
        )

    def test_review_puts_the_worst_position_first(self, hold_client, monkeypatch):
        """先风险后机会：复盘列表的第一条必须是止损区，不是涨得最好的。"""
        _mock_panel(monkeypatch)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})
        hold_client.post("/api/portfolio", json={"symbol": "000001.SZ", "shares": 100, "cost": 100})

        payload = hold_client.get("/api/portfolio/review").json()

        stances = [row["stance"] for row in payload["positions"]]
        assert stances[0] == "stop"  # 跌 30% 且跌破 MA20
        assert "take" in stances  # 涨 30% 且 RSI 极高
        assert payload["summary"]["total_pnl"] < 0
        assert payload["summary"]["note"]
        assert payload["as_of"] == "2024-02-23"

    def test_an_empty_book_is_an_empty_review_not_an_error(self, hold_client):
        payload = hold_client.get("/api/portfolio/review").json()

        assert payload["positions"] == []
        assert payload["summary"] is None
        assert payload["unavailable"] is None

    def test_without_a_panel_the_review_says_why_it_is_empty(self, hold_client, monkeypatch):
        from fastapi import HTTPException

        from quant_research.web import api as api_module

        def _no_panel(*args, **kwargs):
            raise HTTPException(status_code=400, detail="还没有落盘数据；先采集")

        monkeypatch.setattr(api_module, "_load_panel", _no_panel)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})

        payload = hold_client.get("/api/portfolio/review").json()

        assert payload["positions"] == []
        assert "先采集" in payload["unavailable"]

    def test_delete_clears_the_position(self, hold_client, monkeypatch):
        _mock_panel(monkeypatch)
        hold_client.post("/api/portfolio", json={"symbol": "600000.SH", "shares": 100, "cost": 100})

        payload = hold_client.delete("/api/portfolio/600000.SH").json()

        assert payload["positions"] == []
