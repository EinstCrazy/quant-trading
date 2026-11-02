"""Panel cache tests.

The cache exists so a screen does not re-read a thousand small files. What has
to hold: a build reads the store once, an update appends a day, and neither
silently mixes two sources or two adjustments.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_research.data.cache import (
    build_panel_cache,
    cache_path,
    load_panel_cache,
    update_panel_cache,
)
from quant_research.data.loader import PricePanel


def _panel(days: int = 40, symbols: tuple[str, ...] = ("A.SH", "B.SH")) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    rng = np.random.default_rng(11)
    prices = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, (days, len(symbols))), axis=0),
        index=index,
        columns=list(symbols),
    )
    return PricePanel(
        prices=prices,
        extra={
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.998,
            "volume": prices * 1000.0,
            "amount": prices * 1e6,
        },
    )


def _write_store(root: Path, provider: str, panel: PricePanel) -> Path:
    directory = root / "raw" / provider / "daily"
    directory.mkdir(parents=True, exist_ok=True)
    for symbol in panel.prices.columns:
        frame = pd.DataFrame({name: frame[symbol] for name, frame in panel.extra.items()})
        frame["close"] = panel.prices[symbol]
        frame.to_parquet(directory / f"{symbol}.parquet")
    return directory


def test_build_then_load_returns_the_same_prices(tmp_path):
    panel = _panel()
    _write_store(tmp_path, "baostock", panel)

    path = build_panel_cache(tmp_path, "baostock", "qfq")
    assert path.is_file()

    loaded = load_panel_cache(tmp_path, "baostock", "qfq")
    # freq is not compared: the cache is a pivot, and a market with suspensions
    # has no regular frequency to promise
    pd.testing.assert_frame_equal(loaded.prices, panel.prices, check_dtype=False, check_freq=False)
    assert loaded.symbols == tuple(sorted(panel.prices.columns))


def test_the_cache_keeps_every_field_the_files_had(tmp_path):
    """Including amount, which the panel loader's optional fields leave out."""
    panel = _panel()
    _write_store(tmp_path, "baostock", panel)

    build_panel_cache(tmp_path, "baostock", "qfq")
    loaded = load_panel_cache(tmp_path, "baostock", "qfq")

    assert loaded.has_field("amount")
    pd.testing.assert_frame_equal(
        loaded.field("amount"), panel.extra["amount"], check_dtype=False, check_freq=False
    )


def test_update_appends_a_day_without_touching_the_past(tmp_path):
    panel = _panel()
    _write_store(tmp_path, "baostock", panel)
    update_panel_cache(panel, tmp_path, "baostock", "qfq")

    day = panel.prices.index[-1]
    extra_day = pd.bdate_range(day, periods=2, name="date")[-1]
    added = panel.prices.copy()
    added.loc[extra_day] = added.loc[day] * 1.01
    extra_fields = {name: frame.reindex(added.index).ffill() for name, frame in panel.extra.items()}
    update_panel_cache(PricePanel(prices=added, extra=extra_fields), tmp_path, "baostock", "qfq")

    loaded = load_panel_cache(tmp_path, "baostock", "qfq")
    assert loaded.prices.index[-1] == extra_day
    assert len(loaded.prices) == len(panel.prices) + 1
    # the earlier day was not rewritten by the append
    pd.testing.assert_series_equal(loaded.prices.loc[day], panel.prices.loc[day], check_dtype=False)


def test_update_is_idempotent(tmp_path):
    panel = _panel()
    _write_store(tmp_path, "baostock", panel)

    update_panel_cache(panel, tmp_path, "baostock", "qfq")
    first = load_panel_cache(tmp_path, "baostock", "qfq").prices
    update_panel_cache(panel, tmp_path, "baostock", "qfq")
    second = load_panel_cache(tmp_path, "baostock", "qfq").prices

    pd.testing.assert_frame_equal(first, second)


def test_a_newer_value_wins_over_the_cached_one(tmp_path):
    panel = _panel()
    _write_store(tmp_path, "baostock", panel)
    update_panel_cache(panel, tmp_path, "baostock", "qfq")

    corrected = panel.prices.copy()
    corrected.iloc[-1, 0] = 999.0
    corrected_extra = {name: frame.copy() for name, frame in panel.extra.items()}
    update_panel_cache(
        PricePanel(prices=corrected, extra=corrected_extra), tmp_path, "baostock", "qfq"
    )

    loaded = load_panel_cache(tmp_path, "baostock", "qfq")
    assert loaded.prices.iloc[-1].iloc[0] == pytest.approx(999.0)


def test_provider_and_adjustment_are_part_of_the_key(tmp_path):
    """Mixing two adjustments would rank numbers that are not comparable."""
    assert cache_path(tmp_path, "baostock", "qfq") != cache_path(tmp_path, "baostock", "none")
    assert cache_path(tmp_path, "baostock", "qfq") != cache_path(tmp_path, "akshare", "qfq")

    panel = _panel()
    _write_store(tmp_path, "baostock", panel)
    build_panel_cache(tmp_path, "baostock", "qfq")

    with pytest.raises(FileNotFoundError):
        load_panel_cache(tmp_path, "baostock", "none")


def test_suspended_names_survive_the_cache(tmp_path):
    panel = _panel()
    panel.prices.iloc[:10, 0] = np.nan  # not listed yet
    _write_store(tmp_path, "baostock", panel)

    build_panel_cache(tmp_path, "baostock", "qfq")
    loaded = load_panel_cache(tmp_path, "baostock", "qfq")

    assert loaded.prices.isna().any().any()
    assert loaded.meta.missing_policy == "keep"


def test_loading_a_cache_that_was_never_built_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="build it first"):
        load_panel_cache(tmp_path, "baostock", "qfq")
