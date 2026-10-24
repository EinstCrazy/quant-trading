"""Tests for the daily collection pipeline.

The pipeline is exercised end to end with the offline provider, which keeps these
tests free of network access — and which also proves the collector writes files the
research loader can actually read.
"""

from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

from quant_research.cli import main
from quant_research.collector import collect_once, is_due, now_in, resolve_symbols, write_report
from quant_research.data import load_price_panel
from quant_research.data.calendar import TradingCalendar
from quant_research.data.providers import (
    MockProvider,
    ProviderError,
    get_provider,
    normalize_symbol,
    to_bare_symbol,
)
from quant_research.data.providers.base import MarketDataProvider, normalise_bars
from quant_research.data.store import LocalMarketStore

_END = date(2024, 1, 31)  # a Wednesday, so the weekday calendar accepts it


class FailingProvider(MarketDataProvider):
    """Provider that never succeeds, to prove one bad source cannot sink the run."""

    name = "failing"

    def list_symbols(self) -> list[str]:
        return ["600000.SH"]

    def fetch_daily(self, symbols, start=None, end=None) -> dict[str, pd.DataFrame]:
        raise ProviderError("upstream is down")


def _store(tmp_path: Path, *, fmt: str = "parquet") -> LocalMarketStore:
    return LocalMarketStore(tmp_path / "raw", "mock", fmt=fmt)


# --- symbols and frames ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("600519.SH", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("SH.600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("300750.SZ", "300750.SZ"),
        ("688981", "688981.SH"),
        ("830799", "830799.BJ"),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_normalize_symbol_rejects_garbage():
    with pytest.raises(ValueError, match="six-digit"):
        normalize_symbol("ABCDE")


def test_to_bare_symbol():
    assert to_bare_symbol("600519.SH") == "600519"


def test_normalise_bars_rejects_missing_or_bad_prices():
    good = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [10.0]},
        index=pd.to_datetime(["2024-01-02"]),
    )
    assert len(normalise_bars(good, source="test")) == 1

    with pytest.raises(ProviderError, match="required column"):
        normalise_bars(good.drop(columns=["close"]), source="test")
    with pytest.raises(ProviderError, match="non-positive"):
        broken = good.copy()
        broken["close"] = 0.0
        normalise_bars(broken, source="test")


def test_mock_provider_is_deterministic():
    first = MockProvider(("600000.SH",), seed=3).fetch_daily(["600000.SH"], date(2024, 1, 2), _END)
    second = MockProvider(("600000.SH",), seed=3).fetch_daily(["600000.SH"], date(2024, 1, 2), _END)
    pd.testing.assert_frame_equal(first["600000.SH"], second["600000.SH"])


def test_mock_provider_ignores_unknown_symbols():
    provider = MockProvider(("600000.SH",))
    assert provider.fetch_daily(["000001.SZ"], date(2024, 1, 2), _END) == {}


def test_get_provider_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider("nonexistent")


def test_resolve_symbols_dedupes_and_normalises():
    provider = MockProvider(("600000.SH", "000001.SZ"))
    assert resolve_symbols(provider, ["600000", "600000.SH", "000001.SZ"]) == [
        "600000.SH",
        "000001.SZ",
    ]


# --- store -------------------------------------------------------------------


def test_store_upsert_merges_and_dedupes(tmp_path):
    store = _store(tmp_path)
    provider = MockProvider(("600519.SH",))

    first = provider.fetch_daily(["600519.SH"], date(2024, 1, 2), date(2024, 1, 10))["600519.SH"]
    result = store.upsert("600519.SH", first)
    assert result.rows_added == len(first)
    assert store.read("600519.SH").index.max().date() == date(2024, 1, 10)

    second = provider.fetch_daily(["600519.SH"], date(2024, 1, 8), date(2024, 1, 20))["600519.SH"]
    result = store.upsert("600519.SH", second)
    stored = store.read("600519.SH")
    # 8 Jan - 10 Jan overlaps; it must not be duplicated.
    assert stored.index.is_unique
    assert result.rows_total == len(
        provider.fetch_daily(["600519.SH"], date(2024, 1, 2), date(2024, 1, 20))["600519.SH"]
    )
    # 20 Jan is a Saturday, so the business-day provider stops on the 19th.
    assert stored.index.max().date() == date(2024, 1, 19)


def test_store_upsert_overwrites_revised_history(tmp_path):
    store = _store(tmp_path)
    original = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.to_datetime(["2024-01-02"]),
    )
    store.upsert("600519.SH", original)
    revised = original.copy()
    revised["close"] = [2.0]
    store.upsert("600519.SH", revised)

    assert float(store.read("600519.SH")["close"].iloc[0]) == 2.0


