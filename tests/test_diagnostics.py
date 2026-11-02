"""Factor diagnostics tests.

The point of these is not that the arithmetic is hard — it is that the awkward
decisions stay decided: the last rows of a forward return are empty, the
t-statistic is discounted for overlapping windows, and a cross-section too thin
to bucket is skipped rather than chopped into single-name buckets.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant_research.scoring import ma_distance
from quant_research.scoring.diagnostics import (
    available_factors,
    compute_factor,
    factor_ic,
    forward_return,
    ic_decay,
    quantile_returns,
    summarise_ic,
)


def _panel(days: int = 80, n_symbols: int = 12, seed: int = 1) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=days, name="date")
    rng = np.random.default_rng(seed)
    columns = [f"S{i}.SH" for i in range(n_symbols)]
    return pd.DataFrame(
        100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, (days, n_symbols)), axis=0),
        index=index,
        columns=columns,
    )


def test_forward_return_is_tomorrows_move_and_nothing_else():
    close = pd.DataFrame(
        {"A": [100.0, 110.0, 99.0]}, index=pd.bdate_range("2024-01-01", periods=3, name="date")
    )

    ahead = forward_return(close, horizon=1)

    assert ahead["A"].iloc[0] == pytest.approx(0.1)  # 110 / 100 - 1
    assert ahead["A"].iloc[1] == pytest.approx(-0.1)  # 99 / 110 - 1
    assert pd.isna(ahead["A"].iloc[2])  # has not happened yet


def test_forward_return_rejects_a_horizon_that_makes_no_sense():
    with pytest.raises(ValueError, match="horizon"):
        forward_return(_panel(), horizon=0)


def test_ic_is_one_when_the_score_knows_the_future():
    """The ceiling case: if the factor *is* the forward return, IC must be 1."""
    close = _panel()
    ahead = forward_return(close, horizon=1)

    assert factor_ic(ahead, ahead).dropna().iloc[0] == pytest.approx(1.0)
    assert factor_ic(-ahead, ahead).dropna().iloc[0] == pytest.approx(-1.0)


def test_ic_ignores_the_names_either_side_is_missing():
    close = _panel(days=40)
    ahead = forward_return(close, horizon=5)
    factor = ma_distance(close)
    factor.iloc[:, 0] = np.nan  # one name never scores

    ic = factor_ic(factor, ahead).dropna()

    assert not ic.empty


def test_the_t_statistic_is_discounted_for_overlapping_windows():
    """Five-day returns measured daily reuse four of five days: n overstates the case."""
    ic = pd.Series([0.05, 0.02, -0.01, 0.03, 0.04] * 20)

    daily = summarise_ic(ic, horizon=1)
    weekly = summarise_ic(ic, horizon=5)

    assert weekly.t_stat == pytest.approx(daily.t_stat)  # raw is the same
    assert weekly.t_stat_adjusted == pytest.approx(daily.t_stat / math.sqrt(5))


def test_a_flat_ic_has_no_t_statistic_to_report():
    """A t-statistic divides by the spread; a spread of zero must not become inf."""
    with pytest.raises(ValueError, match="no variation"):
        summarise_ic(pd.Series([0.0] * 30))


def test_buckets_come_out_monotonic_when_the_factor_orders_returns():
    close = _panel()
    ahead = forward_return(close, horizon=5)

    spread = quantile_returns(ahead, ahead, quantiles=5)

    assert spread.monotonicity == pytest.approx(1.0)
    assert spread.returns.is_monotonic_increasing


def test_a_reversed_factor_produces_a_descending_ladder():
    close = _panel()
    ahead = forward_return(close, horizon=5)

    spread = quantile_returns(-ahead, ahead, quantiles=5)

    assert spread.monotonicity == pytest.approx(-1.0)
    assert spread.returns.is_monotonic_decreasing


def test_a_cross_section_too_thin_to_bucket_is_skipped_not_chopped():
    """Four names into five buckets is one name per bucket — an average of one."""
    close = _panel(n_symbols=4)
    ahead = forward_return(close, horizon=5)

    with pytest.raises(ValueError, match="enough names"):
        quantile_returns(ahead, ahead, quantiles=5)


def test_the_bucket_average_reports_how_many_names_it_used():
    close = _panel()
    ahead = forward_return(close, horizon=5)

    spread = quantile_returns(ahead, ahead, quantiles=5)

    assert (spread.sizes >= 2).all()
    assert spread.sizes.sum() == pytest.approx(12)  # every name lands in one bucket


def test_ic_decay_covers_every_horizon_it_is_given():
    close = _panel(days=120)

    table = ic_decay(ma_distance(close), close, horizons=(1, 5))

    assert list(table.index) == [1.0, 5.0]
    assert {"mean_ic", "t_stat", "t_adjusted", "positive_share"} <= set(table.columns)


def test_the_registry_builds_the_same_factor_the_screen_uses():
    """Two copies of "what ma_distance means" would eventually disagree."""
    close = _panel()

    assert compute_factor("ma_distance", close).equals(ma_distance(close))


def test_every_advertised_factor_can_actually_be_built():
    close = _panel(days=120)

    for name in available_factors():
        assert compute_factor(name, close).shape == close.shape


def test_an_unknown_factor_name_is_an_error():
    with pytest.raises(ValueError, match="unknown factor"):
        compute_factor("momentum_2", _panel())


def test_the_command_prints_one_row_per_factor(monkeypatch, capsys, tmp_path):
    """A diagnostics command that crashes on a thin factor is useless at 6 p.m."""
    from quant_research import factor_cli
    from quant_research.data.loader import PricePanel
    from quant_research.indicators import IndicatorParams

    prices = _panel(days=140)
    panel = PricePanel(prices=prices, extra={})
    cache = tmp_path / "cache.parquet"
    cache.touch()

    class _Settings:
        class collection:
            provider = "baostock"
            adjust = "qfq"

        class data:
            directory = str(tmp_path)

    monkeypatch.setattr(factor_cli, "load_settings", lambda: _Settings())
    monkeypatch.setattr(factor_cli, "cache_path", lambda *args, **kwargs: cache)
    monkeypatch.setattr(factor_cli, "load_panel_cache", lambda *args, **kwargs: panel)
    monkeypatch.setattr(factor_cli, "params_from_settings", lambda settings: IndicatorParams())

    assert factor_cli.main(["--factors", "ma_distance", "--horizon", "5"]) == 0

    out = capsys.readouterr().out
    assert "ma_distance" in out
    assert "t(adj)" in out
    assert "读法" in out
