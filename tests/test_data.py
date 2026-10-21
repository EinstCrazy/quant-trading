from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_research.data import PricePanel, describe_panel, load_close_prices, load_price_panel


def _write_close_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index_label="date")


def _write_panel_csv(path: Path, frame: pd.DataFrame) -> None:
    """Write a long-format CSV (date, symbol, close) usable by the loader."""
    long = frame.copy()
    long.index.name = "date"
    long = long.reset_index().melt(id_vars="date", var_name="symbol", value_name="close")
    long.to_csv(path, index=False)


def test_load_close_prices_rejects_missing_column(tmp_path):
    path = tmp_path / "bad.csv"
    _write_close_csv(path, pd.DataFrame({"price": [1.0, 2.0]}))

    with pytest.raises(ValueError, match="must contain"):
        load_close_prices(path)


def test_load_close_prices_rejects_duplicate_dates(tmp_path):
    path = tmp_path / "dup.csv"
    frame = pd.DataFrame(
        {"close": [1.0, 2.0]},
        index=pd.to_datetime(["2024-01-01", "2024-01-01"]),
    )
    _write_close_csv(path, frame)

    with pytest.raises(ValueError, match="duplicate"):
        load_close_prices(path)


def test_load_close_prices_rejects_missing_values(tmp_path):
    path = tmp_path / "na.csv"
    frame = pd.DataFrame(
        {"close": [1.0, np.nan]},
        index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
    )
    _write_close_csv(path, frame)

    with pytest.raises(ValueError, match="missing values"):
        load_close_prices(path)


def test_load_close_prices_rejects_non_positive_prices(tmp_path):
    path = tmp_path / "neg.csv"
    frame = pd.DataFrame(
        {"close": [1.0, 0.0]},
        index=pd.to_datetime(["2024-01-01", "2024-01-02"]),
    )
    _write_close_csv(path, frame)

    with pytest.raises(ValueError, match="positive"):
        load_close_prices(path)


def test_load_close_prices_sorts_by_date(tmp_path):
    path = tmp_path / "ok.csv"
    frame = pd.DataFrame(
        {"close": [2.0, 1.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-01"]),
    )
    _write_close_csv(path, frame)

    prices = load_close_prices(path)

    assert prices.index.is_monotonic_increasing
    assert prices.tolist() == [1.0, 2.0]


def test_load_price_panel_from_long_csv(tmp_path, sample_prices):
    path = tmp_path / "panel.csv"
    _write_panel_csv(path, sample_prices)

    panel = load_price_panel(path)

    assert list(panel.prices.columns) == list(sample_prices.columns)
    assert len(panel.prices) == len(sample_prices)
    assert panel.meta.source == str(path)
    assert panel.meta.symbols == tuple(sample_prices.columns)
    assert panel.meta.start == sample_prices.index.min()


def test_load_price_panel_keeps_extra_fields(tmp_path, sample_prices):
    path = tmp_path / "panel.csv"
    long = sample_prices.copy()
    long.index.name = "date"
    long = long.reset_index().melt(id_vars="date", var_name="symbol", value_name="close")
    long["open"] = long["close"] * 0.99
    long["volume"] = 1000
    long.to_csv(path, index=False)

    panel = load_price_panel(path)

    assert panel.has_field("open")
    assert panel.has_field("volume")
    assert not panel.has_field("vwap")
    assert panel.field("open").shape == panel.prices.shape
    with pytest.raises(KeyError):
        panel.field("vwap")


def test_load_price_panel_from_directory(tmp_path, sample_prices):
    directory = tmp_path / "raw"
    directory.mkdir()
    for symbol in sample_prices.columns:
        series = sample_prices[symbol].rename("close")
        series.to_csv(directory / f"{symbol}.csv", index_label="date")

    panel = load_price_panel(directory)

    assert set(panel.prices.columns) == set(sample_prices.columns)
    assert len(panel.prices) == len(sample_prices)


def test_load_price_panel_filters_symbols_and_dates(tmp_path, sample_prices):
    path = tmp_path / "panel.csv"
    _write_panel_csv(path, sample_prices)
    start = str(sample_prices.index[10].date())

    panel = load_price_panel(path, symbols=["ALPHA"], start=start)

    assert list(panel.prices.columns) == ["ALPHA"]
    assert panel.prices.index.min() >= pd.Timestamp(start)


def test_load_price_panel_rejects_unknown_symbol(tmp_path, sample_prices):
    path = tmp_path / "panel.csv"
    _write_panel_csv(path, sample_prices)

    with pytest.raises(ValueError, match="not found"):
        load_price_panel(path, symbols=["MISSING"])


def test_load_price_panel_missing_policy(tmp_path, sample_prices):
    path = tmp_path / "panel.csv"
    with_gap = sample_prices.copy()
    with_gap.iloc[5, 0] = np.nan
    _write_panel_csv(path, with_gap)

    with pytest.raises(ValueError, match="missing values"):
        load_price_panel(path)

    panel = load_price_panel(path, missing_policy="keep")
    assert panel.prices.isna().sum().sum() == 1
    assert panel.meta.missing_policy == "keep"


def test_describe_panel_reports_missing_and_jumps(sample_prices):
    prices = sample_prices.copy()
    prices.iloc[3, 0] = prices.iloc[2, 0] * 0.4  # -60% single-day jump
    prices.iloc[4, 1] = np.nan
    panel = PricePanel(prices=prices)

    report = describe_panel(panel)

    assert report.n_obs == len(prices)
    assert report.monotonic_dates is True
    assert report.is_clean is False
    assert report.missing_rate["BETA"] == pytest.approx(1 / len(prices))
    assert any(symbol == "ALPHA" for _, symbol, _ in report.jumps)
    assert set(report.to_frame().columns) == {"missing_rate", "non_positive"}


def test_describe_panel_on_clean_data(sample_prices):
    report = describe_panel(PricePanel(prices=sample_prices))

    assert report.is_clean
    assert report.jumps == []
    assert report.start == sample_prices.index.min()
    assert report.end == sample_prices.index.max()