def test_store_falls_back_to_csv(tmp_path):
    store = _store(tmp_path, fmt="csv")
    bars = MockProvider(("600519.SH",)).fetch_daily(["600519.SH"], date(2024, 1, 2), _END)[
        "600519.SH"
    ]
    store.upsert("600519.SH", bars)

    assert store.path_for("600519.SH").suffix == ".csv"
    assert len(store.read("600519.SH")) == len(bars)


def test_store_state_round_trip(tmp_path):
    store = _store(tmp_path)
    state = store.load_state(adjust="qfq")
    state.symbols["600519.SH"] = {"last_date": "2024-01-31", "rows": 22}
    state.failures["000001.SZ"] = "timeout"
    store.save_state(state)

    reloaded = store.load_state()
    assert reloaded.last_date("600519.SH") == date(2024, 1, 31)
    assert reloaded.last_date("600000.SH") is None
    assert "timeout" in reloaded.failures["000001.SZ"]


def test_store_state_survives_corruption(tmp_path):
    store = _store(tmp_path)
    store.base_dir.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not json", encoding="utf-8")

    assert store.load_state().symbols == {}


def test_store_atomic_write_leaves_no_temp_files(tmp_path):
    store = _store(tmp_path)
    bars = MockProvider(("600519.SH",)).fetch_daily(["600519.SH"], date(2024, 1, 2), _END)[
        "600519.SH"
    ]
    store.upsert("600519.SH", bars)

    leftovers = [path.name for path in store.daily_dir.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []


# --- calendar ----------------------------------------------------------------


def test_calendar_weekday_fallback():
    calendar = TradingCalendar()
    assert calendar.is_exact is False
    assert calendar.is_trading_day(date(2024, 1, 31))  # Wednesday
    assert not calendar.is_trading_day(date(2024, 2, 4))  # Sunday


def test_exact_calendar_overrides_weekdays(tmp_path):
    calendar = TradingCalendar(days=frozenset({date(2024, 1, 31)}), source="test")
    assert calendar.is_trading_day(date(2024, 1, 31))
    assert not calendar.is_trading_day(date(2024, 2, 1))  # a workday that is closed

    path = calendar.save(tmp_path / "cal.csv")
    reloaded = TradingCalendar.from_file(path)
    assert reloaded.is_trading_day(date(2024, 1, 31))
    assert TradingCalendar.load_or_weekday(tmp_path / "missing.csv").is_exact is False


def test_previous_and_next_trading_day():
    calendar = TradingCalendar(days=frozenset({date(2024, 1, 29), date(2024, 2, 5)}), source="test")
    assert calendar.previous_trading_day(date(2024, 2, 1)) == date(2024, 1, 29)
    assert calendar.next_trading_day(date(2024, 2, 1)) == date(2024, 2, 5)


# --- scheduling --------------------------------------------------------------


def test_is_due():
    calendar = TradingCalendar(days=frozenset({date(2024, 1, 31)}), source="test")
    past = datetime(2024, 1, 31, 19, 0)
    early = datetime(2024, 1, 31, 9, 0)

    assert is_due(past, time(18, 30), None, calendar)
    assert not is_due(early, time(18, 30), None, calendar)
    assert not is_due(past, time(18, 30), date(2024, 1, 31), calendar)  # already ran
    assert not is_due(datetime(2024, 2, 3, 19, 0), time(18, 30), None, calendar)  # Saturday


def test_now_in_uses_the_requested_timezone():
    assert str(now_in("Asia/Shanghai").tzinfo) == "Asia/Shanghai"


# --- collect once ------------------------------------------------------------


def _collect(tmp_path, *, end: date = _END, **kwargs):
    store = _store(tmp_path)
    provider = MockProvider(("600000.SH", "000001.SZ"))
    report = collect_once(
        provider=provider,
        store=store,
        symbols=["600000.SH", "000001.SZ"],
        end=end,
        pause=0.0,
        sleep=lambda _: None,
        **kwargs,
    )
    return store, report


def test_collect_once_writes_both_symbols(tmp_path):
    store, report = _collect(tmp_path)

    assert report.ok
    assert sorted(report.updated) == ["000001.SZ", "600000.SH"]
    assert store.stored_symbols() == ["000001.SZ", "600000.SH"]
    state = store.load_state()
    assert state.last_date("600000.SH") == _END


def test_collect_once_is_incremental(tmp_path):
    store = _store(tmp_path)
    provider = MockProvider(("600000.SH",))
    symbols = ["600000.SH"]

    first = collect_once(
        provider=provider,
        store=store,
        symbols=symbols,
        end=date(2024, 1, 10),
        pause=0.0,
        sleep=lambda _: None,
    )
    first_rows = len(store.read("600000.SH"))

    second = collect_once(
        provider=provider,
        store=store,
        symbols=symbols,
        end=date(2024, 1, 19),
        pause=0.0,
        sleep=lambda _: None,
    )
    second_rows = len(store.read("600000.SH"))

    assert second_rows > first_rows
    # the lookback window re-reads the tail, so the second run adds fewer rows than
    # a full re-fetch would
    assert second.added["600000.SH"] < first.added["600000.SH"] + second_rows


def test_collect_once_skips_non_trading_days(tmp_path):
    store, report = _collect(tmp_path, calendar=TradingCalendar(days=frozenset(), source="empty"))

    assert report.updated == {}
    assert "not a trading day" in report.note
    assert store.stored_symbols() == []


def test_collect_once_isolates_failures(tmp_path):
    store = _store(tmp_path)
    report = collect_once(
        provider=FailingProvider(),
        store=store,
        symbols=["600000.SH"],
        end=_END,
        retries=1,
        pause=0.0,
        sleep=lambda _: None,
    )

    assert not report.ok
    assert "600000.SH" in report.failed
    # a failed run leaves the day open so an operator can retry it
    assert store.load_state().last_run_date is None
    assert "600000.SH" in store.load_state().failures


def test_collect_once_dry_run_writes_nothing(tmp_path):
    store, report = _collect(tmp_path, dry_run=True)

    assert report.updated
    assert store.stored_symbols() == []
    assert not store.state_path.is_file()


def test_collected_data_is_readable_by_the_loader(tmp_path):
    """The point of the whole exercise: collected files must feed the research stack."""
    store, _report = _collect(tmp_path)

    panel = load_price_panel(store.daily_dir)

    assert sorted(panel.prices.columns) == ["000001.SZ", "600000.SH"]
    assert panel.prices.notna().any().all()
    assert panel.field("open") is not None
    assert panel.prices.index.max().date() == _END
    # the store's own adjustment record describes the data better than the default
    assert panel.meta.price_adjustment == "none"


def test_write_report_archives_json(tmp_path):
    _store_unused, report = _collect(tmp_path)
    path = write_report(report, tmp_path / "reports")

    assert path.is_file()
    assert path.read_text(encoding="utf-8")  # non-empty
    assert "600000.SH" in path.read_text(encoding="utf-8")


# --- command line ------------------------------------------------------------


def test_cli_collects_once(tmp_path):
    code = main(
        [
            "--provider",
            "mock",
            "--symbols",
            "600000.SH",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-31",
            "--output-dir",
            str(tmp_path / "raw"),
            "--report-dir",
            str(tmp_path / "reports"),
            "--pause",
            "0",
        ]
    )

    assert code == 0
    assert (tmp_path / "raw" / "mock" / "daily").is_dir()


def test_cli_without_symbols_exits_with_usage(tmp_path):
    code = main(["--provider", "mock", "--output-dir", str(tmp_path / "raw")])
    assert code == 2


def test_cli_refuses_unknown_provider(tmp_path):
    with pytest.raises(SystemExit):  # argparse guards --provider choices
        main(["--provider", "nope", "--output-dir", str(tmp_path / "raw")])


def test_collect_once_reports_progress(tmp_path):
    provider = MockProvider(symbols=("600000.SH", "000001.SZ"))
    seen: list[tuple[int, int, str]] = []
    report = collect_once(
        provider=provider,
        store=_store(tmp_path),
        symbols=["600000.SH", "000001.SZ"],
        end=_END,
        pause=0.0,
        sleep=lambda _: None,
        dry_run=True,
        on_progress=lambda done, total, symbol: seen.append((done, total, symbol)),
    )

    assert len(report.updated) == 2
    # reported before each symbol, then once more after the last one
    assert seen[0] == (0, 2, "600000.SH")
    assert seen[-1] == (2, 2, "")
    assert [item[0] for item in seen] == [0, 1, 2]


def test_collect_once_stops_when_requested(tmp_path):
    provider = MockProvider(symbols=("600000.SH", "000001.SZ"))
    polls = {"count": 0}

    def should_stop() -> bool:
        polls["count"] += 1
        return polls["count"] > 1  # let the first symbol finish, then stop

    report = collect_once(
        provider=provider,
        store=_store(tmp_path),
        symbols=["600000.SH", "000001.SZ"],
        end=_END,
        pause=0.0,
        sleep=lambda _: None,
        dry_run=True,
        should_stop=should_stop,
    )

    assert list(report.updated) == ["600000.SH"]
    assert report.note and "stopped after 1/2" in report.note
