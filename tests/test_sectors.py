"""板块分析：板块汇总 + 板块内个股。

守住的三条：板块涨幅是等权与中位并给（不被一只权重带偏）；单票板块没有
「领涨/领跌」可言，不编；个股的「超额」是相对本板块——板块普涨时跑输
板块本身就是信号。
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.sectors import analyze, members

SECTORS = {
    "600000.SH": "银行",
    "600036.SH": "银行",
    "600519.SH": "白酒",
    "000001.SZ": "电子",
    "300750.SZ": "电子",
}


def _panel(closes: dict[str, list[float]], with_amount: bool = True) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=len(next(iter(closes.values()))))
    prices = pd.DataFrame(closes, index=index)
    extra = {
        "high": prices * 1.01,
        "low": prices * 0.99,
        "volume": prices * 0 + 1000.0,
        "amount": prices * 1e6,  # 元：价 10 元 × 100 万股 = 1000 万元
    }
    if not with_amount:
        extra.pop("amount")
    return PricePanel(prices=prices, extra=extra)


@pytest.fixture()
def panel() -> PricePanel:
    """30 bars flat, then five bars of movement — so the 5-day window is exact."""

    def line(base: float, tail: list[float]) -> list[float]:
        return [base] * 25 + [base * (1 + pct / 100) for pct in tail]

    return _panel(
        {
            # 5 日涨幅 +10% / +4%
            "600000.SH": line(10.0, [0, 2, 5, 8, 10]),
            "600036.SH": line(10.0, [0, 1, 2, 3, 4]),
            # 单票板块，5 日 -2%
            "600519.SH": line(100.0, [0, 0, -0.5, -1, -2]),
            # 5 日 -10% / +2%
            "000001.SZ": line(20.0, [0, -2.5, -5, -7.5, -10]),
            "300750.SZ": line(20.0, [0, 0.5, 1, 1.5, 2]),
        }
    )


class TestAnalyze:
    def test_sector_aggregates_are_equal_weighted_with_a_median(self, panel):
        rows = {row.sector: row for row in analyze(panel, SECTORS, days=5)}

        bank = rows["银行"]
        assert bank.n == 2
        assert bank.change_nd == pytest.approx(7.0, abs=0.01)  # (10% + 4%) / 2
        assert bank.up_ratio == 100.0
        assert bank.median_change_nd == pytest.approx(7.0, abs=0.01)
        assert bank.dispersion is not None and bank.dispersion > 0  # 内部有差异

    def test_sectors_are_sorted_by_strength(self, panel):
        rows = analyze(panel, SECTORS, days=5)

        assert [row.sector for row in rows] == ["银行", "白酒", "电子"]  # +7% > -2% > -4%
        assert rows[0].change_nd > rows[-1].change_nd

    def test_leaders_and_laggards_rank_within_the_sector(self, panel):
        rows = {row.sector: row for row in analyze(panel, SECTORS, days=5)}

        bank = rows["银行"]
        assert bank.leaders[0]["symbol"] == "600000.SH"
        assert bank.laggards == ()  # 2 只票不足两倍 top，不重复编领跌

        electronics = rows["电子"]
        assert [item["symbol"] for item in electronics.leaders] == ["300750.SZ", "000001.SZ"]
        assert electronics.laggards == ()

    def test_a_single_stock_sector_has_no_dispersion(self, panel):
        rows = {row.sector: row for row in analyze(panel, SECTORS, days=5)}

        liquor = rows["白酒"]
        assert liquor.n == 1
        assert liquor.dispersion is None  # 一只票没有「内部差异」

    def test_amount_is_summed_and_reported_in_yi(self, panel):
        rows = {row.sector: row for row in analyze(panel, SECTORS, days=5)}

        assert rows["银行"].amount == pytest.approx((11.0 + 10.4) * 1e6 / 1e8, abs=0.01)

    def test_missing_amount_does_not_break_the_summary(self):
        panel = _panel({"600000.SH": [10.0] * 30}, with_amount=False)

        row = analyze(panel, SECTORS, days=5)[0]

        assert row.amount is None

    def test_too_short_a_panel_raises(self):
        panel = _panel({"600000.SH": [10.0] * 4})

        with pytest.raises(ValueError):
            analyze(panel, SECTORS, days=5)

    def test_symbols_without_a_sector_are_ignored(self, panel):
        rows = analyze(panel, {"600000.SH": "银行"}, days=5)

        assert [row.sector for row in rows] == ["银行"]
        assert rows[0].n == 1


class TestMembers:
    def test_excess_is_measured_against_the_sector(self, panel):
        rows = {row.symbol: row for row in members(panel, SECTORS, "银行", days=5)}

        assert rows["600000.SH"].excess_nd == pytest.approx(3.0, abs=0.01)  # 10% vs 7%
        assert rows["600036.SH"].excess_nd == pytest.approx(-3.0, abs=0.01)

    def test_members_are_sorted_by_relative_strength(self, panel):
        assert [row.symbol for row in members(panel, SECTORS, "银行", days=5)] == [
            "600000.SH",
            "600036.SH",
        ]

    def test_ma20_and_rsi_are_computed_per_stock(self, panel):
        row = members(panel, SECTORS, "银行", days=5)[0]

        assert row.above_ma20 is True
        assert row.rsi_14 is not None and 0 < row.rsi_14 <= 100
        assert row.volume_ratio == pytest.approx(1.0, abs=0.01)

    def test_an_unknown_sector_returns_nothing(self, panel):
        assert members(panel, SECTORS, "不存在的板块", days=5) == []


# --- API ----------------------------------------------------------------------


@pytest.fixture()
def sector_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.config import ReviewSettings
    from quant_research.web import api as api_module
    from quant_research.web import create_app

    data = tmp_path / "data"
    data.mkdir()

    class _Collection:
        provider = "baostock"
        adjust = "qfq"
        output_directory = tmp_path / "raw"

    class _Data:
        directory = data

    class _Settings:
        collection = _Collection()
        data = _Data()
        review = ReviewSettings()

    settings = _Settings()
    monkeypatch.setattr(api_module, "load_settings", lambda *args, **kwargs: settings)
    return TestClient(create_app()), settings


def _mock_panel(monkeypatch, panel):
    from quant_research.web import api as api_module

    monkeypatch.setattr(api_module, "_load_panel", lambda *a, **k: panel)


class TestSectorAPI:
    def test_without_a_sector_table_the_error_names_the_command(self, sector_client):
        client, _ = sector_client

        response = client.get("/api/sectors/analysis")

        assert response.status_code == 400
        assert "quant-symbols --sectors" in response.json()["detail"]

    def test_analysis_reports_coverage_honestly(self, sector_client, monkeypatch, panel):
        client, settings = sector_client
        from quant_research.data import universe

        universe.save_sectors(SECTORS, settings.data.directory)
        _mock_panel(monkeypatch, panel)

        payload = client.get("/api/sectors/analysis?days=5").json()

        assert payload["n_sectors"] == 3
        assert payload["n_covered"] == 5
        assert payload["n_symbols"] == 5
        assert payload["sectors"][0]["sector"] == "银行"

    def test_members_for_one_sector(self, sector_client, monkeypatch, panel):
        client, settings = sector_client
        from quant_research.data import universe

        universe.save_sectors(SECTORS, settings.data.directory)
        _mock_panel(monkeypatch, panel)

        payload = client.get("/api/sectors/members?sector=银行&days=5").json()

        assert payload["n"] == 2
        assert {row["symbol"] for row in payload["members"]} == {"600000.SH", "600036.SH"}

    def test_an_unknown_sector_is_a_404_not_an_empty_list(self, sector_client, monkeypatch, panel):
        client, settings = sector_client
        from quant_research.data import universe

        universe.save_sectors(SECTORS, settings.data.directory)
        _mock_panel(monkeypatch, panel)

        assert client.get("/api/sectors/members?sector=火星板块").status_code == 404
