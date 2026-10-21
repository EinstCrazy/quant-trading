"""Performance and risk metrics for return series.

Unless stated otherwise, ratio metrics annualise with ``periods_per_year`` (252
for daily data) and assume a zero risk-free rate. Win rate and profit factor are
computed on *periodic* returns (not on round-trip trades), which keeps them
well defined before trade pairing is implemented.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def annualized_sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Compute the annualized Sharpe ratio, assuming zero risk-free return."""
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    volatility = returns.std(ddof=1)
    if pd.isna(volatility) or volatility == 0:
        return 0.0
    return float(returns.mean() / volatility * np.sqrt(periods_per_year))


def max_drawdown(returns: pd.Series) -> float:
    """Compute the most negative peak-to-trough drawdown from periodic returns."""
    if (returns <= -1).any():
        raise ValueError("Returns must be greater than -100%")
    wealth = (1 + returns).cumprod()
    drawdown = wealth / wealth.cummax() - 1
    return float(drawdown.min()) if not drawdown.empty else 0.0


def cumulative_return(returns: pd.Series) -> float:
    """Total compounded return over the sample."""
    if returns.empty:
        return 0.0
    return float((1 + returns).prod() - 1)


def cagr(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Compound annual growth rate implied by the sample length."""
    if returns.empty:
        return 0.0
    total = float((1 + returns).prod())
    years = len(returns) / periods_per_year
    if years <= 0 or total <= 0:
        return 0.0
    return float(total ** (1 / years) - 1)


def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
    """Annualised standard deviation of periodic returns."""
    if returns.empty:
        return 0.0
    return float(returns.std(ddof=1) * np.sqrt(periods_per_year))


def downside_deviation(
    returns: pd.Series, periods_per_year: int = 252, target: float = 0.0
) -> float:
    """Annualised deviation of returns below ``target``."""
    if returns.empty:
        return 0.0
    shortfall = (returns - target).clip(upper=0.0)
    return float(np.sqrt((shortfall**2).mean()) * np.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int = 252, target: float = 0.0) -> float:
    """Annualised excess return per unit of downside deviation."""
    downside = downside_deviation(returns, periods_per_year=periods_per_year, target=target)
    if downside == 0 or pd.isna(downside):
        return 0.0
    return float((returns.mean() - target) * periods_per_year / downside)


def calmar_ratio(returns: pd.Series, periods_per_year: int = 252) -> float:
    """CAGR divided by the absolute maximum drawdown."""
    drawdown = abs(max_drawdown(returns))
    growth = cagr(returns, periods_per_year=periods_per_year)
    if drawdown == 0:
        return 0.0
    return float(growth / drawdown)


def drawdown_series(returns: pd.Series) -> pd.Series:
    """Underwater curve: 0 at a new equity high, negative below it."""
    wealth = (1 + returns).cumprod()
    return wealth / wealth.cummax() - 1


def drawdown_table(returns: pd.Series, top: int = 5) -> pd.DataFrame:
    """Deepest drawdown episodes with their length and recovery time."""
    columns = ["start", "trough", "end", "depth", "length_days", "recovery_days", "recovered"]
    if returns.empty:
        return pd.DataFrame(columns=columns)

    drawdown = drawdown_series(returns)
    underwater = drawdown < -1e-12
    if not underwater.any():
        return pd.DataFrame(columns=columns)

    values = drawdown.to_numpy(dtype="float64")
    underwater_positions = np.flatnonzero(values < -1e-12)
    segments = np.split(
        underwater_positions, np.flatnonzero(np.diff(underwater_positions) != 1) + 1
    )

    rows = []
    for segment in segments:
        first, last = int(segment[0]), int(segment[-1])
        trough = min(segment.tolist(), key=lambda offset: values[offset])
        # The recovery day itself is no longer underwater, so look one bar ahead.
        recovery = last + 1 if last + 1 < len(values) and abs(values[last + 1]) <= 1e-12 else None
        rows.append(
            {
                "start": drawdown.index[first],
                "trough": drawdown.index[trough],
                "end": drawdown.index[last],
                "depth": float(values[trough]),
                "length_days": len(segment),
                "recovery_days": int((drawdown.index[recovery] - drawdown.index[trough]).days)
                if recovery is not None
                else pd.NA,
                "recovered": recovery is not None,
            }
        )

    table = pd.DataFrame(rows).sort_values("depth").head(top).reset_index(drop=True)
    return table


def win_rate(returns: pd.Series) -> float:
    """Share of periods with a strictly positive return."""
    if returns.empty:
        return 0.0
    return float((returns > 0).sum() / len(returns))


def profit_factor(returns: pd.Series) -> float:
    """Gross gains divided by gross losses over the sample."""
    losses = float(returns[returns < 0].sum())
    if losses == 0:
        return float("inf") if float(returns[returns > 0].sum()) > 0 else 0.0
    return float(returns[returns > 0].sum() / abs(losses))


def exposure(weights: pd.DataFrame) -> float:
    """Average gross exposure (sum of absolute weights)."""
    if weights.empty:
        return 0.0
    return float(weights.abs().sum(axis=1).mean())


def benchmark_buy_hold(prices: pd.DataFrame | pd.Series, symbol: str | None = None) -> pd.Series:
    """Equal-weighted buy-and-hold return series used as the default benchmark."""
    if isinstance(prices, pd.Series):
        frame = prices.to_frame()
    elif symbol is not None:
        frame = prices[[symbol]]
    else:
        frame = prices
    returns = frame.pct_change()
    return returns.mean(axis=1).fillna(0.0).rename("benchmark")


def relative_return(returns: pd.Series, benchmark: pd.Series) -> float:
    """Strategy cumulative return minus benchmark cumulative return."""
    return cumulative_return(returns) - cumulative_return(benchmark)


def alpha_beta(
    returns: pd.Series, benchmark: pd.Series, periods_per_year: int = 252
) -> tuple[float, float]:
    """Annualised alpha and market beta against ``benchmark``."""
    aligned_returns, aligned_benchmark = returns.align(benchmark, join="inner")
    if aligned_returns.empty:
        return 0.0, 0.0
    variance = aligned_benchmark.var(ddof=1)
    if pd.isna(variance) or variance == 0:
        return 0.0, 0.0
    beta = float(aligned_returns.cov(aligned_benchmark) / variance)
    alpha = float((aligned_returns.mean() - beta * aligned_benchmark.mean()) * periods_per_year)
    return alpha, beta


def tracking_error(returns: pd.Series, benchmark: pd.Series, periods_per_year: int = 252) -> float:
    """Annualised standard deviation of the active return."""
    aligned_returns, aligned_benchmark = returns.align(benchmark, join="inner")
    if aligned_returns.empty:
        return 0.0
    active = aligned_returns - aligned_benchmark
    return float(active.std(ddof=1) * np.sqrt(periods_per_year))


def information_ratio(
    returns: pd.Series, benchmark: pd.Series, periods_per_year: int = 252
) -> float:
    """Annualised active return per unit of tracking error."""
    aligned_returns, aligned_benchmark = returns.align(benchmark, join="inner")
    if aligned_returns.empty:
        return 0.0
    active = aligned_returns - aligned_benchmark
    deviation = active.std(ddof=1)
    if pd.isna(deviation) or deviation == 0:
        return 0.0
    return float(active.mean() / deviation * np.sqrt(periods_per_year))


def summarize(
    returns: pd.Series,
    benchmark: pd.Series | None = None,
    periods_per_year: int = 252,
) -> pd.Series:
    """One-row metric table answering 'is this worth another iteration'."""
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")

    stats = {
        "n_periods": len(returns),
        "cumulative_return": cumulative_return(returns),
        "cagr": cagr(returns, periods_per_year=periods_per_year),
        "annualized_volatility": annualized_volatility(returns, periods_per_year=periods_per_year),
        "sharpe": annualized_sharpe(returns, periods_per_year=periods_per_year),
        "sortino": sortino_ratio(returns, periods_per_year=periods_per_year),
        "calmar": calmar_ratio(returns, periods_per_year=periods_per_year),
        "max_drawdown": max_drawdown(returns),
        "win_rate": win_rate(returns),
        "profit_factor": profit_factor(returns),
    }

    if benchmark is not None:
        alpha, beta = alpha_beta(returns, benchmark, periods_per_year=periods_per_year)
        stats.update(
            {
                "benchmark_return": cumulative_return(benchmark),
                "relative_return": relative_return(returns, benchmark),
                "alpha": alpha,
                "beta": beta,
                "tracking_error": tracking_error(
                    returns, benchmark, periods_per_year=periods_per_year
                ),
                "information_ratio": information_ratio(
                    returns, benchmark, periods_per_year=periods_per_year
                ),
            }
        )

    return pd.Series(stats, name="metrics")


__all__ = [
    "alpha_beta",
    "annualized_sharpe",
    "annualized_volatility",
    "benchmark_buy_hold",
    "cagr",
    "calmar_ratio",
    "cumulative_return",
    "downside_deviation",
    "drawdown_series",
    "drawdown_table",
    "exposure",
    "information_ratio",
    "max_drawdown",
    "profit_factor",
    "relative_return",
    "sortino_ratio",
    "summarize",
    "tracking_error",
    "win_rate",
]
