"""Daily backtest engine: from target weights to an equity curve.

Timing contract (the single source of truth for this project):

- after bar *t*'s close the signal produces ``target_weights[t]``;
- the order is filled during bar ``t + lag`` (at that bar's open/vwap, or at
  bar *t*'s close when ``price="close"``);
- the position is valued at each bar's close, giving ``equity[t]``.

The shift lives **only** in :func:`run_backtest`; callers pass unlagged target
weights and are not allowed to shift on their own.

Two accounting modes are provided. ``shares`` is authoritative (it produces
cash, positions and a trade blotter); ``weights`` is a vectorised approximation
used for fast screening and as an independent cross-check — under zero costs the
two must agree point by point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant_research.backtest.config import BacktestConfig
from quant_research.backtest.costs import commission, execution_price, slippage_cost
from quant_research.backtest.ledger import Ledger
from quant_research.data.loader import DataMeta
from quant_research.logging_setup import get_logger
from quant_research.portfolio import PortfolioPolicy, signal_to_weights

logger = get_logger("backtest")

TRADE_COLUMNS = [
    "date",
    "symbol",
    "shares",
    "price",
    "fill_price",
    "gross",
    "commission",
    "slippage",
    "cost",
    "reason",
]
_EPS = 1e-12


@dataclass
class BacktestResult:
    """Everything a run produces, ready for analysis and archiving."""

    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    turnover: pd.Series
    costs: pd.DataFrame
    config: BacktestConfig
    cash: pd.Series | None = None
    meta: DataMeta | None = None
    warnings: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else self.config.initial_cash

    @property
    def total_costs(self) -> float:
        return float(self.costs["total"].sum()) if len(self.costs) else 0.0

    def summary(self, benchmark: pd.Series | None = None, periods_per_year: int = 252) -> pd.Series:
        """Delegate to :func:`quant_research.metrics.summarize`."""
        from quant_research.metrics import summarize

        return summarize(self.returns, benchmark=benchmark, periods_per_year=periods_per_year)


def _align(prices: pd.DataFrame, target_weights: pd.DataFrame) -> pd.DataFrame:
    weights = target_weights.reindex(index=prices.index, columns=prices.columns)
    missing_symbols = [column for column in prices.columns if column not in target_weights.columns]
    if missing_symbols:
        logger.warning("no target weights supplied for %s; treated as flat", missing_symbols)
    return weights.fillna(0.0).astype("float64")


def _execution_prices(
    prices: pd.DataFrame,
    exec_prices: pd.DataFrame | None,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Fill prices for each bar.

    ``close`` uses the *previous* bar's close: the order decided after that close
    is filled at it, so bar *t*'s return belongs to the position — this is what
    keeps the share ledger and the weight ledger numerically consistent.
    """
    if config.execution.price == "close":
        return prices.shift(config.execution.lag)

    if exec_prices is None:
        raise ValueError(
            f"execution.price={config.execution.price!r} requires an 'exec_prices' panel "
            "(e.g. panel.field('open')); pass it to run_backtest()."
        )
    return exec_prices.reindex(index=prices.index, columns=prices.columns).astype("float64")


def _sizing_prices(
    prices: pd.DataFrame, exec_prices: pd.DataFrame, config: BacktestConfig
) -> pd.DataFrame:
    if config.execution.sizing == "prev_close":
        return prices.shift(1)
    return exec_prices


def _round_to_lot(shares: float, lot_size: int) -> float:
    return math.floor(shares / lot_size) * lot_size


