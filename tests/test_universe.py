"""Symbol ↔ name table tests.

What the autocomplete actually promises:

- typing six digits finds that stock and ranks it above anything else;
- typing a full code with a suffix finds the same stock, not a different one;
- typing a Chinese name finds it by prefix before by substring;
- an empty query returns nothing, because a dropdown of five thousand rows is
  worse than one that waits;
- a missing table degrades to "say so", never to a silent empty list.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from quant_research.config import load_settings
from quant_research.data import universe
from quant_research.data.providers.base import ProviderError
from quant_research.data.providers.mock import MockProvider

FRAME = pd.DataFrame(
    {
        "symbol": ["600519.SH", "600036.SH", "300750.SZ", "000001.SZ", "601166.SH"],
        "name": ["贵州茅台", "招商银行", "宁德时代", "平安银行", "兴业银行"],
        "updated_at": "2026-11-03T06:28:34+00:00",
    }
)


# --- search ----------------------------------------------------------------


def test_a_bare_code_finds_the_stock():
    hits = universe.search(FRAME, "600519")
    assert [hit.symbol for hit in hits] == ["600519.SH"]
    assert hits[0].name == "贵州茅台"


def test_a_code_with_exchange_finds_the_same_stock():
    assert [hit.symbol for hit in universe.search(FRAME, "600519.SH")] == ["600519.SH"]
    # the user may type the exchange first; the digits are what identify a stock
    assert [hit.symbol for hit in universe.search(FRAME, "sh600519")] == ["600519.SH"]


def test_a_code_prefix_ranks_ahead_of_names_containing_the_digits():
    hits = universe.search(FRAME, "6005")
    assert hits[0].symbol == "600519.SH"


def test_a_name_prefix_ranks_ahead_of_a_substring():
    """「宁德」 is a prefix of 宁德时代; nothing else should outrank it."""
    assert [hit.symbol for hit in universe.search(FRAME, "宁德")] == ["300750.SZ"]


def test_a_name_substring_finds_everything_that_contains_it():
    hits = universe.search(FRAME, "银行")
    assert {hit.symbol for hit in hits} == {"600036.SH", "000001.SZ", "601166.SH"}


def test_an_empty_query_lists_nothing():
    """Opening a dropdown with the whole market in it is not a feature."""
    assert universe.search(FRAME, "") == []
    assert universe.search(FRAME, "   ") == []


def test_an_empty_table_matches_nothing():
    assert universe.search(universe.load("/nonexistent-root"), "茅台") == []


def test_limit_is_respected():
    assert len(universe.search(FRAME, "银行", limit=2)) == 2


def test_a_nameless_row_still_completes_by_code():
    """A source without names degrades to codes; it does not stop working."""
    frame = pd.DataFrame({"symbol": ["600519.SH"], "name": [""], "updated_at": ""})
    assert [hit.symbol for hit in universe.search(frame, "600519")] == ["600519.SH"]


# --- storage ---------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    universe.save({"600519.SH": "贵州茅台"}, tmp_path)
    frame = universe.load(tmp_path)

    assert frame.iloc[0]["symbol"] == "600519.SH"
    assert frame.iloc[0]["name"] == "贵州茅台"
    assert universe.table_path(tmp_path).is_file()


def test_saving_nothing_is_refused(tmp_path):
    """Overwriting a good table with an empty one would break every input quietly."""
    with pytest.raises(ValueError, match="empty"):
        universe.save({}, tmp_path)


def test_a_missing_table_loads_empty(tmp_path):
    assert universe.load(tmp_path).empty


def test_refresh_persists_the_provider_universe(tmp_path):
    count = universe.refresh(MockProvider(), tmp_path)

    assert count > 0
    assert len(universe.load(tmp_path)) == count


def test_refresh_wraps_provider_failures():
    class Broken(MockProvider):
        def list_named_symbols(self) -> dict[str, str]:
            raise RuntimeError("boom")

    with pytest.raises(ProviderError, match="boom"):
        universe.refresh(Broken(), "/tmp/unused")


# --- the web contract ------------------------------------------------------


def test_the_endpoint_completes_by_name(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import api as web_api
    from quant_research.web import create_app

    settings = load_settings()
    monkeypatch.setattr(
        web_api,
        "load_settings",
        lambda: replace(settings, data=replace(settings.data, directory=tmp_path)),
    )
    universe.save({"600519.SH": "贵州茅台", "300750.SZ": "宁德时代"}, tmp_path)

    payload = TestClient(create_app()).get("/api/symbols", params={"q": "茅台"}).json()

    assert payload["available"] is True
    assert payload["total"] == 2
    assert payload["items"] == [{"symbol": "600519.SH", "name": "贵州茅台"}]


def test_the_endpoint_says_so_when_the_table_is_missing(tmp_path, monkeypatch):
    """Silence would look like "no such stock", which is a different statement."""
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import api as web_api
    from quant_research.web import create_app

    settings = load_settings()
    monkeypatch.setattr(
        web_api,
        "load_settings",
        lambda: replace(settings, data=replace(settings.data, directory=tmp_path)),
    )

    payload = TestClient(create_app()).get("/api/symbols", params={"q": "茅台"}).json()

    assert payload["available"] is False
    assert payload["items"] == []


def test_the_endpoint_rejects_an_overlong_query(tmp_path, monkeypatch):
    pytest.importorskip("fastapi", reason="web extra not installed")
    from fastapi.testclient import TestClient

    from quant_research.web import create_app

    response = TestClient(create_app()).get("/api/symbols", params={"q": "x" * 40})
    assert response.status_code == 422


# --- 板块表 -------------------------------------------------------------------


def test_sector_table_roundtrip(tmp_path):
    from quant_research.data.universe import load_sectors, save_sectors, sectors_path

    target = save_sectors({"600519.SH": "白酒", "000001.SZ": "银行"}, tmp_path)

    assert target == sectors_path(tmp_path)
    frame = load_sectors(tmp_path)
    assert set(zip(frame["symbol"], frame["sector"])) == {
        ("600519.SH", "白酒"),
        ("000001.SZ", "银行"),
    }


def test_empty_sector_table_is_refused(tmp_path):
    """覆盖一张好表为空表，等于悄悄剥掉所有板块标签——这种失败必须挡下。"""
    import pytest

    from quant_research.data.universe import save_sectors

    with pytest.raises(ValueError):
        save_sectors({}, tmp_path)


def test_missing_sector_table_reads_as_empty(tmp_path):
    from quant_research.data.universe import load_sectors

    assert load_sectors(tmp_path).empty


def test_refresh_sectors_requires_data_from_the_source(tmp_path):
    """源不发布行业时明确报错，而不是把空表存成「已刷新」。"""
    import pytest

    from quant_research.data.providers.base import MarketDataProvider, ProviderError
    from quant_research.data.universe import refresh_sectors

    class Empty(MarketDataProvider):
        name = "empty"

        def list_symbols(self):
            return ["600519.SH"]

        def fetch_daily(self, symbols, start=None, end=None):
            return {}

    with pytest.raises(ProviderError):
        refresh_sectors(Empty(), tmp_path)
