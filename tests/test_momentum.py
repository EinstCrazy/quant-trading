import pandas as pd
import pytest

from quant_research.strategies import STRATEGY_REGISTRY, apply_lag, get_signal
from quant_research.strategies.momentum import moving_average_signal
from quant_research.strategies.random import random_signal


def test_signal_is_flat_until_slow_window_is_available():
    close = pd.Series([10, 11, 12, 13])

    signal = moving_average_signal(close, fast_window=2, slow_window=3)

    assert signal.tolist() == [0, 0, 1, 1]


def test_fast_window_must_be_shorter_than_slow_window():
    with pytest.raises(ValueError, match="fast_window must be less"):
        moving_average_signal(pd.Series([10, 11]), fast_window=3, slow_window=2)


def test_window_sizes_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        moving_average_signal(pd.Series([10.0, 11.0]), fast_window=0, slow_window=2)


def test_dataframe_input_is_computed_column_wise(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=10, slow_window=30)

    assert isinstance(signal, pd.DataFrame)
    assert signal.shape == sample_prices.shape
    for symbol in sample_prices.columns:
        expected = moving_average_signal(sample_prices[symbol], fast_window=10, slow_window=30)
        assert signal[symbol].tolist() == expected.tolist()


def test_min_periods_shifts_the_first_signal(sample_prices):
    early = moving_average_signal(sample_prices, fast_window=5, slow_window=20, min_periods=5)
    late = moving_average_signal(sample_prices, fast_window=5, slow_window=20)

    assert int(early.sum().sum()) > int(late.sum().sum())
    assert bool((late.iloc[:19] == 0).all().all())


def test_signal_returns_raw_unlagged_values(sample_prices):
    """Signals must be unlagged: the engine owns the shift."""
    signal = moving_average_signal(sample_prices, fast_window=5, slow_window=20)

    assert signal.iloc[0].sum() == 0
    assert apply_lag(signal).iloc[0].isna().all()


def test_registry_exposes_registered_strategies(sample_prices):
    assert "ma_cross" in STRATEGY_REGISTRY
    assert "random" in STRATEGY_REGISTRY

    for name in STRATEGY_REGISTRY:
        signal = get_signal(name)(sample_prices)
        assert signal.shape == sample_prices.shape


def test_get_signal_rejects_unknown_name():
    with pytest.raises(KeyError, match="Unknown strategy"):
        get_signal("does_not_exist")


def test_random_signal_is_reproducible_and_bounded(sample_prices):
    first = random_signal(sample_prices, probability=1.0, seed=42)
    second = random_signal(sample_prices, probability=1.0, seed=42)
    empty = random_signal(sample_prices, probability=0.0, seed=42)

    assert first.equals(second)
    assert int(empty.sum().sum()) == 0

    with pytest.raises(ValueError, match="probability"):
        random_signal(sample_prices, probability=1.5)


def test_apply_lag_shifts_by_one_bar(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=5, slow_window=20)

    lagged = apply_lag(signal, periods=1).fillna(0)

    assert (lagged.iloc[1:].to_numpy() == signal.iloc[:-1].to_numpy()).all()

    with pytest.raises(ValueError, match="non-negative"):
        apply_lag(signal, periods=-1)
