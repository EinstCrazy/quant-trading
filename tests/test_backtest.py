import numpy as np
import pandas as pd
import pytest

from quant_research.backtest import BacktestConfig, CostConfig, ExecutionConfig, run_backtest
from quant_research.backtest.engine import run_signal_backtest
from quant_research.strategies.momentum import moving_average_signal

ZERO_COST = CostConfig(commission_bps=0.0, slippage_bps=0.0)


def _fully_invested(prices: pd.DataFrame) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights.iloc[:, 0] = 1.0
    return weights


# 1. 手算一致性
def test_zero_cost_result_matches_hand_calculation(hand_prices):
    prices = hand_prices
    weights = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
    config = BacktestConfig(initial_cash=100_000.0, costs=ZERO_COST)

    result = run_backtest(prices, weights, config=config)

    expected = 100_000.0 * prices["ASSET"].iloc[-1] / prices["ASSET"].iloc[0]
    assert result.final_equity == pytest.approx(expected, rel=1e-9)
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["shares"] == pytest.approx(100_000.0 / 100.0, rel=1e-9)
    assert result.trades.iloc[0]["price"] == pytest.approx(100.0)


# 2. 成本单调性
def test_costs_reduce_final_equity_monotonically(sample_prices, flat_weights):
    equities = []
    for commission_bps in (0.0, 5.0, 20.0, 50.0):
        config = BacktestConfig(costs=CostConfig(commission_bps=commission_bps, slippage_bps=2.0))
        equities.append(run_backtest(sample_prices, flat_weights, config=config).final_equity)

    assert equities == sorted(equities, reverse=True)
    assert equities[-1] < equities[0]
    assert (
        run_backtest(
            sample_prices, flat_weights, config=BacktestConfig(costs=ZERO_COST)
        ).total_costs
        == 0.0
    )


# 3. 无前视偏差（因果性）
def test_future_prices_do_not_change_past_equity(sample_prices, flat_weights):
    config = BacktestConfig(costs=ZERO_COST)
    baseline = run_backtest(sample_prices, flat_weights, config=config)

    cutoff = 60
    altered = sample_prices.copy()
    altered.iloc[cutoff:] *= 1.5
    altered_with_future = run_backtest(altered, flat_weights, config=config)

    pd.testing.assert_series_equal(
        baseline.equity.iloc[:cutoff],
        altered_with_future.equity.iloc[:cutoff],
        rtol=0,
        atol=0,
    )
    baseline_trades = baseline.trades[baseline.trades["date"] < altered.index[cutoff]]
    altered_trades = altered_with_future.trades[
        altered_with_future.trades["date"] < altered.index[cutoff]
    ]
    pd.testing.assert_frame_equal(baseline_trades, altered_trades)


# 4. 首个 bar 不成交
def test_no_trade_on_the_first_bar(hand_prices):
    weights = pd.DataFrame(1.0, index=hand_prices.index, columns=hand_prices.columns)
    config = BacktestConfig(costs=ZERO_COST)

    result = run_backtest(hand_prices, weights, config=config)

    assert result.trades["date"].min() == hand_prices.index[1]
    assert result.equity.iloc[0] == pytest.approx(config.initial_cash)


def test_execution_lag_delays_every_fill(hand_prices):
    weights = pd.DataFrame(0.0, index=hand_prices.index, columns=hand_prices.columns)
    weights.iloc[0, 0] = 1.0  # decided on bar 0 only
    config = BacktestConfig(costs=ZERO_COST, execution=ExecutionConfig(lag=2))

    result = run_backtest(hand_prices, weights, config=config)

    assert result.trades["date"].iloc[0] == hand_prices.index[2]


# 5. 双账本一致
def test_share_and_weight_accounting_agree_at_zero_cost(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=10, slow_window=30)
    shares_config = BacktestConfig(costs=ZERO_COST, accounting="shares")
    weights_config = BacktestConfig(costs=ZERO_COST, accounting="weights")

    from quant_research.portfolio import signal_to_weights

    weights = signal_to_weights(signal, method="equal_weight", target_exposure=1.0)
    by_shares = run_backtest(sample_prices, weights, config=shares_config)
    by_weights = run_backtest(sample_prices, weights, config=weights_config)

    pd.testing.assert_series_equal(by_shares.equity, by_weights.equity, rtol=1e-9)


