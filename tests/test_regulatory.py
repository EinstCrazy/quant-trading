"""Regulatory event tests.

The decisions worth pinning down:

- 立案 is not 立案受理 (a court accepting a suit), not 尽职调查, and not
  「被公安机关立案侦查」(a criminal case);
- a penalty from the finance ministry does not close a CSRC case;
- a case against a 5% holder is not a case against the company — but one
  against the 控股股东 is, because it gates the company's own deals;
- an open case stops counting after a year, a closed one immediately;
- a screen excludes investigated issuers by default, and says so.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.fundamentals import regulatory
from quant_research.fundamentals.regulatory import (
    COMPANY,
    INVESTIGATION,
    PARTY,
    RESOLUTION,
    RegulatoryEvent,
    active,
    load,
    load_manual,
    merge,
    search_events,
    store,
)
from quant_research.scoring import screen

_STAMP = int(datetime(2026, 11, 2, tzinfo=regulatory._CST).timestamp() * 1000)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(regulatory._COLUMNS))


def _open(symbol: str = "300477.SZ", day: str = "2026-11-02", **kw) -> dict:
    return {
        "symbol": symbol,
        "name": "ST合纵",
        "kind": INVESTIGATION,
        "date": day,
        "title": "关于收到中国证监会立案告知书的公告",
        "url": "",
        "severity": COMPANY,
        **kw,
    }


def _close(symbol: str = "300477.SZ", day: str = "2026-11-20") -> dict:
    return {
        "symbol": symbol,
        "name": "ST合纵",
        "kind": RESOLUTION,
        "date": day,
        "title": "关于收到行政处罚决定书的公告",
        "url": "",
        "severity": COMPANY,
    }


# --- telling a filing from a lawsuit --------------------------------------


def test_a_csrc_notice_is_an_investigation():
    assert regulatory._classify("关于收到中国证监会立案告知书的公告") == INVESTIGATION


def test_a_court_accepting_a_suit_is_not_an_investigation():
    """立案受理 is a civil case; flagging it would catch half the market."""
    assert regulatory._classify("关于买卖合同纠纷案立案受理的公告") is None
    assert regulatory._classify("关于合同纠纷立案的公告") is None
    # the word order varies in the wild, and the first pattern missed it
    assert regulatory._classify("关于提起上诉案件受理立案暨诉讼的进展公告") is None


def test_a_criminal_case_is_not_a_securities_case():
    """公安机关立案侦查 is a public-security matter, not the CSRC's."""
    assert regulatory._classify("关于张宝被公安机关立案侦查的进展公告") is None
    assert regulatory._classify("关于副总经理被立案审查调查并留置的公告") is None


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


# --- who the case is about -------------------------------------------------


def test_the_issuer_being_investigated_is_severe():
    assert regulatory._severity("关于收到中国证监会立案告知书的公告") == COMPANY


def test_a_minority_holder_being_investigated_is_not_the_issuer():
    """Excluding the stock for a 5% holder's case would drop healthy names."""
    assert regulatory._severity("关于公司股东收到中国证监会立案告知书的公告") == PARTY


def test_an_executive_being_investigated_is_not_the_issuer():
    assert regulatory._severity("关于公司董事、高级管理人员收到立案告知书的公告") == PARTY
    assert regulatory._severity("关于董事长收到中国证监会立案告知书的公告") == PARTY


def test_a_controlling_holder_is_severe_because_it_gates_the_company():
    """Their compliance blocks the issuer's own refinancing and asset purchases."""
    assert regulatory._severity("关于公司实际控制人收到立案告知书的公告") == COMPANY
    assert regulatory._severity("关于控股股东、实际控制人收到立案通知书的公告") == COMPANY


def test_company_and_holder_together_is_severe():
    assert regulatory._severity("关于公司及控股股东收到立案告知书的公告") == COMPANY
    assert regulatory._severity("关于公司及相关股东收到立案告知书的公告") == COMPANY


def test_a_subsidiary_is_not_the_issuer():
    """「控股子公司关于…」 must not be read as 「公司关于…」."""
    title = "关于控股子公司青岛北洋天青收到中国证券监督管理委员会《立案告知书》的公告"
    assert regulatory._severity(title) == PARTY


