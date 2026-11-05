"""Contract tests for the indicator panel.

The panel is pointed at a fake price panel and a temporary indicator directory,
so these tests cover the contract — what is returned, what is written where, and
what is left out — while the arithmetic itself has its own tests.

One thing is asserted repeatedly here: every number comes with the text that
explains it. An interface that shows RSI=56.3 without saying what RSI measures
is worse than showing nothing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_research.data.loader import PricePanel
from quant_research.web import api
from quant_research.web.app import create_app

_NAMES = ("600000.SH", "600519.SH", "000001.SZ")


def _panel(days: int = 120) -> PricePanel:
    """Three names with all six fields, so every indicator can be computed."""
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    columns = {}
    for position, name in enumerate(_NAMES):
        rng = np.random.default_rng(position)
        columns[name] = 100 * np.cumprod(1 + rng.normal(0.0004 * (position + 1), 0.01, days))
    close = pd.DataFrame(columns, index=index)
    return PricePanel(
        prices=close,
        extra={
            "open": close.shift(1).bfill(),
            "high": close * 1.005,
            "low": close * 0.995,
            "volume": pd.DataFrame(1_000.0, index=index, columns=list(_NAMES)),
            "amount": close * 1_000 * 100,
        },
    )


def _settings(tmp_path, **overrides):
    class _Collection:
        provider = "baostock"
        adjust = "qfq"
        output_directory = tmp_path / "raw"

    class _Data:
        directory = "data"

    class _Indicators:
        output_directory = tmp_path / "indicators"
        format = "parquet"
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

    for key, value in overrides.items():
        setattr(_Indicators, key, value)

    class _Settings:
        collection = _Collection()
        data = _Data()
        indicators = _Indicators()

    return _Settings()


@pytest.fixture
def client(tmp_path, monkeypatch):
    panel = _panel()
    monkeypatch.setattr(api, "_load_panel", lambda provider="baostock", adjust="qfq": panel)
    monkeypatch.setattr(
        api, "_latest_session", lambda provider, adjust: str(panel.prices.index.max().date())
    )
    monkeypatch.setattr(api, "load_settings", lambda *args, **kwargs: _settings(tmp_path))
    # a small in-repo name table: the 404 hints read Chinese names from it, and
    # the tests must not depend on whatever the real table holds today
    monkeypatch.setattr(
        api.universe,
        "load",
        lambda directory: pd.DataFrame(
            {"symbol": list(_NAMES), "name": ["浦发银行", "贵州茅台", "平安银行"], "updated_at": ""}
        ),
    )
    return TestClient(create_app())


def test_the_board_returns_every_indicator(client):
    payload = client.get("/api/indicators").json()

    fields = {row["field"] for row in payload["rows"]}
    assert "rsi_14" in fields
    assert "macd_hist" in fields
    assert "boll_upper" in fields
    assert "kdj_k" in fields  # needs high/low
    assert "vwap" in fields  # needs volume and amount
    assert payload["fields"] == len(payload["rows"])
    assert payload["symbols"] == len(_NAMES)
    assert payload["as_of"] == str(_panel().prices.index.max().date())


def test_every_row_carries_the_text_that_explains_it(client):
    payload = client.get("/api/indicators").json()

    for row in payload["rows"]:
        assert row["label"]
        assert row["unit"]
        assert row["typical_range"]
        assert row["description"]
        assert row["how_to_use"]
        assert row["caution"]  # every indicator has one way to be misread


def test_the_label_carries_the_period(client):
    """A panel that says only "RSI" leaves 14 and 6 indistinguishable."""
    rows = {row["field"]: row for row in client.get("/api/indicators").json()["rows"]}

    assert "14" in rows["rsi_14"]["label"]
    assert "5" in rows["ma_5"]["label"]
    assert "MACD" in rows["macd_hist"]["label"]  # no period in this name, none added


def test_the_summary_is_the_last_cross_section(client):
    payload = client.get("/api/indicators").json()
    rows = {row["field"]: row for row in payload["rows"]}

    rsi = rows["rsi_14"]
    assert rsi["symbols"] == len(_NAMES)
    assert 0.0 <= rsi["low"] <= rsi["median"] <= rsi["high"] <= 100.0
    # a value that only exists for some names still reports how many it covers
    assert rows["adx_14"]["symbols"] <= len(_NAMES)


def test_the_params_are_reported_so_the_numbers_can_be_read_correctly(client):
    params = client.get("/api/indicators").json()["params"]

    assert params["rsi_period"] == 14
    assert params["ma_periods"] == [5, 20]
    assert params["boll_std"] == 2.0


def test_the_first_request_computes_and_stores(client, tmp_path):
    client.get("/api/indicators")

    target = tmp_path / "indicators" / "baostock_qfq"
    assert (target / "meta.json").is_file()
    assert (target / "rsi_14.parquet").is_file()


def test_the_second_request_reads_what_was_stored(client, tmp_path, monkeypatch):
    client.get("/api/indicators")

    def boom(*args, **kwargs):
        raise AssertionError("should not recompute when the bundle is already on disk")

    monkeypatch.setattr(api, "compute_all", boom)
    payload = client.get("/api/indicators").json()

    assert payload["rows"]


def test_rebuild_recomputes_even_when_a_bundle_exists(client, tmp_path, monkeypatch):
    client.get("/api/indicators")
    calls = []
    original = api.compute_all

    def counted(panel, params=None):
        calls.append(1)
        return original(panel, params)

    monkeypatch.setattr(api, "compute_all", counted)
    client.get("/api/indicators", params={"rebuild": True})

    assert calls


def test_a_single_name_returns_its_own_values(client):
    payload = client.get("/api/indicators/600519.SH").json()

    assert payload["symbol"] == "600519.SH"
    assert payload["values"]
    values = {row["field"]: row for row in payload["values"]}
    assert values["rsi_14"]["value"] is not None
    assert values["rsi_14"]["unit"] == "0–100"


def test_an_unknown_name_is_a_404(client):
    response = client.get("/api/indicators/999999.SH")

    assert response.status_code == 404
    assert "不在指标集合里" in response.json()["detail"]


def test_a_mistyped_suffix_is_forgiven(client):
    """600519.HZ is 600519.SH with a typo'd exchange, not a different stock."""
    payload = client.get("/api/indicators/600519.HZ").json()

    assert payload["symbol"] == "600519.SH"


