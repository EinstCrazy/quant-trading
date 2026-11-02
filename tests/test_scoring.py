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
from quant_research.indicators import boll
from quant_research.scoring import (
    boll_position,
    combine,
    factor_correlation,
    macd_trend,
    momentum,
    rsi_reversal,
    screen,
    to_target_weights,
    zscore_by_day,
)
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


# --- factors built on the technical indicators ------------------------------


def test_macd_trend_is_the_same_for_the_same_percentage_move():
    """A 5-yuan name and a 500-yuan name making identical percentage moves must
    score identically. Ranking the raw histogram would just favour the expensive one."""
    index = pd.bdate_range("2024-01-01", periods=80, name="date")
    shape = np.cumprod(1.0 + np.random.default_rng(1).normal(0.0, 0.01, 80))

    cheap = pd.DataFrame({"cheap": 5.0 * shape}, index=index)
    dear = pd.DataFrame({"dear": 500.0 * shape}, index=index)

    assert np.allclose(
        macd_trend(cheap)["cheap"].dropna().to_numpy(),
        macd_trend(dear)["dear"].dropna().to_numpy(),
    )


def test_boll_position_is_the_share_of_the_band_below_the_close():
    prices = _panel(days=80).prices[["S0.SH"]]
    bands = boll(prices, n=20, k=2.0)

    position = boll_position(prices, window=20, width=2.0)
    expected = (prices - bands.lower) / (bands.upper - bands.lower)

    assert np.allclose(position.dropna().to_numpy(), expected.dropna().to_numpy())


def test_boll_position_has_no_answer_for_a_flat_window():
    """A band of zero width has no inside: 0.5 would be an invented answer."""
    prices = pd.DataFrame({"A": [10.0] * 25}, index=pd.bdate_range("2024-01-01", periods=25))
    assert boll_position(prices, window=20).iloc[-1].isna().all()


def test_rsi_reversal_points_the_opposite_way_to_momentum():
    """This is the reason both are opt-in: switched on together they cancel out."""
    index = pd.bdate_range("2024-01-01", periods=120, name="date")
    rising = pd.DataFrame({"up": np.linspace(10.0, 20.0, 120)}, index=index)

    assert momentum(rising, window=60, skip_recent=5).iloc[-1, 0] > 0
    assert rsi_reversal(rising, window=14).iloc[-1, 0] < 0  # RSI is ~100, so 50-100 < 0


@pytest.mark.parametrize("factor", [rsi_reversal, macd_trend, boll_position])
def test_indicator_factors_do_not_look_into_the_future(factor):
    """Cut the last 30 days off and every earlier value must be unchanged."""
    prices = _panel(days=160).prices

    full = factor(prices)
    truncated = factor(prices.iloc[:-30])

    assert np.allclose(
        full.loc[truncated.index].to_numpy(),
        truncated.to_numpy(),
        equal_nan=True,
    )


def test_indicator_factors_are_off_unless_asked_for():
    result = screen(_panel(), top_n=3)

    assert "rsi_reversal" not in result.picks.columns
    assert "macd_trend" not in result.picks.columns


def test_an_enabled_indicator_factor_appears_in_the_picks():
    """A pick nobody can decompose into its parts cannot be argued with."""
    result = screen(_panel(), top_n=3, indicator_factors=("macd_trend",))

    assert "macd_trend" in result.picks.columns
    assert result.picks["macd_trend"].notna().all()


def test_a_mistyped_indicator_factor_is_an_error_not_a_silent_no_op():
    """Ignoring it would change the score without changing the request."""
    with pytest.raises(ValueError, match="rsi_reversel"):
        screen(_panel(), indicator_factors=("rsi_reversel",))


def test_factor_correlation_notices_a_factor_that_is_a_rescaled_twin():
    """Rank correlation of an affine transform is 1, whatever the scale."""
    prices = _panel(days=80).prices
    base = ma_distance(prices)

    matrix = factor_correlation({"ma_distance": base, "its_twin": 2.0 * base + 1.0})

    assert matrix.loc["ma_distance", "its_twin"] == pytest.approx(1.0)


def test_unrelated_factors_come_out_near_zero():
    rng = np.random.default_rng(7)
    index = pd.bdate_range("2024-01-01", periods=64, name="date")
    columns = [f"S{i}" for i in range(10)]
    first = pd.DataFrame(rng.normal(size=(64, 10)), index=index, columns=columns)
    second = pd.DataFrame(rng.normal(size=(64, 10)), index=index, columns=columns)

    value = factor_correlation({"first": first, "second": second}).loc["first", "second"]

    assert abs(value) < 0.2


def test_correlation_needs_at_least_two_factors():
    with pytest.raises(ValueError, match="at least two"):
        factor_correlation({"only": _panel(days=80).prices})


def test_screen_says_it_out_loud_when_two_factors_are_one_judgement(monkeypatch):
    """The score cannot notice redundancy; the log has to."""
    from quant_research.scoring.screen import _warn_about_overlap, logger

    warnings: list[str] = []
    monkeypatch.setattr(logger, "warning", lambda message, *args: warnings.append(message % args))
    prices = _panel(days=100).prices

    _warn_about_overlap({"ma_distance": ma_distance(prices), "its_twin": 2.0 * ma_distance(prices)})

    assert warnings and "加两次权" in warnings[0]