# --- searching cninfo ------------------------------------------------------


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Feed:
    """Stands in for requests, serving a fixed set of pages per keyword."""

    def __init__(self, pages: dict[tuple[str, int], dict]) -> None:
        self.pages = pages
        self.queries: list[dict] = []

    def get(self, url: str, params: dict, headers: dict, timeout: int) -> _Response:
        self.queries.append(params)
        key = (params["searchkey"], int(params["pageNum"]))
        return _Response(self.pages.get(key, {"announcements": [], "totalAnnouncement": 0}))


def _item(code: str, name: str, title: str, stamp: int = _STAMP) -> dict:
    return {
        "secCode": code,
        "secName": name,
        "announcementTitle": title,
        "announcementTime": stamp,
        "adjunctUrl": f"finalpage/2026-11-02/{code}.PDF",
    }


def _wire(monkeypatch, pages: dict[tuple[str, int], dict]) -> _Feed:
    feed = _Feed(pages)
    monkeypatch.setattr(regulatory, "_requests", lambda: feed)
    return feed


def test_search_keeps_only_relevant_filings(monkeypatch):
    _wire(
        monkeypatch,
        {
            ("立案", 1): {
                "announcements": [
                    _item("300477", "ST合纵", "关于收到中国证监会<em>立案</em>告知书的公告"),
                    _item("600114", "东睦股份", "关于买卖合同纠纷案<em>立案</em>受理的公告"),
                    _item("600519", "贵州茅台", "2026年第三季度报告"),
                ],
                "totalAnnouncement": 3,
            }
        },
    )

    events = search_events(days=365, today=date(2026, 11, 3))

    assert [event.symbol for event in events] == ["300477.SZ"]
    assert events[0].kind == INVESTIGATION
    assert events[0].date == "2026-11-02"


def test_search_strips_the_highlight_tag(monkeypatch):
    """cninfo wraps the match in <em>; that tag is not part of the title."""
    _wire(
        monkeypatch,
        {
            ("立案", 1): {
                "announcements": [_item("300477", "ST合纵", "关于<em>立案</em>告知书的公告")],
                "totalAnnouncement": 1,
            }
        },
    )

    events = search_events(days=365, today=date(2026, 11, 3))

    assert events[0].title == "关于立案告知书的公告"


def test_search_pages_until_the_total_is_reached(monkeypatch):
    feed = _wire(
        monkeypatch,
        {
            ("立案", 1): {
                "announcements": [_item("300477", "A", "立案告知书")],
                "totalAnnouncement": 2,
            },
            ("立案", 2): {
                "announcements": [_item("300478", "B", "立案告知书")],
                "totalAnnouncement": 2,
            },
        },
    )

    events = search_events(days=365, today=date(2026, 11, 3))

    assert len(events) == 2
    assert [q["pageNum"] for q in feed.queries if q["searchkey"] == "立案"] == ["1", "2"]


def test_one_filing_found_by_two_keywords_is_kept_once(monkeypatch):
    item = _item("300477", "A", "关于收到行政处罚决定书暨立案调查结案的公告")
    _wire(
        monkeypatch,
        {
            ("行政处罚决定书", 1): {"announcements": [item], "totalAnnouncement": 1},
            ("立案", 1): {"announcements": [item], "totalAnnouncement": 1},
        },
    )

    events = search_events(days=365, today=date(2026, 11, 3))

    assert len(events) == 1


def test_a_truncated_search_raises_instead_of_looking_complete(monkeypatch):
    """Silently stopping at the page cap would read as "these are all the cases"."""
    _wire(
        monkeypatch,
        {
            ("立案", 1): {
                "announcements": [_item("300477", "A", "立案告知书")],
                "totalAnnouncement": 500,
            }
        },
    )

    with pytest.raises(regulatory.ProviderError, match="未翻完"):
        search_events(days=365, today=date(2026, 11, 3), max_pages=1)


# --- the state -------------------------------------------------------------


def test_an_unclosed_case_is_active():
    found = active(_frame([_open()]), as_of="2026-12-01")

    assert "300477.SZ" in found
    assert found["300477.SZ"].date == "2026-11-02"


