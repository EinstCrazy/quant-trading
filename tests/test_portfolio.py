import numpy as np
import pandas as pd
import pytest

from quant_research.portfolio import (
    PortfolioPolicy,
    enforce_policy,
    signal_to_weights,
    signal_to_weights_with_policy,
)
from quant_research.strategies.momentum import moving_average_signal


def _row(values: list[float], columns: tuple[str, ...] = ("A", "B", "C")) -> pd.DataFrame:
    return pd.DataFrame([values], columns=list(columns), index=pd.Index([0], name="date"))


def _weights(values, columns=("A", "B", "C")) -> pd.DataFrame:
    return pd.DataFrame(values, columns=list(columns))


# --- policy construction -----------------------------------------------------


def test_policy_rejects_nonsense_limits():
    with pytest.raises(ValueError, match="max_weight must be positive"):
        PortfolioPolicy(max_weight=0.0)
    with pytest.raises(ValueError, match="cannot exceed max_gross"):
        PortfolioPolicy(max_weight=1.5, max_gross=1.0)
    with pytest.raises(ValueError, match="target_net"):
        PortfolioPolicy(target_net=2.0, max_gross=1.0)
    with pytest.raises(ValueError, match="scaling"):
        PortfolioPolicy(scaling="optimised")  # type: ignore[arg-type]


# --- constraints -------------------------------------------------------------


def test_water_fill_caps_one_name_and_gives_the_rest_away():
    raw = _row([0.7, 0.2, 0.1])
    policy = PortfolioPolicy(max_weight=0.4)

    result = enforce_policy(raw, policy)

    assert result.weights.iloc[0].tolist() == pytest.approx([0.4, 0.4, 0.2])
    assert float(result.weights.sum(axis=1).iloc[0]) == pytest.approx(1.0)
    assert result.stats["weight_capped"] == 1


def test_proportional_scaling_shrinks_the_whole_row():
    raw = _row([0.7, 0.2, 0.1])
    policy = PortfolioPolicy(max_weight=0.4, scaling="proportional")

    result = enforce_policy(raw, policy)

    # max_weight / max(row) = 0.4 / 0.7 -> everything scales down together
    assert result.weights.iloc[0].tolist() == pytest.approx([0.4, 0.4 / 0.7 * 0.2, 0.4 / 0.7 * 0.1])
    assert float(result.weights.sum(axis=1).iloc[0]) < 1.0


def test_total_leverage_is_scaled_not_clipped():
    raw = _row([0.8, 0.8, 0.0])
    policy = PortfolioPolicy(max_gross=1.0)

    result = enforce_policy(raw, policy)

    assert result.weights.iloc[0].tolist() == pytest.approx([0.5, 0.5, 0.0])
    assert result.stats["gross_scaled"] == 1


def test_target_net_is_achieved_for_a_long_only_book():
    raw = _row([1.0, 1.0, 1.0])
    policy = PortfolioPolicy(target_net=0.5)

    result = enforce_policy(raw, policy)

    assert float(result.weights.sum(axis=1).iloc[0]) == pytest.approx(0.5)
    assert result.weights.iloc[0].tolist() == pytest.approx([1 / 6, 1 / 6, 1 / 6])


def test_long_short_book_hits_both_net_and_gross():
    raw = _row([1.0, -1.0, 0.5, -0.5], columns=("A", "B", "C", "D"))
    policy = PortfolioPolicy(allow_short=True, max_gross=1.0, target_net=0.2)

    result = enforce_policy(raw, policy)

    row = result.weights.iloc[0]
    assert row.sum() == pytest.approx(0.2)
    assert row.abs().sum() == pytest.approx(1.0)
    assert (row[["A", "C"]] > 0).all()
    assert (row[["B", "D"]] < 0).all()


def test_short_weights_are_dropped_when_policy_forbids_them():
    raw = _row([1.0, -1.0, 0.0])
    policy = PortfolioPolicy(allow_short=False)

    result = enforce_policy(raw, policy)

    assert result.weights.iloc[0].tolist() == pytest.approx([1.0, 0.0, 0.0])
    assert result.stats["short_dropped"] == 1


def test_policy_is_idempotent():
    raw = _weights([[0.9, 0.05, 0.05], [0.4, 0.4, 0.4]])
    policy = PortfolioPolicy(max_weight=0.5, max_gross=1.0)

    once = enforce_policy(raw, policy)
    twice = enforce_policy(once.weights, policy)

    pd.testing.assert_frame_equal(once.weights, twice.weights)


def test_policy_reports_stats_per_date(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=5, slow_window=15)
    raw = signal_to_weights(signal, method="equal_weight")

    result = enforce_policy(raw, PortfolioPolicy(max_weight=0.2, target_net=1.0))

    assert result.stats["weight_capped"] > 0
    assert result.weights.max().max() <= 0.2 + 1e-12
    assert set(result.to_dict()["stats"]) == {
        "short_dropped",
        "weight_capped",
        "gross_scaled",
        "net_adjusted",
    }


# --- weighting methods -------------------------------------------------------


def test_inverse_volatility_gives_calm_names_more_capital():
    signal = _row([1.0, 1.0, 1.0])
    risk = _row([0.10, 0.20, 0.10])

    weights = signal_to_weights(signal, method="inverse_volatility", risk=risk)

    row = weights.iloc[0]
    assert row["A"] == pytest.approx(0.4)
    assert row["B"] == pytest.approx(0.2)
    assert row["C"] == pytest.approx(0.4)
    assert float(row.sum()) == pytest.approx(1.0)


def test_inverse_volatility_degrades_to_equal_weight_without_risk():
    signal = _row([1.0, 1.0, 1.0])

    weights = signal_to_weights(signal, method="inverse_volatility")

    assert weights.iloc[0].tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_inverse_volatility_skips_names_without_usable_risk():
    signal = _row([1.0, 1.0, 1.0])
    risk = _row([0.10, np.nan, 0.10])

    weights = signal_to_weights(signal, method="inverse_volatility", risk=risk)

    assert weights.iloc[0]["B"] == 0.0
    assert weights.iloc[0][["A", "C"]].tolist() == pytest.approx([0.5, 0.5])
    assert float(weights.iloc[0].sum()) == pytest.approx(1.0)


def test_signal_to_weights_applies_policy_when_supplied():
    signal = _row([1.0, 1.0, 1.0])
    policy = PortfolioPolicy(max_weight=0.4)

    weights = signal_to_weights(signal, method="equal_weight", policy=policy)

    assert weights.iloc[0].max() <= 0.4 + 1e-12
    assert float(weights.sum(axis=1).iloc[0]) == pytest.approx(1.0)


def test_signal_to_weights_with_policy_keeps_the_constraint_stats(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=5, slow_window=15)
    policy = PortfolioPolicy(max_weight=0.2)

    result = signal_to_weights_with_policy(signal, method="equal_weight", policy=policy)

    assert result.weights.shape == sample_prices.shape
    assert result.policy_result is not None
    assert result.policy_result.stats["weight_capped"] > 0
    assert result.weights.max().max() <= 0.2 + 1e-12
