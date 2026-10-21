"""Random signal used as a control benchmark.

A strategy that cannot beat a random signal of comparable turnover has no
demonstrated edge. This signal exists purely to make that comparison cheap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_research.strategies.base import register


@register("random")
def random_signal(
    close: pd.Series | pd.DataFrame,
    probability: float = 0.5,
    seed: int = 0,
) -> pd.Series | pd.DataFrame:
    """Return an i.i.d. 0/1 signal with the given probability of being long."""
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability must be between 0 and 1, got {probability}")

    rng = np.random.default_rng(seed)
    draws = rng.random((len(close), 1 if isinstance(close, pd.Series) else close.shape[1]))
    frame = pd.DataFrame((draws < probability).astype("int8"), index=close.index)

    if isinstance(close, pd.Series):
        return frame.iloc[:, 0].rename("signal")
    return frame.set_axis(close.columns, axis=1)


__all__ = ["random_signal"]
