"""Signal scores to target portfolio weights.

Two concerns are kept apart on purpose:

- :func:`signal_to_weights` decides *how much of the book each name deserves*
  (fixed, equal or inverse-volatility weighting);
- :class:`PortfolioPolicy` decides *what is allowed* (per-name cap, leverage, net
  exposure) and is applied as a deterministic rescaling, never an optimiser.

Execution-level controls — rebalance buffer and minimum trade size — live in
:mod:`quant_research.backtest` because they need the live book to answer.
"""

from quant_research.portfolio.policy import PolicyResult, PortfolioPolicy, enforce_policy
from quant_research.portfolio.weights import (
    WeightMethod,
    WeightResult,
    signal_to_weights,
    signal_to_weights_with_policy,
)

__all__ = [
    "PolicyResult",
    "PortfolioPolicy",
    "WeightMethod",
    "WeightResult",
    "enforce_policy",
    "signal_to_weights",
    "signal_to_weights_with_policy",
]
