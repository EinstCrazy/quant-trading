"""Regulatory event tests.

The decisions worth pinning down:

- 立案 is not 立案受理 (a court accepting a suit) and not 尽职调查;
- a penalty from the finance ministry does not close a CSRC case;
- an open case stops counting after a year, a closed one immediately;
- a screen excludes investigated names by default, and says so.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.fundamentals import regulatory
from quant_research.fundamentals.regulatory import (
    INVESTIGATION,
    RESOLUTION,
    RegulatoryEvent,
    active,
    load_manual,
    load_state,
    merge,
    recent_sessions,
    save_state,
    scan,
)
from quant_research.scoring import screen


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "name", "kind", "date", "title", "url"])


def _open(symbol: str = "300477.SZ", day: str = "2026-11-02") -> dict:
    return {
        "symbol": symbol,
        "name": "ST合纵",
        "kind": INVESTIGATION,
        "date": day,
        "title": "关于收到中国证监会立案告知书的公告",
        "url": "",
    }


def _close(symbol: str = "300477.SZ", day: str = "2026-11-20") -> dict:
    return {
        "symbol": symbol,
        "name": "ST合纵",
        "kind": RESOLUTION,
        "date": day,
        "title": "关于收到行政处罚决定书的公告",
        "url": "",
    }


# --- telling a filing from a lawsuit --------------------------------------


def test_a_csrc_notice_is_an_investigation():
    assert regulatory._classify("关于收到中国证监会立案告知书的公告") == INVESTIGATION


def test_a_court_accepting_a_suit_is_not_an_investigation():
    """立案受理 is a civil case; flagging it would catch half the market."""
    assert regulatory._classify("关于买卖合同纠纷案立案受理的公告") is None
    assert regulatory._classify("关于合同纠纷立案的公告") is None


def test_due_diligence_is_not_an_investigation():
    """尽职调查 contains 调查, which is why the rule looks for 立案 instead."""
    assert regulatory._classify("独立财务顾问在充分尽职调查和内部核查的基础上") is None


def test_a_penalty_closes_the_case():
    assert regulatory._classify("关于收到《行政处罚决定书》的公告") == RESOLUTION
    assert regulatory._classify("关于收到结案通知的公告") == RESOLUTION


def test_a_penalty_from_another_regulator_does_not_close_a_csrc_case():
    """财政部 fining you says nothing about whether the CSRC is still digging."""
    assert regulatory._classify("关于收到财政部行政处罚决定书的公告") is None
    assert regulatory._classify("关于收到税务局行政处罚决定书的公告") is None


# --- scanning --------------------------------------------------------------


class _FakeFeed:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def stock_notice_report(self, symbol: str, date: str) -> pd.DataFrame:
        return self.frame


def _feed(monkeypatch, frame: pd.DataFrame) -> None:
    monkeypatch.setattr(regulatory, "_akshare", lambda: _FakeFeed(frame))


def test_scan_keeps_only_relevant_filings(monkeypatch):
    frame = pd.DataFrame(
        {
            "代码": ["300477", "600114", "688613", "600476", "600519", "510300"],
            "名称": ["ST合纵", "东睦股份", "奥精医疗", "*ST湘邮", "贵州茅台", "300ETF"],
            "公告标题": [
                "关于收到中国证监会立案告知书的公告",
                "关于房屋买卖合同纠纷案立案受理的公告",
                "关于收到中国证监会立案告知书暨风险提示的公告",
                "关于收到财政部行政处罚决定书的公告",
                "2026年第三季度报告",
                "基金份额净值公告",
            ],
            "公告日期": ["2026-11-02"] * 6,
            "网址": [""] * 6,
        }
    )
    _feed(monkeypatch, frame)

    outcome = scan(["20261102"])

    assert {event.symbol for event in outcome.events} == {"300477.SZ", "688613.SH"}
    assert all(event.kind == INVESTIGATION for event in outcome.events)
    assert outcome.scanned == ["20261102"]
    assert outcome.failed == []


def test_scan_skips_dates_already_covered(monkeypatch):
    _feed(monkeypatch, pd.DataFrame({"代码": [], "名称": [], "公告标题": [], "公告日期": []}))

    # nothing to fetch, nothing to complain about
    assert scan(["20261102"], skip={"20261102"}).events == []


def test_a_bad_day_is_retried_rather_than_lost(monkeypatch):
    """One broken response must not abort forty days, nor be marked as done."""

    class _Flaky:
        def stock_notice_report(self, symbol: str, date: str) -> pd.DataFrame:
            if date == "20261102":
                raise RuntimeError("接口挂了")
            return pd.DataFrame(
                {
                    "代码": ["300477"],
                    "名称": ["ST合纵"],
                    "公告标题": ["关于收到中国证监会立案告知书的公告"],
                    "公告日期": [date],
                }
            )

    monkeypatch.setattr(regulatory, "_akshare", lambda: _Flaky())

    outcome = scan(["20261102", "20261103"])

    assert outcome.failed == ["20261102"]  # retried next run
    assert outcome.scanned == ["20261103"]  # kept, and does not cost a refetch
    assert len(outcome.events) == 1


def test_an_unexpected_shape_counts_as_a_failure(monkeypatch):
    _feed(monkeypatch, pd.DataFrame({"日期": ["2026-11-02"], "标题": ["立案"]}))

    outcome = scan(["20261102"])

    assert outcome.failed == ["20261102"]
    assert outcome.events == []


# --- the state -------------------------------------------------------------


def test_an_unclosed_case_is_active():
    found = active(_frame([_open()]), as_of="2026-12-01")

    assert "300477.SZ" in found
    assert found["300477.SZ"].date == "2026-11-02"


def test_a_closed_case_is_not_active():
    found = active(_frame([_open(), _close()]), as_of="2026-12-01")

    assert found == {}


def test_a_closure_dated_before_the_opening_does_not_count():
    """Only a closure *after* the opening ends that investigation."""
    rows = [_open(day="2026-11-02"), _close(day="2026-01-05")]
    assert "300477.SZ" in active(_frame(rows), as_of="2026-12-01")


def test_an_old_case_drops_out_after_a_year():
    rows = [_open(day="2024-01-10")]

    assert "300477.SZ" not in active(_frame(rows), as_of="2026-11-03", within_days=365)
    assert "300477.SZ" in active(_frame(rows), as_of="2024-06-01", within_days=365)


def test_a_filing_after_the_replay_date_is_ignored():
    """Same rule as the reports: an August notice cannot explain a June screen."""
    rows = [_open(day="2026-08-15")]

    assert active(_frame(rows), as_of="2026-06-01") == {}
    assert "300477.SZ" in active(_frame(rows), as_of="2026-09-01")


def test_an_empty_store_yields_nothing():
    assert active(_frame([]), as_of="2026-11-03") == {}
    assert active(pd.DataFrame(), as_of="2026-11-03") == {}


def test_merge_does_not_duplicate_the_same_filing():
    one = _frame([_open()])
    two = _frame([_open()])

    assert len(merge(one, two)) == 1


# --- the manual list and the scan state -----------------------------------


def test_the_manual_list_reads_bare_codes(tmp_path):
    path = tmp_path / "regulatory.yaml"
    path.write_text(
        "investigations:\n"
        "  - symbol: 600519\n"
        "    date: 2025-06-01\n"
        "    note: 涉嫌信息披露违法违规\n",
        encoding="utf-8",
    )

    frame = load_manual(path)

    assert frame.iloc[0]["symbol"] == "600519.SH"
    assert frame.iloc[0]["kind"] == INVESTIGATION


def test_a_missing_manual_file_is_empty_not_an_error(tmp_path):
    assert load_manual(tmp_path / "nope.yaml").empty
    assert load_manual(None).empty


def test_scan_state_accumulates_and_skips(tmp_path):
    save_state(["20261103"], tmp_path)
    save_state(["20261102"], tmp_path)

    assert load_state(tmp_path) == {"20261103", "20261102"}
    assert load_state(tmp_path / "missing") == set()


def test_recent_sessions_returns_weekdays_only():
    days = recent_sessions(7, today=date(2026, 11, 3))

    assert "20261101" not in days  # Sunday
    assert "20261031" not in days  # Saturday
    assert days[0] == "20261103"


# --- the screen ------------------------------------------------------------


def _price_panel(
    days: int = 120, symbols: tuple[str, ...] = ("A.SH", "B.SH", "C.SH")
) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    rng = np.random.default_rng(7)
    prices = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rng.normal(0.0015, 0.01, (days, len(symbols))), axis=0),
        index=index,
        columns=list(symbols),
    )
    return PricePanel(prices=prices, extra={})


def test_an_investigated_name_never_reaches_the_picks():
    event = RegulatoryEvent("A.SH", "A", INVESTIGATION, "2026-11-02", "立案", "")
    watch = {"A.SH": event}

    plain = screen(_price_panel(), top_n=3)
    filtered = screen(_price_panel(), top_n=3, investigated=watch)

    assert "A.SH" in plain.symbols
    assert "A.SH" not in filtered.symbols
    assert filtered.dropped.get("under_investigation") == 1


def test_the_veto_can_be_lifted():
    event = RegulatoryEvent("A.SH", "A", INVESTIGATION, "2026-11-02", "立案", "")
    kept = screen(_price_panel(), top_n=3, investigated={"A.SH": event}, exclude_investigated=False)

    assert "A.SH" in kept.symbols
    assert "under_investigation" not in kept.dropped


def test_an_empty_watchlist_changes_nothing():
    plain = screen(_price_panel(), top_n=3)
    with_empty = screen(_price_panel(), top_n=3, investigated={})

    assert plain.symbols == with_empty.symbols


@pytest.mark.parametrize("kind", [INVESTIGATION, RESOLUTION])
def test_event_kind_flag_matches_the_string(kind):
    event = RegulatoryEvent("A.SH", "A", kind, "2026-11-02", "t", "")
    assert event.is_open is (kind == INVESTIGATION)
