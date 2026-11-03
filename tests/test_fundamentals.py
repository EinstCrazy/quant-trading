"""Financial risk tests.

The decisions worth pinning down here are not arithmetic:

- a name with no filings is "unknown", never "low";
- the leverage figure survives BaoStock's occasional hundredfold slip;
- a bank is not called over-leveraged for doing what a bank does, but the
  waiver is shown rather than hidden;
- a report published after the screening date is not used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.fundamentals import assess, liability_ratio, quarters_back
from quant_research.fundamentals.fetch import ReportPanel, merge
from quant_research.fundamentals.risk import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_UNKNOWN,
    FinancialRisk,
    assess_many,
)
from quant_research.scoring import screen


def _panel(rows: list[dict]) -> ReportPanel:
    frame = pd.DataFrame(rows)
    frame["published"] = pd.to_datetime(frame["published"])
    index = pd.MultiIndex.from_frame(frame[["symbol", "period"]], names=["symbol", "period"])
    return ReportPanel(
        frame=frame.drop(columns=["symbol", "period"]).set_index(index), provider="baostock"
    )


def _healthy(**overrides) -> dict:
    row = {
        "symbol": "600519.SH",
        "period": "2025Q4",
        "published": "2026-04-17",
        "netProfit": 8.5e10,
        "npMargin": 0.50,
        "gpMargin": 0.90,
        "roeAvg": 0.34,
        "liabilityToAsset": 0.16,
        "assetToEquity": 1.20,
        "currentRatio": 5.0,
        "CFOToNP": 1.2,
        "YOYNI": 0.05,
        "ebitToInterest": 100.0,
    }
    row.update(overrides)
    return row


# --- which quarters to ask for ---------------------------------------------


def test_quarters_back_skips_the_quarter_that_has_not_ended():
    """A quarter still running cannot have a report; asking wastes four queries."""
    from datetime import date

    assert quarters_back(3, today=date(2026, 11, 3)) == [(2026, 3), (2026, 2), (2026, 1)]
    assert quarters_back(2, today=date(2026, 1, 15)) == [(2025, 4), (2025, 3)]


def test_quarters_back_needs_a_positive_count():
    with pytest.raises(ValueError, match="positive"):
        quarters_back(0)


# --- the leverage figure ----------------------------------------------------


def test_leverage_comes_from_the_equity_multiplier():
    row = pd.Series({"liabilityToAsset": 0.164154, "assetToEquity": 1.196392})

    assert liability_ratio(row) == pytest.approx(0.164154, abs=1e-6)


def test_a_hundredfold_slip_is_caught_by_the_multiplier():
    """The real case: 2025Q2 Moutai reported 0.0015 where the multiplier implies 0.1475."""
    row = pd.Series({"liabilityToAsset": 0.001475, "assetToEquity": 1.173087})

    assert liability_ratio(row) == pytest.approx(1 - 1 / 1.173087)


def test_leverage_is_unknown_when_neither_field_works():
    assert liability_ratio(pd.Series({"liabilityToAsset": np.nan, "assetToEquity": np.nan})) is None


# --- the flags --------------------------------------------------------------


def test_a_missing_report_is_unknown_not_low():
    """Absence of evidence is a reason to look closer, not a reason to relax."""
    risk = assess("600519.SH", None)

    assert risk.level == LEVEL_UNKNOWN
    assert risk.snapshot is None


def test_a_clean_balance_sheet_raises_nothing():
    risk = assess("600519.SH", _panel([_healthy()]).latest("600519.SH"))

    assert risk.level == LEVEL_LOW
    assert risk.flags == ()
    assert risk.reason == "无红旗"


def test_a_loss_is_the_heaviest_single_flag():
    risk = assess("A.SH", _panel([_healthy(symbol="A.SH", netProfit=-1.0e8)]).latest("A.SH"))

    assert [flag.code for flag in risk.flags] == ["loss"]
    assert risk.score == 30
    assert risk.level == LEVEL_MEDIUM


def test_leverage_liquidity_and_cash_conversion_stack_up():
    row = _healthy(
        symbol="A.SH",
        liabilityToAsset=0.82,
        assetToEquity=5.5,
        currentRatio=0.7,
        CFOToNP=0.05,
    )
    risk = assess("A.SH", _panel([row]).latest("A.SH"))

    codes = {flag.code for flag in risk.flags}
    assert {"over_leveraged", "thin_liquidity", "profit_without_cash"} <= codes
    assert risk.level == LEVEL_HIGH


def test_a_bank_is_not_called_over_leveraged_but_the_flag_is_shown():
    """Deposits are a bank's raw material. The waiver is reported, not hidden."""
    row = _healthy(
        symbol="601166.SH",
        gpMargin=np.nan,  # no cost of goods: the tell for a financial
        liabilityToAsset=0.913,
        assetToEquity=11.5,
        currentRatio=np.nan,
        CFOToNP=0.9,
        YOYNI=0.02,
    )
    risk = assess("601166.SH", _panel([row]).latest("601166.SH"))

    assert risk.likely_financial is True
    assert "over_leveraged" not in {flag.code for flag in risk.flags}
    assert "over_leveraged" in {flag.code for flag in risk.waived}
    assert "金融口径" in risk.reason
    assert risk.level == LEVEL_LOW


