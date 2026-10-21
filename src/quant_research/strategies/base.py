"""The signal contract, the strategy registry and the lag helper.

Conventions (see ``docs/ARCHITECTURE.md``):

- A signal function takes prices and returns a ``date x symbol`` frame of raw
  scores. It is **not** lagged; the backtest engine applies the lag exactly once.
- A signal is registered so that adding a strategy never touches the main flow.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

SignalFn = Callable[..., pd.DataFrame]

STRATEGY_REGISTRY: dict[str, SignalFn] = {}


def register(name: str) -> Callable[[SignalFn], SignalFn]:
    """Register a signal factory under ``name``."""

    def decorator(function: SignalFn) -> SignalFn:
        if name in STRATEGY_REGISTRY:
            raise ValueError(f"Strategy {name!r} is already registered")
        STRATEGY_REGISTRY[name] = function
        return function

    return decorator


def get_signal(name: str) -> SignalFn:
    """Return a registered signal factory."""
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(STRATEGY_REGISTRY)) or "<empty>"
        raise KeyError(f"Unknown strategy {name!r}. Available strategies: {available}") from None


def apply_lag(frame: pd.DataFrame | pd.Series, periods: int = 1):
    """Shift a signal/weight frame so it can only be traded on the next bar.

    Exposed for external use, but the engine performs the shift internally —
    callers must not have to remember it.
    """
    if periods < 0:
        raise ValueError(f"periods must be non-negative, got {periods}")
    return frame.shift(periods)


__all__ = ["STRATEGY_REGISTRY", "SignalFn", "apply_lag", "get_signal", "register"]
