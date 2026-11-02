"""Turn a whole-market panel into a short list of names worth looking at.

The step that decides whether a screen can be acted on is not the ranking but
the filtering: a name that cannot be bought is not a pick, it is a line in a
report that will not survive contact with the opening auction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from quant_research.data.loader import PricePanel
from quant_research.portfolio.weights import signal_to_weights
from quant_research.scoring.factors import (
    combine,
    inverse_volatility,
    liquidity,
    ma_distance,
    momentum,
)

_DEFAULT_LIMIT_UP = 0.098  # A-share daily limit, less a tick for rounding

__all__ = ["ScreenResult", "screen", "to_target_weights"]


@dataclass(frozen=True)
class ScreenResult:
    """One day's short list, with enough context to be argued with."""

    as_of: pd.Timestamp
    picks: pd.DataFrame
    universe: int
    dropped: dict[str, int] = field(default_factory=dict)
    scored: pd.Series = field(default_factory=pd.Series)

    @property
    def symbols(self) -> list[str]:
        return [str(index) for index in self.picks.index]


def screen(
    panel: PricePanel,
    *,
    as_of: str | datetime | None = None,
    top_n: int = 20,
    min_history: int = 60,
    min_amount: float | None = None,
    limit_up: float = _DEFAULT_LIMIT_UP,
    positive_only: bool = True,
    factor_weights: dict[str, float] | None = None,
    momentum_window: int = 60,
    skip_recent: int = 5,
) -> ScreenResult:
    """Score the market as of one day and keep the top ``top_n`` buyable names.

    Filtering happens in this order, and each drop is counted: no score (not
    enough history to score at all), too short a history, too thin a turnover,
    limit-up-locked (``open == high == low`` at the daily limit, which means
    there will be nothing to buy tomorrow morning), and — because scores are
    daily z-scores — below the cross-sectional average, which is not a long
    candidate at all. A quiet day can legitimately return fewer than ``top_n``
    names: forcing the list to length would mean buying the least bad loser.
    """
    if top_n < 1:
        raise ValueError("top_n must be positive")

    prices = panel.prices.sort_index()
    if prices.empty:
        raise ValueError("cannot screen an empty panel")

    day = prices.index.max() if as_of is None else pd.Timestamp(as_of)
    if day not in prices.index:
        raise ValueError(f"{day.date()} is not a trading day in this panel")

    factors: dict[str, pd.DataFrame] = {
        "momentum": momentum(prices, window=momentum_window, skip_recent=skip_recent),
        "ma_distance": ma_distance(prices),
        "inverse_volatility": inverse_volatility(prices),
    }
    turnover = None
    if panel.has_field("amount"):
        turnover = liquidity(panel.field("amount"))
    elif panel.has_field("volume"):
        turnover = liquidity(panel.field("volume"))

    score = combine(factors, factor_weights)
    row = score.loc[day]
    dropped: dict[str, int] = {}

    # Count each rejection on the set it was judged against, then drop it.
    # Counting afterwards would measure the survivors and always report zero.
    #
    # History is judged first: the factors need history too, so a young name
    # would otherwise always be reported as "no score", which blames the score
    # for something the calendar decided.
    history = prices.loc[:day].count()
    short = ~(history.reindex(row.index).fillna(0) >= min_history)
    dropped["short_history"] = int(short.sum())
    usable = row[~short]

    scored_names = len(usable)
    usable = usable.dropna()
    dropped["no_score"] = int(scored_names - len(usable))

    if min_amount is not None and turnover is not None:
        thin = ~(turnover.loc[day].reindex(usable.index) >= min_amount).fillna(True)
        dropped["thin_turnover"] = int(thin.sum())
        usable = usable[~thin]

    if limit_up is not None and panel.has_field("open") and panel.has_field("low"):
        change = _daily_change(panel, prices, day).reindex(usable.index)
        same_price = (panel.field("open").loc[day] == panel.field("high").loc[day]) & (
            panel.field("high").loc[day] == panel.field("low").loc[day]
        )
        locked = (same_price & (change.fillna(0.0) >= limit_up)).reindex(usable.index)
        locked = locked.fillna(False)
        dropped["limit_up_locked"] = int(locked.sum())
        usable = usable[~locked]

    if positive_only:
        # scores are daily z-scores, so a negative one lost to the average name
        below = usable <= 0
        dropped["below_cross_sectional_mean"] = int(below.sum())
        usable = usable[~below]

    ranked = usable.sort_values(ascending=False).head(top_n)
    picks = pd.DataFrame(
        {
            "score": ranked.round(4),
            "rank": range(1, len(ranked) + 1),
            "momentum": factors["momentum"].loc[day].reindex(ranked.index).round(4),
            "ma_distance": factors["ma_distance"].loc[day].reindex(ranked.index).round(4),
            "inverse_volatility": factors["inverse_volatility"]
            .loc[day]
            .reindex(ranked.index)
            .round(4),
        }
    )
    if turnover is not None:
        picks["turnover"] = turnover.loc[day].reindex(ranked.index)
    picks.index.name = "symbol"

    return ScreenResult(
        as_of=day,
        picks=picks,
        universe=len(usable),
        dropped=dropped,
        scored=usable.sort_values(ascending=False),
    )


def _daily_change(panel: PricePanel, prices: pd.DataFrame, day: pd.Timestamp) -> pd.Series:
    """The day's return, from the vendor's own column when it kept one."""
    if panel.has_field("pct_change"):
        return panel.field("pct_change").loc[day] / 100.0
    return prices.pct_change().loc[day]


def to_target_weights(
    result: ScreenResult,
    *,
    method: str = "equal_weight",
    target_exposure: float = 1.0,
    risk: pd.Series | pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Hand the picks to the portfolio layer in the shape it already expects.

    Equal weight by default; ``inverse_volatility`` needs ``risk`` to be passed
    in, which the caller has to have computed on its own schedule.
    """
    frame = result.picks[["score"]].T
    frame.index = pd.DatetimeIndex([result.as_of], name="date")
    return signal_to_weights(
        frame,
        method=method,  # type: ignore[arg-type]
        target_exposure=target_exposure,
        allow_short=False,
        risk=risk,
    )
