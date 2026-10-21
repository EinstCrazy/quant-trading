"""Strategy signals: one file per family, registered in :data:`STRATEGY_REGISTRY`."""

from quant_research.strategies.base import (
    STRATEGY_REGISTRY,
    SignalFn,
    apply_lag,
    get_signal,
    register,
)
from quant_research.strategies.momentum import moving_average_signal
from quant_research.strategies.random import random_signal

__all__ = [
    "STRATEGY_REGISTRY",
    "SignalFn",
    "apply_lag",
    "get_signal",
    "moving_average_signal",
    "random_signal",
    "register",
]