def test_a_bare_code_is_completed_with_its_exchange(client):
    """600519 alone is unambiguous — the digits say Shanghai."""
    payload = client.get("/api/indicators/600519").json()

    assert payload["symbol"] == "600519.SH"


def test_a_missing_code_names_the_stock_it_would_be(client):
    """The 404 says which stock is missing, in the language the user typed."""
    response = client.get("/api/indicators/000002.HZ")

    assert response.status_code == 404
    assert "000002.SZ 不在指标集合里" in response.json()["detail"]


def test_a_code_with_no_stored_rows_names_the_fix(client):
    """Not a cache problem: the source has no rows for it, so name the command."""
    response = client.get("/api/indicators/300750.SZ")

    assert response.status_code == 404
    assert "quant-collect --provider baostock --symbols 300750.SZ" in response.json()["detail"]


def test_a_code_stored_under_another_source_points_there(client, tmp_path):
    """The store has it, just under another source's folder — say which one."""
    daily = tmp_path / "raw" / "akshare" / "daily"
    daily.mkdir(parents=True)
    (daily / "300750.SZ.parquet").write_bytes(b"")

    response = client.get("/api/indicators/300750.SZ")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "akshare 源里有" in detail
    assert "切换数据源" in detail


def test_a_source_with_no_data_says_so_rather_than_failing_obscurely(client, monkeypatch):
    def boom(provider: str, adjust: str):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"{provider} 还没有落盘数据")

    monkeypatch.setattr(api, "_load_panel", boom)

    response = client.get("/api/indicators")

    assert response.status_code == 400
    assert "还没有落盘数据" in response.json()["detail"]


def test_a_stored_bundle_older_than_the_panel_is_flagged(client, monkeypatch, tmp_path):
    client.get("/api/indicators")
    monkeypatch.setattr(api, "_latest_session", lambda provider, adjust: "2026-01-01")

    assert client.get("/api/indicators").json()["stale"] is True


# --- the series a chart draws ----------------------------------------------


def _panels(client: TestClient, symbol: str = "600519.SH", **params) -> dict:
    payload = client.get(f"/api/indicators/{symbol}/series", params=params).json()
    return {panel["key"]: panel for panel in payload["panels"]}


def test_the_series_arrives_panel_by_panel(client):
    """One chart per panel: RSI and a price share no scale, and two y-axes invite
    reading crossings that mean nothing."""
    panels = _panels(client)

    assert list(panels) == ["price", "rsi", "macd", "kdj"]
    lengths = {len(line["points"]) for panel in panels.values() for line in panel["lines"]}
    assert lengths == {120}  # every line covers the same window


