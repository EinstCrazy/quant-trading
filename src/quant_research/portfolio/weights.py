"""Turn signal scores into target portfolio weights.

The first version is deliberately simple — fixed exposure for single-symbol
strategies and equal weight across symbols with a signal. Position sizing and
constraint solving are out of scope until the minimal backtest loop is trusted.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from quant_research.logging_setup import get_logger

logger = get_logger("portfolio")

WeightMethod = Literal["fixed", "equal_weight"]


def _as_frame(signal: pd.Series | pd.DataFrame) -> pd.DataFrame:
    if isinstance(signal, pd.Series):
        return signal.to_frame(signal.name or "asset")
    return signal


def signal_to_weights(
    signal: pd.Series | pd.DataFrame,
    *,
    method: WeightMethod = "fixed",
    target_exposure: float = 1.0,
    allow_short: bool = False,
) -> pd.DataFrame:
    """Convert raw signal scores into target weights (one row per decision date).

    ``fixed`` maps any positive score to ``target_exposure`` (suited to 0/1
    signals such as the moving-average cross). ``equal_weight`` spreads
    ``target_exposure`` evenly across the symbols holding a positive score on
    each date. Negative scores are dropped unless ``allow_short`` is enabled.
    """
    if method not in ("fixed", "equal_weight"):
        raise ValueError(f"method must be 'fixed' or 'equal_weight', got {method!r}")
    if target_exposure < 0:
        raise ValueError(f"target_exposure must be non-negative, got {target_exposure}")

    frame = _as_frame(signal).astype("float64")
    active = frame > 0

    if method == "fixed":
        weights = active.astype("float64") * target_exposure
    else:
        counts = active.sum(axis=1).replace(0, pd.NA)
        weights = active.div(counts, axis=0).fillna(0.0).astype("float64") * target_exposure

    if not allow_short:
        negative = (frame < 0).any(axis=1)
        if bool(negative.any()):
            logger.warning(
                "dropped negative signal scores on %d date(s); allow_short is disabled",
                int(negative.sum()),
            )
        weights = weights.clip(lower=0.0)

    weights.index.name = signal.index.name
    return weights


__all__ = ["signal_to_weights"]
