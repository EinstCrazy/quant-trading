"""Nightly run tests.

The behaviour that matters is the refusal: a day that has not landed must not be
screened. Everything else — waiting, collecting, writing the ledger — is plumbing
around that one decision.
"""

from __future__ import annotations

import json
from datetime import date, time, timedelta

import numpy as np
import pandas as pd
import pytest

from quant_research.nightly import run_nightly, write_report
from quant_research.scoring.ledger import ScreenLedger

_SYMBOLS = (
    "600000.SH",
    "600519.SH",
    "000001.SZ",
    "000651.SZ",
)  # real-shaped codes: the collector validates them
_MONDAY = date(2026, 11, 2)


class _FakeProvider:
    """Vends a fixed window ending on ``last_day``.

    That one field is the whole test: whether the day we are asking about is in
    what the source hands back.
    """

    def __init__(self, last_day: date, symbols: tuple[str, ...] = _SYMBOLS) -> None:
        self.name = "fake"
        self.adjust = "none"
        self.last_day = last_day
        self.symbols = list(symbols)
        self.calls = 0

    def fetch_daily(self, symbols, start=None, end=None) -> dict[str, pd.DataFrame]:
        self.calls += 1
        index = pd.bdate_range(end=pd.Timestamp(self.last_day), periods=150, name="date")
        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            # Seeded on the code, not the position: the collector asks for one
            # symbol per call, so a positional seed hands every name the same
            # series — an identical market has no dispersion and no scores.
            rng = np.random.default_rng(sum(ord(character) for character in symbol))
            close = 100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.01, len(index)))
            frames[symbol] = pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.003,
                    "low": close * 0.997,
                    "close": close,
                    "volume": close * 100.0,
                    "amount": close * 1e6,
                },
                index=index,
            )
        return frames


def _run(tmp_path, provider, day=_MONDAY, symbols=None, **kwargs) -> tuple:  # type: ignore[no-untyped-def]
    ledger = ScreenLedger(tmp_path / "ledger.jsonl")
    slept: list[float] = []
    report = run_nightly(
        provider_name="fake",
        provider=provider,
        data_root=tmp_path,
        symbols=symbols if symbols is not None else list(provider.symbols),
        today=day,
        sleep=slept.append,
        ledger=ledger,
        min_history=60,
        pause=0.0,  # so `slept` records the wait and nothing else
        **kwargs,
    )
    return report, ledger, slept


def test_a_day_that_has_not_landed_is_not_screened(tmp_path):
    """The source only has up to Friday; Monday's screen would be fiction."""
    provider = _FakeProvider(last_day=_MONDAY - timedelta(days=1))
    report, ledger, _ = _run(tmp_path, provider)

    assert report.screened is False
    assert report.picks == 0
    assert "has not landed" in report.note
    assert ledger.get(str(_MONDAY)) is None  # and nothing was written down


def test_a_day_that_has_landed_is_screened_and_recorded(tmp_path):
    provider = _FakeProvider(last_day=_MONDAY)
    report, ledger, _ = _run(tmp_path, provider)

    assert report.screened is True
    assert report.picks > 0
    record = ledger.get(str(_MONDAY))
    assert record is not None
    assert len(record.symbols) == report.picks


def test_a_non_trading_day_does_nothing_at_all(tmp_path):
    provider = _FakeProvider(last_day=_MONDAY)
    sunday = _MONDAY - timedelta(days=1)
    report, _, _ = _run(tmp_path, provider, day=sunday)

    assert report.screened is False
    assert "not a trading day" in report.note
    assert provider.calls == 0  # no requests were made for a day that does not trade


def test_the_run_waits_when_asked_to(tmp_path):
    provider = _FakeProvider(last_day=_MONDAY)
    report, _, slept = _run(tmp_path, provider, wait_until=time(23, 59))

    assert slept, "waiting until 23:59 should have slept"
    assert report.as_of == str(_MONDAY)


def test_the_run_does_not_wait_when_the_time_has_passed(tmp_path):
    provider = _FakeProvider(last_day=_MONDAY)
    _, _, slept = _run(tmp_path, provider, wait_until=time(0, 1))

    assert slept == []


def test_without_symbols_and_an_empty_store_it_says_so(tmp_path):
    # called directly: the helper would otherwise fill in the provider's symbols
    report = run_nightly(
        provider_name="fake",
        provider=_FakeProvider(last_day=_MONDAY),
        data_root=tmp_path,
        symbols=None,
        today=_MONDAY,
        pause=0.0,
        sleep=lambda seconds: None,
        ledger=ScreenLedger(tmp_path / "ledger.jsonl"),
    )

    assert report.screened is False
    assert "no symbols" in report.note


def test_the_report_archives_what_the_night_did(tmp_path):
    provider = _FakeProvider(last_day=_MONDAY)
    report, _, _ = _run(tmp_path, provider)

    path = write_report(report, tmp_path / "nightly")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["as_of"] == str(_MONDAY)
    assert payload["screened"] is True
    assert path.parent.is_dir()


def test_a_screening_failure_still_leaves_a_report(tmp_path):
    """A night that refused to screen is still worth knowing about."""
    provider = _FakeProvider(last_day=_MONDAY - timedelta(days=1))
    report, _, _ = _run(tmp_path, provider)

    assert report.screened is False
    assert pytest.approx(0) == report.picks
