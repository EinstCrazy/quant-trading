"""Offline tests for the baostock and SSE providers.

Both wrap network sources, so these tests stub the source layer instead of
reaching out. What needs guarding is the mapping: shares converted to lots,
suspended days dropped, the GBK list file parsed, a declined ``fetch_daily``
raised as an error rather than returned as an empty mapping.
"""

from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import pytest

from quant_research.data.providers import AVAILABLE_PROVIDERS, get_provider
from quant_research.data.providers.akshare_provider import AkshareProvider
from quant_research.data.providers.baostock_provider import BaostockProvider
from quant_research.data.providers.base import ProviderError, SourceUnreachable
from quant_research.data.providers.sse_provider import SseProvider


class _Login:
    error_code = "0"
    error_msg = "success"


class _Result:
    """Stands in for BaoStock's ResultData: iteration via next()/get_row_data()."""

    def __init__(self, rows: list[list[str]], error_code: str = "0") -> None:
        self.error_code = error_code
        self.error_msg = "success"
        self._rows = rows
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


def _install_fake_baostock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history_rows: list[list[str]] | None = None,
    all_stock: object | None = None,
) -> dict[str, int]:
    """Put a fake ``baostock`` module in place; returns its call counters."""
    calls = {"logout": 0}

    class FakeBaostock:
        @staticmethod
        def login() -> _Login:
            return _Login()

        @staticmethod
        def logout() -> None:
            calls["logout"] += 1

        @staticmethod
        def query_history_k_data_plus(code: str, fields: str, **_: object) -> _Result:
            return _Result(list(history_rows or []))

        @staticmethod
        def query_all_stock(day: str | None = None) -> object:
            return all_stock if all_stock is not None else _Result([])

    monkeypatch.setitem(sys.modules, "baostock", FakeBaostock)
    return calls


# one normal day (volume in shares) and one halted day (tradestatus 0)
_HISTORY = [
    [
        "2024-01-02",
        "1558.47",
        "1561.37",
        "1524.94",
        "1531.22",
        "3215644",
        "5440082548.08",
        "-2.3749",
        "1",
    ],
    [
        "2024-01-03",
        "1531.22",
        "1531.22",
        "1531.22",
        "1531.22",
        "0",
        "0.0",
        "0.0",
        "0",
    ],
]


def test_baostock_converts_shares_to_lots_and_drops_halts(monkeypatch):
    calls = _install_fake_baostock(monkeypatch, history_rows=_HISTORY)
    provider = BaostockProvider(adjust="qfq")

    frames = provider.fetch_daily(["600519.SH"], start=date(2024, 1, 2), end=date(2024, 1, 3))

    assert list(frames) == ["600519.SH"]
    bars = frames["600519.SH"]
    assert len(bars) == 1  # the halted day is gone, not frozen into a flat bar
    assert bars.index[0] == pd.Timestamp("2024-01-02")
    assert bars["volume"].iloc[0] == pytest.approx(32156.44)  # shares ÷ 100 = lots
    assert "pct_change" in bars.columns
    assert bars["amount"].iloc[0] == pytest.approx(5.440083e9)
    assert calls["logout"] == 1  # the session is closed even on the success path


def test_baostock_list_symbols_falls_back_to_previous_trading_day(monkeypatch):
    class FakeBaostock:
        @staticmethod
        def login() -> _Login:
            return _Login()

        @staticmethod
        def logout() -> None:
            pass

        @staticmethod
        def query_all_stock(day: str | None = None) -> _Result:
            if day == date.today().strftime("%Y-%m-%d"):
                return _Result([])  # weekend / before the day's first update
            return _Result(
                [
                    ["sh.600519", "1", "贵州茅台"],
                    ["sh.688001", "1", "华兴源创"],
                    ["sh.600000", "0", "浦发银行"],  # halted today: not tradable
                ]
            )

    monkeypatch.setitem(sys.modules, "baostock", FakeBaostock)
    assert BaostockProvider().list_symbols() == ["600519.SH", "688001.SH"]


def test_baostock_rejects_unknown_adjust():
    with pytest.raises(ValueError, match="adjust"):
        BaostockProvider(adjust="magic")


# the list file: GBK, tab separated, header row, 公司代码 before 代码
_LIST_BODY = (
    "公司代码 \t公司简称 \t代码\t简称\t上市日期\r\n"
    "600000\t 浦发银行\t  600000\t  浦发银行\t  1999-11-10\r\n"
    "688001\t 华兴源创\t  688001\t  华兴源创\t  2019-07-22\r\n"
    "600004\t 白云机场\t  600004\t  白云机场\t  2003-04-28\r\n"
)


def test_sse_list_symbols_parses_the_exchange_file(monkeypatch):
    monkeypatch.setattr(SseProvider, "_download", staticmethod(lambda stock_type: _LIST_BODY))
    provider = SseProvider()
    assert provider.list_symbols() == ["600000.SH", "600004.SH", "688001.SH"]


def test_sse_list_symbols_requires_data(monkeypatch):
    monkeypatch.setattr(
        SseProvider,
        "_download",
        staticmethod(lambda stock_type: "公司代码\t公司简称\t代码\t简称\t上市日期\r\n"),
    )
    with pytest.raises(ProviderError, match="no parseable symbol"):
        SseProvider().list_symbols()


def test_sse_fetch_daily_refuses_rather_than_returning_empty():
    # an empty mapping would look like "no trading in this window" to the
    # collector; the refusal must be loud enough to stop the run
    with pytest.raises(ProviderError, match="no free per-symbol price history"):
        SseProvider().fetch_daily(["600519.SH"])


def test_registry_knows_both_new_sources():
    assert "baostock" in AVAILABLE_PROVIDERS
    assert "sse" in AVAILABLE_PROVIDERS
    assert isinstance(get_provider("baostock"), BaostockProvider)
    assert isinstance(get_provider("sse"), SseProvider)


def test_akshare_explains_a_refused_connection(monkeypatch):
    """A throttled source has to say what it probably is, and what to do next."""

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**_: object) -> object:
            raise ConnectionError(
                "('Connection aborted.', RemoteDisconnected('Remote end closed connection'))"
            )

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    with pytest.raises(SourceUnreachable) as info:
        AkshareProvider(adjust="qfq").fetch_daily(["600519.SH"])

    text = str(info.value)
    assert "限流" in text  # names the likely cause
    assert "baostock" in text  # and points at the way out
    assert "Remote end closed" in text  # without dropping the raw detail


def test_akshare_keeps_data_errors_out_of_the_throttling_bucket(monkeypatch):
    """A malformed answer is not a refusal — it must not be dressed up as one."""

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**_: object) -> object:
            raise ValueError("unexpected columns")

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    with pytest.raises(ProviderError) as info:
        AkshareProvider(adjust="qfq").fetch_daily(["600519.SH"])

    assert not isinstance(info.value, SourceUnreachable)
    assert "600519.SH" in str(info.value)
