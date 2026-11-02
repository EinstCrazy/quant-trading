"""Score a whole market, then pick from it.

Built for the nightly loop: after the close, rank every name we have data for,
drop those that cannot be acted on, and hand back a short list. Nothing here
looks forward — see :mod:`quant_research.scoring.factors`.
"""

from __future__ import annotations

from quant_research.scoring.factors import (
    combine,
    inverse_volatility,
    liquidity,
    ma_distance,
    momentum,
    zscore_by_day,
)
from quant_research.scoring.screen import ScreenResult, screen, to_target_weights

__all__ = [
    "ScreenResult",
    "combine",
    "inverse_volatility",
    "liquidity",
    "ma_distance",
    "momentum",
    "screen",
    "to_target_weights",
    "zscore_by_day",
]
