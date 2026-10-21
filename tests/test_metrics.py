import numpy as np
import pandas as pd
import pytest

from quant_research.metrics import (
    alpha_beta,
    annualized_sharpe,
    annualized_volatility,
    benchmark_buy_hold,
    cagr,
    calmar_ratio,
    cumulative_return,
    drawdown_series,
    drawdown_table,
    information_ratio,
    max_drawdown,
    profit_factor,
    sortino_ratio,
    summarize,
    tracking_error,
    win_rate,
)


def _returns(values, start="2024-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), name="return")


def test_annualized_sharpe_handles_degenerate_inputs():
    assert annualized_sharpe(_returns([0.01, 0.01, 0.01])) == 0.0
    assert annualized_sharpe(_returns([])) == 0.0
    assert annualized_sharpe(_returns([0.02])) == 0.0
    with pytest.raises(ValueError, match="periods_per_year"):
        annualized_sharpe(_returns([0.01, 0.02]), periods_per_year=0)


def test_annualized_sharpe_scales_with_frequency():
    returns = _returns([0.01, -0.005, 0.02, 0.0])

    assert annualized_sharpe(returns, periods_per_year=252) > annualized_sharpe(
        returns, periods_per_year=12
    )


def test_max_drawdown_edge_cases():
    assert max_drawdown(_returns([])) == 0.0
    assert max_drawdown(_returns([0.01, 0.01, 0.01])) == 0.0
    with pytest.raises(ValueError, match="greater than -100%"):
        max_drawdown(_returns([-1.0]))


def test_max_drawdown_on_known_sample():
    returns = _returns([0.10, -0.20, 0.05])

    # wealth: 1.10, 0.88, 0.924 -> deepest drawdown is -20%
    assert max_drawdown(returns) == pytest.approx(-0.2, abs=1e-12)


def test_cumulative_return_and_cagr():
    returns = _returns([0.01] * 252)

    assert cumulative_return(returns) == pytest.approx(1.01**252 - 1, rel=1e-9)
    assert cagr(returns) == pytest.approx(1.01**252 - 1, rel=1e-9)
    assert cagr(_returns([])) == 0.0


def test_annualized_volatility_and_sortino():
    rng = np.random.default_rng(3)
    returns = _returns(rng.normal(0.0005, 0.01, 300))

    assert annualized_volatility(returns) > 0.0
    assert sortino_ratio(returns) != 0.0
    assert sortino_ratio(_returns([0.01, 0.01])) == 0.0  # no downside -> no ratio


def test_calmar_ratio_uses_max_drawdown():
    returns = _returns([0.02, -0.10, 0.05, 0.03])

    assert calmar_ratio(returns) == pytest.approx(
        cagr(returns) / abs(max_drawdown(returns)), rel=1e-9
    )
    assert calmar_ratio(_returns([0.01, 0.01])) == 0.0


def test_drawdown_series_and_table():
    returns = _returns([0.10, -0.20, -0.10, 0.40, 0.02])
    series = drawdown_series(returns)
    table = drawdown_table(returns)

    assert series.iloc[0] == 0.0
    assert series.min() < 0
    assert list(table.columns) == [
        "start",
        "trough",
        "end",
        "depth",
        "length_days",
        "recovery_days",
        "recovered",
    ]
    assert table.iloc[0]["depth"] == pytest.approx(series.min(), rel=1e-9)
    assert bool(table.iloc[0]["recovered"]) is True


def test_drawdown_table_marks_unrecovered_episode():
    returns = _returns([0.05, -0.30, -0.10])

    table = drawdown_table(returns)

    assert len(table) == 1
    assert bool(table.iloc[0]["recovered"]) is False
    assert pd.isna(table.iloc[0]["recovery_days"])


def test_win_rate_and_profit_factor():
    returns = _returns([0.02, -0.01, 0.03, -0.02])

    assert win_rate(returns) == pytest.approx(0.5)
    assert profit_factor(returns) == pytest.approx(0.05 / 0.03)
    assert profit_factor(_returns([0.01, 0.02])) == float("inf")
    assert profit_factor(_returns([-0.01])) == 0.0


def test_benchmark_buy_hold_is_equal_weighted(sample_prices):
    benchmark = benchmark_buy_hold(sample_prices)

    assert len(benchmark) == len(sample_prices)
    assert benchmark.iloc[0] == 0.0
    assert benchmark.name == "benchmark"

    single = benchmark_buy_hold(sample_prices, symbol="ALPHA")
    expected = sample_prices["ALPHA"].pct_change().fillna(0.0)
    pd.testing.assert_series_equal(single, expected.rename("benchmark"))


def test_alpha_beta_against_itself(sample_prices):
    benchmark = benchmark_buy_hold(sample_prices)

    alpha, beta = alpha_beta(benchmark, benchmark)

    assert beta == pytest.approx(1.0, abs=1e-9)
    assert alpha == pytest.approx(0.0, abs=1e-9)


def test_tracking_error_and_information_ratio(sample_prices):
    benchmark = benchmark_buy_hold(sample_prices)
    active = benchmark * 0.5

    assert tracking_error(active, benchmark) > 0.0
    assert tracking_error(benchmark, benchmark) == pytest.approx(0.0, abs=1e-12)
    assert information_ratio(benchmark, benchmark) == 0.0


def test_summarize_without_benchmark(sample_prices):
    returns = benchmark_buy_hold(sample_prices)

    summary = summarize(returns)

    assert summary["n_periods"] == len(returns)
    assert "benchmark_return" not in summary.index
    assert summary["cumulative_return"] == pytest.approx(cumulative_return(returns))


def test_summarize_with_benchmark(sample_prices):
    returns = benchmark_buy_hold(sample_prices, symbol="ALPHA")
    benchmark = benchmark_buy_hold(sample_prices)

    summary = summarize(returns, benchmark=benchmark)

    for key in ("benchmark_return", "relative_return", "alpha", "beta", "information_ratio"):
        assert key in summary.index
    assert summary["beta"] > 0

    with pytest.raises(ValueError, match="periods_per_year"):
        summarize(returns, periods_per_year=0)
