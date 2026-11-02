"""Futu provider tests.

OpenD is a user-side install and cannot be started from here, so the gateway is
faked at the SDK boundary. What these actually cover is the part that is ours:
code translation, column renaming, shares-to-lots, paging, and the two ways a
call can fail. The first run against a real gateway is still the real test.
"""

from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_research.data.providers.base import (
    ProviderError,
    ProviderUnavailable,
    SourceUnreachable,
)
from quant_research.data.providers.futu_provider import (
    FutuProvider,
    from_futu_code,
    to_futu_code,
)

_DAYS = ("2024-01-02", "2024-01-03")


class _FakeContext:
    """Stands in for OpenQuoteContext, returning canned Futu-shaped frames."""

    def __init__(self, klines=None, snapshot=None, basic=None, pages=1, ret=0) -> None:
        self._klines = klines or {}
        self._snapshot = snapshot
        self._basic = basic
        self._pages = pages
        self._ret = ret
        self.calls = 0
        self.closed = False

    def request_history_kline(
        self, code, start=None, end=None, ktype=None, autype=None, max_count=None, page_req_key=None
    ):
        self.calls += 1
        if self._ret != 0:
            return self._ret, "quota exceeded", None
        frame = self._klines.get(code)
        if frame is None:
            return 0, pd.DataFrame(), None
        page = 0 if page_req_key is None else int(page_req_key)
        more = str(page + 1) if page + 1 < self._pages else None
        return 0, frame, more

    def get_market_snapshot(self, codes):
        return self._ret, self._snapshot if self._ret == 0 else "nope"

    def get_stock_basicinfo(self, market=None, stock_type=None):
        return self._ret, self._basic if self._ret == 0 else "nope"

    def close(self):
        self.closed = True


def _sdk(context=None, broken=False) -> SimpleNamespace:
    def open_context(host, port):
        if broken:
            raise OSError("connection refused")
        return context if context is not None else _FakeContext()

    return SimpleNamespace(
        RET_OK=0,
        RET_ERROR=-1,
        KLType=SimpleNamespace(K_DAY="K_DAY"),
        AuType=SimpleNamespace(QFQ="qfq", HFQ="hfq", NONE="none"),
        Market=SimpleNamespace(SH="SH", SZ="SZ", HK="HK", US="US"),
        SecurityType=SimpleNamespace(STOCK="STOCK"),
        OpenQuoteContext=open_context,
    )


def _klines(code: str = "SH.600519", days=_DAYS, volume: float = 1000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [code] * len(days),
            "time_key": list(days),
            "open": [10.0] * len(days),
            "high": [10.5] * len(days),
            "low": [9.5] * len(days),
            "close": [10.2] * len(days),
            "volume": [volume] * len(days),
            "turnover": [1.0e7] * len(days),
        }
    )


def _provider(context, monkeypatch, **kwargs) -> FutuProvider:
    provider = FutuProvider(**kwargs)
    monkeypatch.setattr(FutuProvider, "_futu", staticmethod(lambda: _sdk(context)))
    return provider


def test_codes_translate_both_ways():
    assert to_futu_code("600519.SH") == "SH.600519"
    assert from_futu_code("SH.600519") == "600519.SH"
    assert from_futu_code(to_futu_code("000001.SZ")) == "000001.SZ"


def test_history_is_renamed_and_converted_to_lots(monkeypatch):
    """Futu reports shares; the store keeps lots, like every other provider."""
    context = _FakeContext(klines={"SH.600519": _klines(volume=1000.0)})
    provider = _provider(context, monkeypatch)

    bars = provider.fetch_daily(["600519.SH"], start=date(2024, 1, 1), end=date(2024, 1, 31))[
        "600519.SH"
    ]

    assert list(bars.columns) == ["open", "high", "low", "close", "volume", "amount"]
    assert bars["volume"].tolist() == [10.0, 10.0]  # 1000 shares -> 10 lots
    assert bars["amount"].tolist() == [1.0e7, 1.0e7]
    assert str(bars.index[0].date()) == "2024-01-02"
    assert bars.index.name == "date"


def test_a_symbol_with_no_rows_is_omitted_rather_than_empty(monkeypatch):
    context = _FakeContext(klines={})
    provider = _provider(context, monkeypatch)

    assert provider.fetch_daily(["600519.SH"]) == {}


def test_history_pages_are_concatenated(monkeypatch):
    """A range longer than one response must not come back silently truncated."""
    context = _FakeContext(klines={"SH.600519": _klines()}, pages=3)
    provider = _provider(context, monkeypatch)

    bars = provider.fetch_daily(["600519.SH"])["600519.SH"]

    assert len(bars) == 2  # 2 rows per page x 3 pages -> deduplicated by date
    assert context.calls == 3


def test_a_failed_history_call_raises_rather_than_returning_nothing(monkeypatch):
    context = _FakeContext(ret=-1)
    provider = _provider(context, monkeypatch)

    with pytest.raises(ProviderError, match="quota exceeded"):
        provider.fetch_daily(["600519.SH"])
    assert context.closed  # the gateway is released even on failure


def test_list_symbols_normalises_the_codes(monkeypatch):
    context = _FakeContext(basic=pd.DataFrame({"code": ["SH.600519", "SZ.000001"]}))
    provider = _provider(context, monkeypatch)

    assert provider.list_symbols() == ["000001.SZ", "600519.SH"]


def test_snapshot_translates_codes_and_lots(monkeypatch):
    snapshot = pd.DataFrame(
        {
            "code": ["SH.600519"],
            "last_price": [10.2],
            "open_price": [10.0],
            "high_price": [10.5],
            "low_price": [9.5],
            "prev_close_price": [10.1],
            "volume": [2000.0],
            "turnover": [2.0e7],
            "turnover_rate": [0.01],
        }
    )
    context = _FakeContext(snapshot=snapshot)
    provider = _provider(context, monkeypatch)

    quotes = provider.fetch_snapshot(["600519.SH"])

    assert list(quotes.index) == ["600519.SH"]
    assert quotes.loc["600519.SH", "volume"] == 20.0  # shares -> lots
    assert quotes.loc["600519.SH", "last_price"] == 10.2


def test_an_unreachable_gateway_is_reported_as_such(monkeypatch):
    provider = FutuProvider()
    monkeypatch.setattr(FutuProvider, "_futu", staticmethod(lambda: _sdk(broken=True)))

    with pytest.raises(SourceUnreachable, match="OpenD"):
        provider.list_symbols()


def test_a_missing_sdk_says_how_to_install_it(monkeypatch):
    """Make ``import futu`` itself fail — replacing _futu would bypass its own guard."""
    monkeypatch.setitem(sys.modules, "futu", None)

    with pytest.raises(ProviderUnavailable, match=r"pip install -e '\.\[futu\]'"):
        FutuProvider().list_symbols()


def test_unsupported_markets_are_refused_up_front():
    with pytest.raises(ValueError, match="markets"):
        FutuProvider(markets=("BJ",))
    with pytest.raises(ValueError, match="adjust"):
        FutuProvider(adjust="sideways")


def test_the_registry_builds_it_by_name(monkeypatch):
    from quant_research.data.providers import AVAILABLE_PROVIDERS, get_provider

    assert "futu" in AVAILABLE_PROVIDERS
    assert isinstance(get_provider("futu"), FutuProvider)