def _run_shares(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    exec_prices: pd.DataFrame,
    config: BacktestConfig,
    warnings: list[tuple[str, str, str]],
) -> tuple[BacktestResult, pd.Series]:
    index = prices.index
    symbols = tuple(str(column) for column in prices.columns)
    n_bars = len(index)

    valuation_values = prices.ffill().to_numpy(dtype="float64")
    exec_values = exec_prices.to_numpy(dtype="float64")
    sizing_values = _sizing_prices(prices, exec_prices, config).to_numpy(dtype="float64")
    target_values = weights.shift(config.execution.lag).fillna(0.0).to_numpy(dtype="float64")

    ledger = Ledger(symbols, config.initial_cash)
    buffer = config.rebalance.buffer
    min_notional = config.rebalance.min_trade_notional
    equity = np.zeros(n_bars, dtype="float64")
    cash_history = np.zeros(n_bars, dtype="float64")
    turnover = np.zeros(n_bars, dtype="float64")
    weights_history = np.zeros((n_bars, len(symbols)), dtype="float64")
    positions_history = np.zeros((n_bars, len(symbols)), dtype="float64")
    records: list[tuple[object, ...]] = []

    for bar in range(n_bars):
        if bar > 0:
            equity_previous = ledger.equity(valuation_values[bar - 1])
            traded_notional = 0.0
            orders: list[tuple[float, int, str, float]] = []

            for position, symbol in enumerate(symbols):
                weight = target_values[bar][position]
                if np.isnan(weight):
                    continue

                price = exec_values[bar][position]
                if np.isnan(price) or price <= 0:
                    if weight != 0 or ledger.shares[position] != 0:
                        warnings.append(("skipped_symbol", str(index[bar].date()), symbol))
                    continue

                size_price = sizing_values[bar][position]
                if np.isnan(size_price) or size_price <= 0:
                    size_price = price

                # Rebalance buffer: ignore differences too small to matter. Comparing
                # the target against the *current* weight (which drifts with price)
                # rather than against the previous target is what actually removes
                # the daily drip of trivial top-up trades.
                if buffer > 0 and equity_previous > _EPS:
                    previous_price = valuation_values[bar - 1][position]
                    if np.isfinite(previous_price) and previous_price > 0:
                        held_weight = ledger.shares[position] * previous_price / equity_previous
                        if abs(weight - held_weight) <= buffer:
                            continue

                target_shares = weight * equity_previous / size_price
                if target_shares > ledger.shares[position]:
                    # Reserve the round-trip cost inside the buy budget, otherwise a
                    # fully invested book would be clipped to its last cent every day.
                    unit_cost = size_price * (1 + config.costs.slippage_bps / 1e4)
                    unit_cost *= 1 + config.costs.commission_bps / 1e4
                    target_shares = weight * equity_previous / unit_cost
                if not config.allow_short and target_shares < 0:
                    target_shares = 0.0
                delta = target_shares - ledger.shares[position]

                if config.execution.integer_shares:
                    lot = config.execution.lot_size
                    delta = (
                        _round_to_lot(ledger.shares[position] + delta, lot)
                        - ledger.shares[position]
                    )

                if abs(delta) < _EPS:
                    continue
                if abs(delta) * price < min_notional:
                    continue  # not worth the round-trip cost

                orders.append((delta, position, symbol, price))

            # Sells first: they release the cash the buys then consume, so a
            # fully invested book can still rebalance without being clipped.
            orders.sort(key=lambda order: order[0])

            for delta, position, symbol, price in orders:
                reason = "rebalance"
                fill = execution_price(price, delta, config.costs)
                trade_commission = commission(delta, fill, config.costs)
                trade_slippage = slippage_cost(delta, price, config.costs)
                impact = delta * fill + trade_commission

                needs_clip = config.execution.clip_to_cash and impact > ledger.cash + _EPS
                if needs_clip and delta > 0:
                    requested = delta
                    unit_cost = price * (1 + config.costs.slippage_bps / 1e4)
                    unit_cost *= 1 + config.costs.commission_bps / 1e4
                    affordable = max(ledger.cash, 0.0) / unit_cost
                    delta = min(delta, affordable)
                    if config.execution.integer_shares:
                        lot = config.execution.lot_size
                        delta = _round_to_lot(ledger.shares[position] + delta, lot)
                        delta -= ledger.shares[position]
                    fill = execution_price(price, delta, config.costs)
                    trade_commission = commission(delta, fill, config.costs)
                    trade_slippage = slippage_cost(delta, price, config.costs)
                    impact = delta * fill + trade_commission
                    reason = "cash_clipped"
                    # A fully invested book can never pay for drift corrections; only
                    # surface clips that are material relative to total equity.
                    clipped_notional = (requested - delta) * price
                    if clipped_notional > 0.005 * equity_previous:
                        warnings.append(("cash_clipped", str(index[bar].date()), symbol))

                if abs(delta) < _EPS:
                    continue

                ledger.cash -= impact
                ledger.shares[position] += delta
                traded_notional += abs(delta) * price
                records.append(
                    (
                        index[bar],
                        symbol,
                        delta,
                        price,
                        fill,
                        delta * price,
                        trade_commission,
                        trade_slippage,
                        trade_commission + trade_slippage,
                        reason,
                    )
                )

            turnover[bar] = traded_notional / equity_previous if equity_previous > 0 else 0.0

        equity[bar] = ledger.equity(valuation_values[bar])
        cash_history[bar] = ledger.cash
        weights_history[bar] = ledger.weights(valuation_values[bar])
        positions_history[bar] = ledger.shares

    returns = np.zeros(n_bars, dtype="float64")
    if n_bars > 1:
        returns[1:] = equity[1:] / np.where(equity[:-1] == 0, np.nan, equity[:-1]) - 1
    returns = np.nan_to_num(returns, nan=0.0)

    trades = pd.DataFrame(records, columns=TRADE_COLUMNS)
    daily_costs = (
        trades.assign(commission=trades["commission"], slippage=trades["slippage"])
        .groupby("date")[["commission", "slippage"]]
        .sum()
        .reindex(index)
        .fillna(0.0)
        if len(trades)
        else pd.DataFrame(0.0, index=index, columns=["commission", "slippage"])
    )
    daily_costs["total"] = daily_costs["commission"] + daily_costs["slippage"]

    return (
        BacktestResult(
            equity=pd.Series(equity, index=index, name="equity"),
            returns=pd.Series(returns, index=index, name="return"),
            weights=pd.DataFrame(weights_history, index=index, columns=list(symbols)),
            positions=pd.DataFrame(positions_history, index=index, columns=list(symbols)),
            trades=trades,
            turnover=pd.Series(turnover, index=index, name="turnover"),
            costs=daily_costs,
            config=config,
        ),
        pd.Series(cash_history, index=index, name="cash"),
    )