def test_a_missing_gross_margin_alone_does_not_excuse_leverage():
    """A distressed manufacturer with an unreported margin is not a bank."""
    row = _healthy(symbol="A.SH", gpMargin=np.nan, liabilityToAsset=0.62, assetToEquity=2.6)
    risk = assess("A.SH", _panel([row]).latest("A.SH"))

    assert risk.likely_financial is False
    assert risk.level == LEVEL_LOW


# --- reading a report without seeing the future ----------------------------


def test_only_reports_published_by_the_screening_date_are_used():
    panel = _panel(
        [
            _healthy(period="2025Q1", published="2025-04-25"),
            _healthy(period="2025Q2", published="2025-08-15"),
        ]
    )

    assert panel.latest("600519.SH").name == "2025Q2"
    assert panel.latest("600519.SH", as_of="2025-06-01").name == "2025Q1"
    assert panel.latest("600519.SH", as_of="2025-01-01") is None


def test_an_empty_panel_answers_none_instead_of_raising():
    """`load()` on a fresh checkout returns nothing, and that must be readable."""
    empty = ReportPanel(frame=pd.DataFrame(), provider="baostock")

    assert empty.latest("600519.SH") is None
    assert assess_many(empty) == []


def test_merging_keeps_the_quarters_already_held():
    old = _panel([_healthy(period="2025Q1", published="2025-04-25")])
    new = _panel([_healthy(period="2025Q2", published="2025-08-15")])

    combined = merge(old, new)

    assert list(combined.frame.index.get_level_values("period")) == ["2025Q1", "2025Q2"]


# --- the filter in the screen ----------------------------------------------


def _price_panel(
    days: int = 120, symbols: tuple[str, ...] = ("A.SH", "B.SH", "C.SH")
) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    rng = np.random.default_rng(3)
    prices = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rng.normal(0.001, 0.01, (days, len(symbols))), axis=0),
        index=index,
        columns=list(symbols),
    )
    return PricePanel(prices=prices, extra={})


def _risk(symbol: str, level: str, score: float = 60.0) -> FinancialRisk:
    return FinancialRisk(
        symbol=symbol,
        snapshot=None,
        flags=(),
        waived=(),
        score=score,
        level=level,
        likely_financial=False,
    )


def test_a_high_risk_name_never_reaches_the_picks():
    panel = _price_panel()

    plain = screen(panel, top_n=3)
    filtered = screen(panel, top_n=3, financial_risks=[_risk("A.SH", LEVEL_HIGH)])

    assert "A.SH" in plain.symbols  # it would have been picked
    assert "A.SH" not in filtered.symbols
    assert filtered.dropped.get("financial_risk") == 1


def test_a_name_with_no_report_is_left_alone():
    """Unknown is not high: the filter drops what it knows, not what it cannot see."""
    filtered = screen(_price_panel(), top_n=3, financial_risks=[_risk("A.SH", LEVEL_UNKNOWN)])

    assert "A.SH" in filtered.symbols
    assert "financial_risk" not in filtered.dropped


def test_the_threshold_can_be_lowered_to_medium():
    risks = [_risk("A.SH", LEVEL_MEDIUM)]

    strict = screen(
        _price_panel(), top_n=3, financial_risks=risks, exclude_risk_levels=(LEVEL_HIGH,)
    )
    loose = screen(
        _price_panel(),
        top_n=3,
        financial_risks=risks,
        exclude_risk_levels=(LEVEL_HIGH, LEVEL_MEDIUM),
    )

    assert "A.SH" in strict.symbols
    assert "A.SH" not in loose.symbols
