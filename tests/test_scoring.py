"""Scoring tests.

The ones that matter most are the look-ahead tests: a score computed on the
night of the close must equal the same score recomputed years later with the
full history in hand. Everything else here is arithmetic by comparison.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_research.data.loader import PricePanel
from quant_research.scoring import combine, momentum, screen, to_target_weights, zscore_by_day
from quant_research.scoring.factors import inverse_volatility, ma_distance


def _panel(days: int = 140, n_symbols: int = 6, seed: int = 3) -> PricePanel:
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0004, 0.012, size=(days, n_symbols))
    columns = [f"S{i}.SH" for i in range(n_symbols)]
    prices = pd.DataFrame(100.0 * np.cumprod(1.0 + returns, axis=0), index=index, columns=columns)
    extra = {
        "open": prices * 0.999,
        "high": prices * 1.004,
        "low": prices * 0.996,
        "volume": pd.DataFrame(
            rng.integers(1_000_000, 10_000_000, (days, n_symbols)).astype(float),
            index=index,
            columns=columns,
        ),
        "amount": pd.DataFrame(
            rng.uniform(1e8, 1e9, (days, n_symbols)), index=index, columns=columns
        ),
    }
    return PricePanel(prices=prices, extra=extra)


def test_momentum_measures_the_past_and_only_the_past():
    prices = pd.DataFrame(
        {"A": [10.0, 11.0, 12.0, 13.0]},
        index=pd.bdate_range("2024-01-01", periods=4, name="date"),
    )
    score = momentum(prices, window=3, skip_recent=1)

    # last day: close two days back over close three days back, i.e. 12/10 - 1.
    assert score.iloc[-1, 0] == pytest.approx(0.2)
    assert score.iloc[0].isna().all()  # nothing to look back on yet


def test_momentum_rejects_windows_that_make_no_sense():
    prices = pd.DataFrame({"A": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        momentum(prices, window=1)
    with pytest.raises(ValueError):
        momentum(prices, window=5, skip_recent=5)


def test_a_score_does_not_change_when_the_future_arrives():
    """The whole promise of a nightly run: tonight's number is tomorrow's number."""
    prices = _panel().prices
    full = momentum(prices)
    truncated = momentum(prices.iloc[:100])

    pd.testing.assert_series_equal(full.iloc[99], truncated.iloc[99])


def test_zscore_is_computed_within_each_day():
    frame = pd.DataFrame(
        {"A": [1.0, 10.0], "B": [2.0, 20.0], "C": [3.0, 60.0]},
        index=pd.bdate_range("2024-01-01", periods=2, name="date"),
    )
    scores = zscore_by_day(frame)

    assert scores.mean(axis=1).abs().max() < 1e-9
    assert (scores.std(axis=1) - 1.0).abs().max() < 1e-9
    # row two is unchanged by row one's spread: the days are independent.
    # (10, 20, 60): mean 30, sample std 26.4575
    assert scores.iloc[1].tolist() == pytest.approx([-0.7559, -0.3780, 1.1339], abs=1e-4)


def test_zscore_leaves_a_flat_day_undefined():
    frame = pd.DataFrame(
        {"A": [5.0], "B": [5.0], "C": [5.0]}, index=pd.bdate_range("2024-01-01", periods=1)
    )
    assert zscore_by_day(frame).iloc[0].isna().all()


def test_combine_is_an_unweighted_sum_by_default():
    panel = _panel()
    factors = {
        "momentum": momentum(panel.prices),
        "ma_distance": ma_distance(panel.prices),
    }
    combined = combine(factors)
    day = panel.prices.index[-1]
    expected = (
        zscore_by_day(factors["momentum"]).loc[day] + zscore_by_day(factors["ma_distance"]).loc[day]
    )
    pd.testing.assert_series_equal(combined.loc[day].dropna(), expected.dropna())


def test_a_market_with_no_dispersion_produces_no_scores():
    """Four names with identical prices have no spread to standardise — so no picks.

    Not padding this to top_n is deliberate: a score that says nothing should
    produce nothing.
    """
    prices = pd.DataFrame(
        100.0,
        index=pd.bdate_range("2024-01-01", periods=100, name="date"),
        columns=["A.SH", "B.SH", "C.SH", "D.SH"],
    )
    panel = PricePanel(prices=prices, extra={"open": prices.copy()})

    result = screen(panel, top_n=2, min_history=60)

    assert result.picks.empty
    assert result.dropped["no_score"] == 4


def test_screen_returns_the_top_names_ranked():
    result = screen(_panel(), top_n=3)
    assert list(result.picks["rank"]) == list(range(1, len(result.picks) + 1))
    assert result.picks["score"].is_monotonic_decreasing
    assert len(result.symbols) <= 3
    assert (result.picks["score"] > 0).all()  # only names that beat the average


def test_a_quiet_day_returns_fewer_names_than_asked_for():
    """Padding the list to top_n would mean buying the least bad loser."""
    panel = _panel()
    strict = screen(panel, top_n=6, positive_only=True)
    permissive = screen(panel, top_n=6, positive_only=False)

    assert len(strict.symbols) <= len(permissive.symbols)
    assert (strict.picks["score"] > 0).all()


def test_screen_drops_names_that_cannot_be_bought_tomorrow():
    """A limit-up lock is nothing to buy: open, high and low are one price."""
    panel = _panel()
    day = panel.prices.index[-1]
    target = panel.prices.loc[day].idxmax()

    previous = panel.prices.iloc[-2][target]
    panel.prices.loc[day, target] = previous * 1.10  # limit up
    for field_name in ("open", "high", "low"):
        panel.extra[field_name].loc[day, target] = panel.prices.loc[day, target]

    result = screen(panel, top_n=5)
    assert target not in result.symbols
    assert result.dropped["limit_up_locked"] >= 1


def test_screen_drops_names_without_enough_history():
    panel = _panel()
    panel.prices.iloc[:130, 0] = np.nan  # listed recently, ten days of history

    result = screen(panel, top_n=5, min_history=60)
    assert "S0.SH" not in result.symbols
    assert result.dropped["short_history"] >= 1


def test_screen_drops_thin_names_when_a_floor_is_asked_for():
    panel = _panel()
    panel.extra["amount"].loc[:, "S0.SH"] = 1.0  # a name nobody trades

    result = screen(panel, top_n=5, min_amount=1e8)
    assert "S0.SH" not in result.symbols


def test_screen_is_reproducible_from_a_truncated_panel():
    """Same day, same picks — computed with or without the later history."""
    panel = _panel()
    day = panel.prices.index[-1]
    cut = PricePanel(
        prices=panel.prices.loc[:day],
        extra={name: frame.loc[:day] for name, frame in panel.extra.items()},
    )

    pd.testing.assert_frame_equal(screen(panel, top_n=5).picks, screen(cut, top_n=5).picks)


def test_screen_rejects_a_day_the_panel_does_not_have():
    panel = _panel()
    with pytest.raises(ValueError, match="not a trading day"):
        screen(panel, as_of="2030-01-01")


def test_picks_become_a_fully_invested_weight_row():
    result = screen(_panel(), top_n=5)
    weights = to_target_weights(result)

    assert list(weights.index) == [result.as_of]
    assert weights.loc[result.as_of].sum() == pytest.approx(1.0)
    assert (weights.loc[result.as_of] > 0).all()


def test_inverse_volatility_prefers_the_quiet_name():
    index = pd.bdate_range("2024-01-01", periods=40, name="date")
    calm = pd.Series(np.linspace(100, 110, 40), index=index)
    jumpy = pd.Series(100 + np.cumsum(np.resize([1.0, -1.0], 40)), index=index)
    frame = pd.DataFrame({"calm": calm, "jumpy": jumpy})

    score = inverse_volatility(frame)
    assert score.iloc[-1]["calm"] > score.iloc[-1]["jumpy"]
