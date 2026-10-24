"""Turn signal scores into target portfolio weights.

The first version only offered fixed and equal weights. It now also supports
inverse-volatility sizing and an optional :class:`PortfolioPolicy` that caps
per-name weight, total leverage and net exposure. Position sizing remains
deterministic and explainable — no optimiser, no covariance forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from quant_research.logging_setup import get_logger
from quant_research.portfolio.policy import PolicyResult, PortfolioPolicy, enforce_policy

logger = get_logger("portfolio")

WeightMethod = Literal["fixed", "equal_weight", "inverse_volatility"]


@dataclass(frozen=True)
class WeightResult:
    """Target weights and, when a policy was applied, its constraint statistics."""

    weights: pd.DataFrame
    policy_result: PolicyResult | None = None


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
    risk: pd.Series | pd.DataFrame | None = None,
    policy: PortfolioPolicy | None = None,
) -> pd.DataFrame:
    """Convert raw signal scores into target weights (one row per decision date).

    ``fixed`` maps any positive score to ``target_exposure`` (suited to 0/1
    signals such as the moving-average cross). ``equal_weight`` spreads
    ``target_exposure`` evenly across the symbols holding a positive score on each
    date. ``inverse_volatility`` splits ``target_exposure`` in proportion to
    ``1 / risk``, degrades to equal weight when ``risk`` is missing, and ignores
    non-positive risk values.

    Negative scores are dropped unless ``allow_short`` is enabled. Passing a
    ``policy`` additionally enforces per-name, gross and net limits; see
    :func:`quant_research.portfolio.enforce_policy`.
    """
    if method not in ("fixed", "equal_weight", "inverse_volatility"):
        raise ValueError(
            f"method must be 'fixed', 'equal_weight' or 'inverse_volatility', got {method!r}"
        )
    if target_exposure < 0:
        raise ValueError(f"target_exposure must be non-negative, got {target_exposure}")

    frame = _as_frame(signal).astype("float64")
    active = frame > 0

    if method == "fixed":
        weights = active.astype("float64") * target_exposure
    elif method == "equal_weight":
        counts = active.sum(axis=1).replace(0, pd.NA)
        weights = active.div(counts, axis=0).fillna(0.0).astype("float64") * target_exposure
    else:
        weights = _inverse_volatility_weights(frame, active, risk, target_exposure)

    if policy is not None and policy.allow_short:
        allow_short = True  # an explicit policy governs shorts
    if not allow_short:
        negative = (frame < 0).any(axis=1)
        if bool(negative.any()):
            logger.warning(
                "dropped negative signal scores on %d date(s); allow_short is disabled",
                int(negative.sum()),
            )
        weights = weights.clip(lower=0.0)

    weights.index.name = signal.index.name

    if policy is not None:
        weights = enforce_policy(weights, policy).weights
    return weights


def signal_to_weights_with_policy(
    signal: pd.Series | pd.DataFrame,
    *,
    method: WeightMethod = "fixed",
    target_exposure: float = 1.0,
    policy: PortfolioPolicy,
    risk: pd.Series | pd.DataFrame | None = None,
) -> WeightResult:
    """Same as :func:`signal_to_weights` but keeps the constraint statistics."""
    raw = signal_to_weights(
        signal,
        method=method,
        target_exposure=target_exposure,
        allow_short=policy.allow_short,
        risk=risk,
    )
    result = enforce_policy(raw, policy)
    return WeightResult(weights=result.weights, policy_result=result)


def _inverse_volatility_weights(
    frame: pd.DataFrame,
    active: pd.DataFrame,
    risk: pd.Series | pd.DataFrame | None,
    target_exposure: float,
) -> pd.DataFrame:
    """Split exposure by ``1 / risk``; fall back to equal weight without risk."""
    counts = active.sum(axis=1).replace(0, pd.NA)
    if risk is None:
        logger.warning("inverse_volatility needs a risk estimate; falling back to equal weight")
        return active.div(counts, axis=0).fillna(0.0).astype("float64") * target_exposure

    if isinstance(risk, pd.Series):
        risk_frame = risk.to_frame()
        risk_frame = risk_frame.reindex(columns=frame.columns)
    else:
        risk_frame = risk
    risk_frame = risk_frame.reindex(index=frame.index, columns=frame.columns).astype("float64")
    usable = risk_frame.where(risk_frame > 0)
    # Symbols without a usable risk estimate simply receive no allocation.
    inverse = (1.0 / usable).where(active, 0.0)
    totals = inverse.sum(axis=1).replace(0, pd.NA)
    return inverse.div(totals, axis=0).fillna(0.0).astype("float64") * target_exposure


__all__ = [
    "PolicyResult",
    "PortfolioPolicy",
    "WeightMethod",
    "WeightResult",
    "enforce_policy",
    "signal_to_weights",
    "signal_to_weights_with_policy",
]