# 6. 多空开关
def test_short_weights_are_rejected_when_disabled(hand_prices):
    weights = pd.DataFrame(-1.0, index=hand_prices.index, columns=hand_prices.columns)
    config = BacktestConfig(costs=ZERO_COST, allow_short=False)

    result = run_backtest(hand_prices, weights, config=config)

    assert result.weights.min().min() >= -1e-12
    assert result.equity.iloc[-1] == pytest.approx(config.initial_cash)


# 7. 现金不为负
def test_cash_never_goes_negative_with_clipping(sample_prices):
    signal = moving_average_signal(sample_prices, fast_window=5, slow_window=15)
    config = BacktestConfig(
        costs=CostConfig(commission_bps=10.0, slippage_bps=5.0),
        execution=ExecutionConfig(integer_shares=True, lot_size=100, clip_to_cash=True),
    )

    result = run_signal_backtest(sample_prices, signal, weight_method="equal_weight", config=config)

    assert result.cash is not None
    assert result.cash.min() >= -1e-9
    assert (result.positions % 100 == 0).all().all()


# 8. 换手可控
def test_turnover_matches_signal_flip_frequency(sample_prices):
    prices = sample_prices[["ALPHA"]].copy()
    block = np.repeat([1.0, 0.0], 10)
    signal = pd.DataFrame(
        np.tile(block, len(prices) // 20 + 1)[: len(prices)],
        index=prices.index,
        columns=["ALPHA"],
    )
    config = BacktestConfig(costs=ZERO_COST)

    result = run_signal_backtest(prices, signal, config=config)

    expected = float(np.abs(np.diff(signal["ALPHA"].to_numpy(), prepend=0.0)).sum()) / len(prices)
    assert result.turnover.mean() == pytest.approx(expected, rel=1e-6)
    assert result.turnover.mean() < 0.2


# 9. 缺失价格
def test_missing_price_blocks_trading_for_that_symbol(sample_prices):
    prices = sample_prices.copy()
    gap_date = prices.index[30]
    prices.loc[gap_date, "ALPHA"] = np.nan
    weights = _fully_invested(prices)
    config = BacktestConfig(costs=ZERO_COST)

    result = run_backtest(prices, weights, config=config)

    traded = result.trades[result.trades["date"] == gap_date]
    assert "ALPHA" not in set(traded["symbol"])
    assert any(kind == "skipped_symbol" for kind, _, _ in result.warnings)


# 10. 空信号
def test_flat_signal_keeps_equity_at_initial_cash(sample_prices):
    signal = pd.DataFrame(0.0, index=sample_prices.index, columns=sample_prices.columns)
    config = BacktestConfig(costs=CostConfig(commission_bps=5.0, slippage_bps=2.0))

    result = run_signal_backtest(sample_prices, signal, config=config)

    assert result.trades.empty
    assert result.final_equity == pytest.approx(config.initial_cash)
    assert result.turnover.sum() == 0.0


def test_open_execution_requires_a_price_panel(sample_prices, flat_weights):
    config = BacktestConfig(costs=ZERO_COST, execution=ExecutionConfig(price="open"))

    with pytest.raises(ValueError, match="exec_prices"):
        run_backtest(sample_prices, flat_weights, config=config)


def test_open_execution_uses_the_supplied_panel(sample_prices, flat_weights):
    open_prices = sample_prices * 0.99
    config = BacktestConfig(costs=ZERO_COST, execution=ExecutionConfig(price="open"))

    result = run_backtest(sample_prices, flat_weights, exec_prices=open_prices, config=config)

    first_fill_bar = sample_prices.index[1]
    assert result.trades.iloc[0]["price"] == pytest.approx(
        float(open_prices.loc[first_fill_bar, "ALPHA"])
    )
    assert result.final_equity != pytest.approx(100_000.0)


def test_result_summary_exposes_metric_table(sample_prices, flat_weights):
    config = BacktestConfig(costs=CostConfig(commission_bps=5.0, slippage_bps=2.0))

    summary = run_backtest(sample_prices, flat_weights, config=config).summary()

    assert "sharpe" in summary.index
    assert "max_drawdown" in summary.index
    assert summary["cagr"] == summary["cagr"]  # not NaN


def test_run_backtest_rejects_empty_prices(flat_weights):
    with pytest.raises(ValueError, match="non-empty"):
        run_backtest(pd.DataFrame(), flat_weights)