def test_the_price_panel_carries_candles_with_the_band_and_the_averages(client):
    """The candle is the close (no separate close line); overlays ride on top."""
    panel = _panels(client)["price"]

    candles = panel["candles"]
    assert len(candles) == 120
    assert [len(row) for row in candles[:3]] == [5, 5, 5]  # date, o, h, l, c
    fields = [line["field"] for line in panel["lines"]]
    assert {"boll_mid", "boll_upper", "boll_lower"} <= set(fields)
    assert {"ma_5", "ma_20"} <= set(fields)  # the periods the settings asked for
    assert "close" not in fields  # the candles already show it


def test_the_oscillator_panels_carry_no_candles(client):
    """RSI of 56 has no OHLC; an empty candle list keeps the chart honest."""
    panels = _panels(client)

    assert panels["rsi"]["candles"] == []
    assert panels["macd"]["candles"] == []


def test_the_histogram_is_sent_as_bars(client):
    """The sign of the histogram is the point, and a line hides it."""
    macd = _panels(client)["macd"]

    kinds = {line["field"]: line["kind"] for line in macd["lines"]}
    assert kinds["macd_hist"] == "bar"
    assert kinds["macd_dif"] == "line"
    assert kinds["macd_dea"] == "line"


def test_the_reference_levels_travel_with_the_panel(client):
    panels = _panels(client)

    assert panels["rsi"]["reference"] == [20.0, 80.0]
    assert panels["kdj"]["reference"] == [20.0, 80.0]
    assert panels["price"]["reference"] == []


def test_the_warm_up_is_a_gap_not_a_zero(client):
    """Null means "not enough history yet". Sending 0 would draw a real RSI of 0."""
    rsi = _panels(client, days=500)["rsi"]["lines"][0]["points"]

    values = [value for _, value in rsi]
    # 14 changes need 15 closes, so the first 14 slots are genuinely empty
    assert values[:14] == [None] * 14
    assert values[14] is not None
    assert all(value is not None for value in values[14:])


def test_the_window_follows_days(client):
    points = _panels(client, days=30)["price"]["lines"][0]["points"]

    assert len(points) == 30


def test_a_window_outside_the_range_is_rejected(client):
    """Clamping would silently return 500 days when 2000 were asked for."""
    assert client.get("/api/indicators/600519.SH/series", params={"days": 3}).status_code == 422
    assert client.get("/api/indicators/600519.SH/series", params={"days": 501}).status_code == 422


def test_an_unknown_name_has_no_series(client):
    assert client.get("/api/indicators/999999.SH/series").status_code == 404


def test_changing_the_period_moves_the_line_instead_of_emptying_the_chart(tmp_path, monkeypatch):
    """Panels are looked up by family: ``rsi_period: 21`` still yields an RSI panel."""
    panel = _panel()
    monkeypatch.setattr(api, "_load_panel", lambda provider="baostock", adjust="qfq": panel)
    monkeypatch.setattr(
        api, "_latest_session", lambda provider, adjust: str(panel.prices.index.max().date())
    )
    monkeypatch.setattr(
        api, "load_settings", lambda *args, **kwargs: _settings(tmp_path, rsi_period=21)
    )
    client = TestClient(create_app())

    rsi = _panels(client)["rsi"]

    assert rsi["lines"][0]["field"] == "rsi_21"
    assert "21" in rsi["lines"][0]["label"]


def test_a_newly_stored_name_rebuilds_the_bundle(tmp_path, monkeypatch):
    """Collecting a stock then opening it must just work — no command to remember."""
    current = {"panel": _panel()}
    monkeypatch.setattr(
        api, "_load_panel", lambda provider="baostock", adjust="qfq": current["panel"]
    )
    monkeypatch.setattr(
        api,
        "_latest_session",
        lambda provider, adjust: str(current["panel"].prices.index.max().date()),
    )
    monkeypatch.setattr(api, "load_settings", lambda *args, **kwargs: _settings(tmp_path))
    client = TestClient(create_app())

    assert client.get("/api/indicators").json()["symbols"] == 3

    # a fourth name lands in the store; the next read must include it
    close = current["panel"].prices
    extended = close.assign(**{"603881.SH": close.iloc[:, 0] * 0.9})
    extra = {
        key: frame.assign(**{"603881.SH": frame.iloc[:, 0]})
        for key, frame in current["panel"].extra.items()
    }
    current["panel"] = PricePanel(prices=extended, extra=extra)

    assert client.get("/api/indicators").json()["symbols"] == 4
    assert client.get("/api/indicators/603881").json()["symbol"] == "603881.SH"
