"""Does a factor actually rank future returns?

Every other module in :mod:`quant_research.scoring` is built to look backwards.
This one looks forwards on purpose: an evaluation that refuses to peek at the
outcome is not an evaluation. ``forward_return`` is the only function in the
project allowed to use tomorrow's price, and it lives here, in a file whose name
says so, because an exception that is easy to reach is not an exception for long.

The numbers here are the only evidence a factor earns its weight. A factor with
no IC and no ordering is not a second opinion — it is a second way to say what
another factor already said.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_research.indicators import IndicatorParams
from quant_research.scoring.factors import (
    boll_position,
    inverse_volatility,
    ma_distance,
    macd_trend,
    momentum,
    rsi_reversal,
)

__all__ = [
    "IcSummary",
    "QuantileSpread",
    "available_factors",
    "compute_factor",
    "factor_ic",
    "forward_return",
    "ic_decay",
    "quantile_returns",
    "summarise_ic",
]


def forward_return(close: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    """Return over the next ``horizon`` sessions, starting from today's close.

    **Evaluation only.** Every factor in this package is required to use only
    data up to and including its own day; this function cannot do its job if it
    obeys that rule, so it does not belong anywhere near a signal.

    The last ``horizon`` rows are ``NaN`` — their outcome has not happened yet,
    and inventing a number for them is how a backtest quietly stops being one.
    """
    if horizon < 1:
        raise ValueError("horizon must be positive")
    return close.shift(-horizon) / close - 1.0


def factor_ic(factor: pd.DataFrame, forward: pd.DataFrame) -> pd.Series:
    """Daily cross-sectional rank correlation between score and forward return.

    Rank, not Pearson: in a thirty-name cross-section one 9% outlier would
    otherwise decide the whole day. Ranked first, then Pearson — that *is*
    Spearman, and it avoids pulling in scipy for a two-line calculation. The
    result is a series, not a number, because the interesting part is how
    unstable it is, not its average.
    """
    common = factor.index.intersection(forward.index)
    return factor.loc[common].rank(axis=1).corrwith(forward.loc[common].rank(axis=1), axis=1)


@dataclass(frozen=True)
class IcSummary:
    """One factor's IC, with the caveat attached where it is read."""

    days: int
    mean: float
    std: float
    positive_share: float
    t_stat: float
    # overlapping windows: n/horizon independent observations, not n
    t_stat_adjusted: float


def summarise_ic(ic: pd.Series, horizon: int = 1) -> IcSummary:
    """Mean IC and its t-statistic, honest about overlapping holding periods.

    A five-day holding period measured every day reuses four of its five days in
    the next observation, so ``n`` overstates the evidence by roughly a factor of
    ``horizon``. Dividing the t-statistic by ``sqrt(horizon)`` is crude, but a
    crude correction that is printed next to the optimistic number is worth more
    than a confident one that is not.
    """
    clean = ic.dropna()
    if len(clean) < 2:
        raise ValueError("need at least two days of IC to say anything")
    spread = float(clean.std(ddof=1))
    if not math.isfinite(spread) or spread == 0.0:
        raise ValueError("IC has no variation; a t-statistic would be invented")

    mean = float(clean.mean())
    t_stat = mean / (spread / math.sqrt(len(clean)))
    return IcSummary(
        days=len(clean),
        mean=mean,
        std=spread,
        positive_share=float((clean > 0).mean()),
        t_stat=t_stat,
        t_stat_adjusted=t_stat / math.sqrt(horizon),
    )


@dataclass(frozen=True)
class QuantileSpread:
    """Average forward return by score bucket, plus how well it orders them."""

    returns: pd.Series
    sizes: pd.Series
    # rank correlation between bucket number and bucket return: 1.0 is perfect
    monotonicity: float


def quantile_returns(
    factor: pd.DataFrame, forward: pd.DataFrame, quantiles: int = 5
) -> QuantileSpread:
    """Average forward return per bucket of the score, bucketed one day at a time.

    This says more than IC to most readers: an IC of 0.04 is hard to feel, while
    "the top fifth returned 0.31% and the bottom fifth −0.12% over five days" is
    the same fact in the unit people think in.

    Days are bucketed separately. Pooling the whole sample would put Monday's
    scores next to a year of drift and bucket by market condition instead of by
    score.
    """
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")

    common = factor.index.intersection(forward.index)
    collected: dict[int, list[float]] = {}
    sizes: dict[int, list[float]] = {}
    for day in common:
        row = pd.DataFrame({"score": factor.loc[day], "ahead": forward.loc[day]}).dropna()
        # two names per bucket, or a bucket average is just one name's return
        if len(row) < quantiles * 2:
            continue
        labels = pd.qcut(row["score"], quantiles, labels=False, duplicates="drop")
        means = row["ahead"].groupby(labels).mean()
        counts = row["ahead"].groupby(labels).size()
        for bucket, value in means.items():
            index = int(bucket)  # type: ignore[arg-type]
            collected.setdefault(index, []).append(float(value))
            sizes.setdefault(index, []).append(float(counts[index]))

    if len(collected) < 2:
        raise ValueError("no day had enough names to order into buckets")

    returns = pd.Series({b: float(np.mean(v)) for b, v in collected.items()}).sort_index()
    mean_sizes = pd.Series({b: float(np.mean(v)) for b, v in sizes.items()}).sort_index()
    order = pd.Series(np.arange(len(returns), dtype=float), index=returns.index)
    return QuantileSpread(
        returns=returns,
        sizes=mean_sizes,
        monotonicity=float(returns.rank().corr(order.rank())),
    )


def ic_decay(
    factor: pd.DataFrame, close: pd.DataFrame, horizons: tuple[int, ...] = (1, 5, 10, 20)
) -> pd.DataFrame:
    """IC at several holding periods.

    A factor that decays fast is for trading; one that holds up is for holding.
    Which one it is cannot be guessed from its formula.
    """
    rows: list[dict[str, float]] = []
    for horizon in horizons:
        summary = summarise_ic(factor_ic(factor, forward_return(close, horizon)), horizon=horizon)
        rows.append(
            {
                "horizon": float(horizon),
                "mean_ic": summary.mean,
                "t_stat": summary.t_stat,
                "t_adjusted": summary.t_stat_adjusted,
                "positive_share": summary.positive_share,
                "days": float(summary.days),
            }
        )
    return pd.DataFrame(rows).set_index("horizon")


# --- the factor registry ----------------------------------------------------
#
# One place that knows how to build each factor from a close panel, so the
# diagnostic and the screen cannot drift apart on what "momentum" means.

_FACTORS = {
    "momentum": lambda close, params: momentum(close, window=60, skip_recent=5),
    "ma_distance": lambda close, params: ma_distance(close),
    "inverse_volatility": lambda close, params: inverse_volatility(close),
    "rsi_reversal": lambda close, params: rsi_reversal(close, window=params.rsi_period),
    "macd_trend": lambda close, params: macd_trend(
        close, fast=params.macd_fast, slow=params.macd_slow, signal=params.macd_signal
    ),
    "boll_position": lambda close, params: boll_position(
        close, window=params.boll_period, width=params.boll_std
    ),
}


def available_factors() -> tuple[str, ...]:
    """Every factor the diagnostics know how to build."""
    return tuple(_FACTORS)


def compute_factor(
    name: str, close: pd.DataFrame, params: IndicatorParams | None = None
) -> pd.DataFrame:
    """Build one factor from a close panel."""
    if name not in _FACTORS:
        raise ValueError(f"unknown factor {name!r}; choose from {available_factors()}")
    return _FACTORS[name](close, params or IndicatorParams())
