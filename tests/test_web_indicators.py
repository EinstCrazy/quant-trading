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


def _settings(tmp_path):
    class _Collection:
        provider = "baostock"
        adjust = "qfq"

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