def test_a_closed_case_is_not_active():
    assert active(_frame([_open(), _close()]), as_of="2026-12-01") == {}


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


def test_severity_survives_the_round_trip():
    found = active(_frame([_open(severity=PARTY)]), as_of="2026-12-01")

    assert found["300477.SZ"].is_issuer is False


def test_an_empty_store_yields_nothing():
    assert active(_frame([]), as_of="2026-11-03") == {}
    assert active(pd.DataFrame(), as_of="2026-11-03") == {}


def test_merge_does_not_duplicate_the_same_filing():
    assert len(merge(_frame([_open()]), _frame([_open()]))) == 1


# --- storage and the manual list -------------------------------------------


def test_store_round_trips_severity(tmp_path):
    store(
        [RegulatoryEvent("300477.SZ", "A", INVESTIGATION, "2026-11-02", "立案", severity=PARTY)],
        tmp_path,
    )

    assert load(tmp_path).iloc[0]["severity"] == PARTY


def test_a_store_written_before_severity_existed_still_loads(tmp_path, monkeypatch):
    """Old rows were all kept under the issuer rule, which is what it then was."""
    tmp_path.mkdir(exist_ok=True)
    old = pd.DataFrame(
        [
            {
                "symbol": "300477.SZ",
                "name": "A",
                "kind": INVESTIGATION,
                "date": "2026-11-02",
                "title": "立案",
                "url": "",
            }
        ]
    )
    old.to_parquet(tmp_path / "events.parquet")

    assert load(tmp_path).iloc[0]["severity"] == COMPANY


def test_the_manual_list_reads_bare_codes(tmp_path):
    path = tmp_path / "regulatory.yaml"
    path.write_text(
        "investigations:\n"
        "  - symbol: 600519\n"
        "    date: 2025-06-01\n"
        "    note: 涉嫌信息披露违法违规\n"
        "  - symbol: 600520.SH\n"
        "    date: 2025-06-02\n"
        "    issuer: false\n",
        encoding="utf-8",
    )

    frame = load_manual(path)

    assert frame.iloc[0]["symbol"] == "600519.SH"
    assert frame.iloc[0]["severity"] == COMPANY
    assert frame.iloc[1]["severity"] == PARTY


def test_a_missing_manual_file_is_empty_not_an_error(tmp_path):
    assert load_manual(tmp_path / "nope.yaml").empty
    assert load_manual(None).empty


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
    event = RegulatoryEvent("A.SH", "A", INVESTIGATION, "2026-11-02", "立案")
    watch = {"A.SH": event}

    plain = screen(_price_panel(), top_n=3)
    filtered = screen(_price_panel(), top_n=3, investigated=watch)

    assert "A.SH" in plain.symbols
    assert "A.SH" not in filtered.symbols
    assert filtered.dropped.get("under_investigation") == 1


def test_the_veto_can_be_lifted():
    event = RegulatoryEvent("A.SH", "A", INVESTIGATION, "2026-11-02", "立案")
    kept = screen(_price_panel(), top_n=3, investigated={"A.SH": event}, exclude_investigated=False)

    assert "A.SH" in kept.symbols
    assert "under_investigation" not in kept.dropped


def test_an_empty_watchlist_changes_nothing():
    plain = screen(_price_panel(), top_n=3)
    with_empty = screen(_price_panel(), top_n=3, investigated={})

    assert plain.symbols == with_empty.symbols


@pytest.mark.parametrize("kind", [INVESTIGATION, RESOLUTION])
def test_event_kind_flag_matches_the_string(kind):
    event = RegulatoryEvent("A.SH", "A", kind, "2026-11-02", "t")
    assert event.is_open is (kind == INVESTIGATION)


@pytest.mark.parametrize(("severity", "issuer"), [(COMPANY, True), (PARTY, False)])
def test_issuer_flag_follows_severity(severity: str, issuer: bool):
    event = RegulatoryEvent("A.SH", "A", INVESTIGATION, "2026-11-02", "t", severity=severity)
    assert event.is_issuer is issuer
