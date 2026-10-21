import pandas as pd
import pytest

from quant_research.portfolio import signal_to_weights
from quant_research.strategies.momentum import moving_average_signal


def test_fixed_weights_map_positive_scores_to_exposure():
    signal = pd.DataFrame({"ASSET": [0.0, 1.0, 1.0, 0.0]})

    weights = signal_to_weights(signal, method="fixed", target_exposure=1.0)

    assert weights["ASSET"].tolist() == [0.0, 1.0, 1.0, 0.0]


def test_target_exposure_scales_fixed_weights():
    signal = pd.DataFrame({"ASSET": [1.0, 1.0]})

    weights = signal_to_weights(signal, target_exposure=0.5)

    assert weights["ASSET"].tolist() == [0.5, 0.5]


def test_equal_weight_spreads_exposure_across_active_symbols():
    signal = pd.DataFrame(
        {
            "A": [1.0, 1.0, 0.0],
            "B": [1.0, 0.0, 0.0],
            "C": [0.0, 0.0, 0.0],
        }
    )

    weights = signal_to_weights(signal, method="equal_weight", target_exposure=1.0)

    assert weights.loc[0].tolist() == [0.5, 0.5, 0.0]
    assert weights.loc[1].tolist() == [1.0, 0.0, 0.0]
    assert weights.loc[2].sum() == 0.0


def test_negative_scores_are_dropped_when_shorting_is_disabled():
    signal = pd.DataFrame({"ASSET": [1.0, -1.0, -0.5]})

    weights = signal_to_weights(signal, allow_short=False)

    assert weights["ASSET"].tolist() == [1.0, 0.0, 0.0]


def test_negative_scores_never_open_a_position():
    """Fixed sizing only maps positive scores to exposure, shorts or not."""
    signal = pd.DataFrame({"ASSET": [-1.0, -1.0]})

    for allow_short in (False, True):
        weights = signal_to_weights(signal, method="fixed", allow_short=allow_short)
        assert weights["ASSET"].tolist() == [0.0, 0.0]
        assert (weights >= 0).all().all()


def test_series_input_is_accepted():
    signal = pd.Series([0, 1, 1], name="ASSET")

    weights = signal_to_weights(signal)

    assert isinstance(weights, pd.DataFrame)
    assert weights["ASSET"].tolist() == [0.0, 1.0, 1.0]


def test_invalid_arguments_are_rejected():
    signal = pd.DataFrame({"ASSET": [1.0]})

    with pytest.raises(ValueError, match="method"):
        signal_to_weights(signal, method="risk_parity")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="target_exposure"):
        signal_to_weights(signal, target_exposure=-1.0)


def test_weights_cover_every_signal_date(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=10, slow_window=30)

    weights = signal_to_weights(signal, method="equal_weight")

    assert weights.shape == sample_prices.shape
    assert weights.index.equals(sample_prices.index)
    assert (weights.sum(axis=1) <= 1.0 + 1e-12).all()