def _run_weights(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    exec_prices: pd.DataFrame,
    config: BacktestConfig,
) -> BacktestResult:
    """Vectorised approximation: weighted returns minus turnover costs."""
    exec_weights = weights.shift(config.execution.lag).fillna(0.0)

    if config.execution.price == "close":
        asset_returns = prices.pct_change()
    else:
        asset_returns = prices / exec_prices - 1

    gross = (exec_weights * asset_returns.fillna(0.0)).sum(axis=1).fillna(0.0)
    turnover = exec_weights.diff().fillna(exec_weights).abs().sum(axis=1).fillna(0.0)
    cost_rate = turnover * config.costs.total_bps / 1e4
    net = (gross - cost_rate).fillna(0.0)

    equity = config.initial_cash * (1 + net).cumprod()
    returns = net.copy()
    returns.name = "return"

    daily_costs = pd.DataFrame(
        {
            "commission": turnover
            * config.costs.commission_bps
            / 1e4
            * equity.shift(1).fillna(config.initial_cash),
            "slippage": turnover
            * config.costs.slippage_bps
            / 1e4
            * equity.shift(1).fillna(config.initial_cash),
        },
        index=prices.index,
    )
    daily_costs["total"] = daily_costs["commission"] + daily_costs["slippage"]

    return BacktestResult(
        equity=equity.rename("equity"),
        returns=returns,
        weights=exec_weights,
        positions=pd.DataFrame(np.nan, index=prices.index, columns=list(prices.columns)),
        trades=pd.DataFrame(columns=TRADE_COLUMNS),
        turnover=turnover.rename("turnover"),
        costs=daily_costs,
        config=config,
    )


def run_backtest(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    *,
    exec_prices: pd.DataFrame | None = None,
    config: BacktestConfig | None = None,
    meta: DataMeta | None = None,
) -> BacktestResult:
    """Run a daily backtest from target weights.

    ``target_weights[t]`` is the position wanted *after* bar ``t``'s close; the
    engine fills it ``config.execution.lag`` bars later. ``exec_prices`` is
    required when ``execution.price`` is ``open`` or ``vwap``.
    """
    config = config or BacktestConfig()
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ValueError("prices must be a non-empty DataFrame")

    prices = prices.sort_index()
    weights = _align(prices, target_weights)

    if not config.allow_short and (weights < 0).any().any():
        negative_dates = int((weights < 0).any(axis=1).sum())
        logger.warning("clipped negative target weights on %d date(s)", negative_dates)
        weights = weights.clip(lower=0.0)

    fill_prices = _execution_prices(prices, exec_prices, config)

    if config.accounting == "weights":
        result = _run_weights(prices, weights, fill_prices, config)
    else:
        warnings: list[tuple[str, str, str]] = []
        result, cash = _run_shares(prices, weights, fill_prices, config, warnings)
        result.warnings = warnings
        result.cash = cash

    if meta is None:
        result.warnings.append(("missing_meta", "-", "-"))
    else:
        result.meta = meta
        if meta.start is None or meta.end is None:
            result.warnings.append(("incomplete_meta", "-", "-"))

    logger.info(
        "backtest finished: final equity %.2f, total costs %.2f, mean turnover %.4f",
        result.final_equity,
        result.total_costs,
        float(result.turnover.mean()),
    )
    return result


def run_signal_backtest(
    prices: pd.DataFrame,
    signal: pd.Series | pd.DataFrame,
    *,
    target_exposure: float = 1.0,
    weight_method: str = "fixed",
    risk: pd.Series | pd.DataFrame | None = None,
    policy: PortfolioPolicy | None = None,
    exec_prices: pd.DataFrame | None = None,
    config: BacktestConfig | None = None,
    meta: DataMeta | None = None,
) -> BacktestResult:
    """Signal → target weights → backtest, the path the engine wants you to use.

    ``policy`` applies portfolio limits during position sizing; ``risk`` feeds the
    ``inverse_volatility`` weighting (a per-symbol volatility or any other positive
    dispersion measure — smaller gets a larger weight). Rebalance controls live in
    ``config.rebalance`` because they need the live book.
    """
    config = config or BacktestConfig()
    allow_short = config.allow_short or (policy.allow_short if policy is not None else False)
    weights = signal_to_weights(
        signal,
        method=weight_method,  # type: ignore[arg-type]
        target_exposure=target_exposure,
        allow_short=allow_short,
        risk=risk,
        policy=policy,
    )
    if policy is not None:
        logger.info(
            "portfolio policy applied: %s",
            ", ".join(f"{k}={v}" for k, v in policy.to_dict().items()),
        )
    return run_backtest(prices, weights, exec_prices=exec_prices, config=config, meta=meta)


__all__ = ["BacktestResult", "run_backtest", "run_signal_backtest"]
