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
    industry_rows: list[list[str]] | None = None,
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

        @staticmethod
        def query_stock_industry() -> _Result:
            return _Result(list(industry_rows or []))

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
        "0.4518",
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
    assert bars["turnover"].iloc[0] == pytest.approx(0.4518)  # baostock 的 turn 原样映射
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


def test_akshare_maps_turnover_column(monkeypatch):
    """东方财富的「换手率」列要进来：没有它，换手率指标对 akshare 源永远缺位。"""

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**_: object) -> object:
            return pd.DataFrame(
                {
                    "日期": ["2024-01-02", "2024-01-03"],
                    "开盘": [100.0, 101.0],
                    "收盘": [101.0, 102.0],
                    "最高": [102.0, 103.0],
                    "最低": [99.0, 100.0],
                    "成交量": [100000, 110000],
                    "成交额": [10100000.0, 11220000.0],
                    "涨跌幅": [1.0, 0.99],
                    "换手率": [1.23, 1.35],
                }
            )

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    frames = AkshareProvider(adjust="qfq").fetch_daily(["600519.SH"])

    bars = frames["600519.SH"]
    assert "turnover" in bars.columns
    assert bars["turnover"].tolist() == pytest.approx([1.23, 1.35])


# 行业表行的顺序是 [updateDate, code, code_name, industry, classification]：
# 代码在第 2 列、行业在第 4 列。这里用真实形状的行守住列序——取错列时
# found 会静默为空，再往上就变成「板块表尚未建立」的假故障。
_INDUSTRY = [
    ["2026-11-09", "sh.600519", "贵州茅台", "C15酒、饮料和精制茶制造业", "证监会行业分类"],
    ["2026-11-09", "sz.000001", "平安银行", "J66货币金融服务", "证监会行业分类"],
    ["2026-11-09", "sz.000002", "万科A", "", "证监会行业分类"],
    ["2026-11-09", "sh.600000", "浦发银行", "J66货币金融服务", "申万行业分类"],
    ["2026-11-09", "not a code", "脏行", "X9脏数据", "证监会行业分类"],
]


def test_list_industries_reads_the_code_and_industry_columns(monkeypatch):
    _install_fake_baostock(monkeypatch, industry_rows=_INDUSTRY)

    found = BaostockProvider().list_industries()

    assert found["600519.SH"] == "酒、饮料和精制茶制造业"  # 分类前缀不是名字
    assert found["000001.SZ"] == "货币金融服务"
    assert found["600000.SH"] == "货币金融服务"
    assert "000002.SZ" not in found  # 行业列为空 → 跳过，而不是存一个空板块
    assert len(found) == 3


def _good_hist(start: str = "19940101") -> pd.DataFrame:
    """一张合法的日线表：日期列从 start 派生，避免测试里写死长列表。"""
    dates = pd.bdate_range("2024-01-02", periods=30).strftime("%Y-%m-%d")
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": [100.0] * 30,
            "收盘": [101.0] * 30,
            "最高": [102.0] * 30,
            "最低": [99.0] * 30,
            "成交量": [100000] * 30,
            "成交额": [10100000.0] * 30,
            "涨跌幅": [0.99] * 30,
            "换手率": [1.23] * 30,
        }
    )


def test_akshare_retries_a_short_window_when_qfq_goes_negative(monkeypatch):
    """000559 实测坑：超长历史 qfq 把早期价格折成负数，校验拒绝。
    缩窗到 2015 重试比「要么全历史要么没有」有用——用户要的是这只票
    能进面板复盘，不是一份哲学上完整的 30 年日线。"""
    calls: list[str] = []

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**kwargs: object) -> pd.DataFrame:
            calls.append(str(kwargs.get("start_date")))
            frame = _good_hist()
            if calls[-1] == "19900101":
                frame.loc[0, "收盘"] = -0.09  # 复权负价：校验必须拒绝
            return frame

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    provider = AkshareProvider(adjust="qfq")

    frames = provider.fetch_daily(["000559.SH"])

    assert len(frames["000559.SH"]) == 30
    assert calls == ["19900101", "20150101"]  # 全历史被拒 → 缩窗重试


def test_akshare_keeps_the_explicit_window_when_qfq_goes_negative(monkeypatch):
    """显式指定过 start 的失败如实上抛——用户要 2020 起就该拿到 2020 起
    的真相或一个明确的错误，不是悄悄被改成 2015 起。"""
    calls: list[str] = []

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**kwargs: object) -> pd.DataFrame:
            calls.append(str(kwargs.get("start_date")))
            frame = _good_hist()
            frame.loc[0, "收盘"] = -0.09
            return frame

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    provider = AkshareProvider(adjust="qfq")
    start = date(2020, 1, 1)

    with pytest.raises(ProviderError, match="non-positive"):
        provider.fetch_daily(["000559.SH"], start=start)

    assert calls == ["20200101"]  # 没有第二次调用：显式窗口不许被改写


def test_akshare_falls_back_to_unadjusted_when_qfq_is_negative_anywhere(monkeypatch):
    """600519 实测：2015 起 qfq 收盘最小值 -143.5（不复权 169.64）——负价
    不是「早期历史」专有，任何窗口都可能。最后一层回退不复权：真实成交
    价能直接用（持仓现价/盈亏），分红除权跳空如实存在，口径写进 source。"""
    calls: list[tuple[str, str]] = []

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**kwargs: object) -> pd.DataFrame:
            calls.append((str(kwargs.get("start_date")), str(kwargs.get("adjust"))))
            frame = _good_hist()
            if kwargs.get("adjust"):  # 任何复权窗口都给负数
                frame.loc[0, "收盘"] = -0.09
            return frame

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    provider = AkshareProvider(adjust="qfq")

    frames = provider.fetch_daily(["600519.SH"])

    assert len(frames["600519.SH"]) == 30
    assert frames["600519.SH"]["close"].min() > 0  # 不复权数据为正
    assert calls == [("19900101", "qfq"), ("20150101", "qfq"), ("19900101", "")]


def test_akshare_unadjusted_fallback_marks_the_adjustment_basis(monkeypatch):
    """会追口径的人要能追到：落盘 meta 标明这只票不是 qfq。"""

    class FakeAkshare:
        @staticmethod
        def stock_zh_a_hist(**kwargs: object) -> pd.DataFrame:
            frame = _good_hist()
            if kwargs.get("adjust"):
                frame.loc[0, "收盘"] = -0.09
            return frame

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare)
    frames = AkshareProvider(adjust="qfq").fetch_daily(["600519.SH"])

    assert "unadjusted" in frames["600519.SH"].attrs.get("source", "")
