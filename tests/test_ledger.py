"""Ledger and review tests.

The review numbers are checked against hand arithmetic: if a ledger says a list
of names returned something, that something has to be reconstructible with a
pencil.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.scoring.ledger import ScreenLedger, ScreenRecord, review


def _panel() -> PricePanel:
    """Six sessions, four names, open equal to close so the sums are checkable."""
    index = pd.bdate_range("2024-01-01", periods=6, name="date")
    prices = pd.DataFrame(
        {
            "A.SH": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "B.SH": [20.0, 20.0, 20.0, 20.0, 20.0, 20.0],
            "C.SH": [50.0, 40.0, 30.0, 20.0, 10.0, 5.0],
            "D.SH": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
        },
        index=index,
    )
    return PricePanel(prices=prices, extra={"open": prices.copy()})


def _ledger(tmp_path: Path, records: list[ScreenRecord]) -> ScreenLedger:
    ledger = ScreenLedger(tmp_path / "screening" / "index.jsonl")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        "\n".join(json.dumps(record.to_dict()) for record in records) + "\n", encoding="utf-8"
    )
    return ledger


def _record(as_of: str, symbols: list[str]) -> ScreenRecord:
    return ScreenRecord(
        as_of=as_of, created_at="2024-01-01T18:30:00", picks={s: 1.0 for s in symbols}
    )


def test_a_record_survives_the_round_trip(tmp_path):
    ledger = ScreenLedger(tmp_path / "index.jsonl")
    ledger.record(
        _result_like(),
        provider="baostock",
        adjust="qfq",
        params={"top_n": 3},
        note="first run",
    )

    loaded = ledger.recent()
    assert len(loaded) == 1
    assert loaded[0].as_of == "2024-01-05"
    assert loaded[0].provider == "baostock"
    assert loaded[0].params == {"top_n": 3}
    assert loaded[0].symbols == ["A.SH", "B.SH"]
    assert ledger.get("2024-01-05") is not None


def test_the_same_day_has_one_record_not_two(tmp_path):
    """One opinion per day: re-running the evening replaces rather than appends."""
    ledger = ScreenLedger(tmp_path / "index.jsonl")
    ledger.record(_result_like(), provider="baostock", adjust="qfq")
    ledger.record(_result_like(), provider="baostock", adjust="qfq", note="second try")

    records = ledger.recent()
    assert len(records) == 1
    assert records[0].note == "second try"


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "index.jsonl"
    path.write_text(
        json.dumps(_record("2024-01-02", ["A.SH"]).to_dict()) + "\n{garbage\n", encoding="utf-8"
    )
    assert len(ScreenLedger(path).recent()) == 1


def test_review_measures_buying_the_next_open(tmp_path):
    """Bought at d1's open, sold at d3's close — by hand: 13/11-1, 0, 20/30-1."""
    ledger = _ledger(tmp_path, [_record("2024-01-01", ["A.SH", "B.SH", "C.SH"])])
    frame = review(ledger, _panel(), horizon=2)

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["entry"] == "2024-01-02"  # the next session, not the same one
    assert row["exit"] == "2024-01-04"
    # C is bought at 40 (d1's open), not at 50 (d0's close) — the entry day matters
    gain_a = 13 / 11 - 1
    loss_c = 20 / 40 - 1
    assert row["mean_return"] == pytest.approx((gain_a + 0 + loss_c) / 3)
    assert row["win_rate"] == pytest.approx(1 / 3)
    # the whole market, equal weighted, over the same window
    assert row["benchmark"] == pytest.approx((gain_a + 0 + loss_c + 0) / 4)
    assert row["excess"] == pytest.approx(row["mean_return"] - row["benchmark"])
    assert row["beat_market"] == pytest.approx(2 / 3)


def test_review_leaves_out_windows_that_have_not_closed(tmp_path):
    """A half-finished holding period would quietly bias the average."""
    ledger = _ledger(tmp_path, [_record("2024-01-01", ["A.SH"]), _record("2024-01-05", ["A.SH"])])
    frame = review(ledger, _panel(), horizon=3)

    assert frame["as_of"].tolist() == ["2024-01-01"]


def test_review_on_an_empty_ledger_is_an_empty_table(tmp_path):
    frame = review(ScreenLedger(tmp_path / "index.jsonl"), _panel())

    assert frame.empty
    assert "mean_return" in frame.columns  # columns still exist, for a caller to print


def test_review_rejects_a_horizon_of_zero(tmp_path):
    with pytest.raises(ValueError):
        review(ScreenLedger(tmp_path / "index.jsonl"), _panel(), horizon=0)


def _result_like():  # type: ignore[no-untyped-def]
    """A stand-in ScreenResult: the ledger only reads as_of, scores and counts."""
    from quant_research.scoring.screen import ScreenResult

    picks = pd.DataFrame({"score": [2.0, 1.0]}, index=pd.Index(["A.SH", "B.SH"], name="symbol"))
    return ScreenResult(
        as_of=pd.Timestamp("2024-01-05"),
        picks=picks,
        universe=4,
        dropped={"below_cross_sectional_mean": 2},
    )
