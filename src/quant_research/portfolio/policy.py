"""Deterministic portfolio constraints.

This module is intentionally **not** an optimiser. It turns desired weights into
weights that satisfy simple, auditable limits by scaling them — no forecast, no
covariance, no numerical search. Two properties matter more than optimality:

1. **Deterministic** — the same inputs always produce the same weights.
2. **Explainable** — when a constraint binds, the caller can see which one and on
   how many dates, via the returned statistics dict.

Long-only and long/short books are handled separately because they ask different
questions: long-only only needs "how much in total and per name", while long/short
also needs "how much of the book is long versus short".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from quant_research.logging_setup import get_logger

logger = get_logger("portfolio")

_EPS = 1e-12
_MAX_ROUNDS = 16


@dataclass(frozen=True)
class PortfolioPolicy:
    """Limits applied to raw target weights.

    Attributes:
        allow_short: keep negative weights; otherwise they are dropped.
        max_weight: per-name cap on absolute weight.
        max_gross: hard cap on ``sum(|w|)`` — total leverage.
        target_net: desired ``sum(w)``. ``None`` leaves net exposure free.
        scaling: how per-name caps are resolved — ``"water_fill"`` redistributes
            the capped amount to the uncapped names (preserving total exposure),
            ``"proportional"`` scales the whole row down instead.
    """

    allow_short: bool = False
    max_weight: float = 1.0
    max_gross: float = 1.0
    target_net: float | None = None
    scaling: Literal["water_fill", "proportional"] = "water_fill"

    def __post_init__(self) -> None:
        if self.max_weight <= 0:
            raise ValueError(f"max_weight must be positive, got {self.max_weight}")
        if self.max_gross <= 0:
            raise ValueError(f"max_gross must be positive, got {self.max_gross}")
        if self.max_weight > self.max_gross + _EPS:
            raise ValueError(
                f"max_weight ({self.max_weight}) cannot exceed max_gross ({self.max_gross})"
            )
        if self.target_net is not None and abs(self.target_net) > self.max_gross + _EPS:
            raise ValueError(
                f"target_net ({self.target_net}) cannot exceed max_gross in absolute value "
                f"({self.max_gross})"
            )
        if self.scaling not in ("water_fill", "proportional"):
            raise ValueError(
                f"scaling must be 'water_fill' or 'proportional', got {self.scaling!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "allow_short": self.allow_short,
            "max_weight": self.max_weight,
            "max_gross": self.max_gross,
            "target_net": self.target_net,
            "scaling": self.scaling,
        }


@dataclass(frozen=True)
class PolicyResult:
    """Constrained weights plus a record of which limits bound."""

    weights: pd.DataFrame
    stats: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {"stats": dict(self.stats)}


def _water_fill(raw: pd.Series, max_weight: float, total: float) -> pd.Series:
    """Spread ``total`` across ``raw`` proportionally, capping each name.

    The amount a capped name cannot absorb is redistributed to the remaining
    names, preserving both their relative proportions and the requested total —
    unlike uniform scaling, which silently gives the exposure back to nobody.
    """
    result = pd.Series(0.0, index=raw.index)
    if total <= _EPS:
        return result

    free = raw[raw > _EPS]
    budget = total
    for _ in range(_MAX_ROUNDS):
        if free.empty or budget <= _EPS:
            break
        scale = budget / free.sum()
        alloc = free * scale
        capped = alloc > max_weight + _EPS
        if not capped.any():
            result.loc[alloc.index] = alloc
            return result
        result.loc[alloc.index[capped]] = max_weight
        budget -= max_weight * int(capped.sum())
        free = free[alloc[~capped].index]
    return result


def _cap_row(row: pd.Series, max_weight: float, scaling: str) -> pd.Series:
    """Apply the per-name cap to a single row of absolute weights."""
    if max_weight >= 1.0 - _EPS and scaling == "proportional":
        return row
    over = row > max_weight + _EPS
    if not over.any():
        return row
    if scaling == "proportional":
        return row * (max_weight / row.max())
    return _water_fill(row, max_weight, float(row.sum()))


def _target_leg_totals(gross: float, net: float | None) -> tuple[float, float]:
    """Split target gross/net into long and short leg sizes.

    With ``L = sum(w+)`` and ``S = sum(|w-|)`` we solve ``L + S = gross`` and
    ``L - S = net`` analytically, which preserves the relative mix inside each leg.
    """
    if net is None:
        return gross, gross
    net = float(np.clip(net, -gross, gross))
    return (gross + net) / 2.0, (gross - net) / 2.0


def _enforce_row(row: pd.Series, policy: PortfolioPolicy) -> pd.Series:
    long_raw = row.clip(lower=0.0)
    short_raw = (-row).clip(lower=0.0)
    if not policy.allow_short:
        short_raw = short_raw * 0.0

    if not policy.allow_short:
        total = policy.target_net if policy.target_net is not None else policy.max_gross
        desired = long_raw
        if desired.sum() <= _EPS:
            return pd.Series(0.0, index=row.index)
        # Stay within the hard gross cap even when a softer net target is given.
        desired = desired * (total / desired.sum())
        desired = desired * min(1.0, policy.max_gross / max(float(desired.sum()), _EPS))
        absolute = _cap_row(desired, policy.max_weight, policy.scaling)
        return absolute

    long_total, short_total = _target_leg_totals(policy.max_gross, policy.target_net)
    legs = []
    for raw, budget in ((long_raw, long_total), (short_raw, short_total)):
        if raw.sum() <= _EPS or budget <= _EPS:
            legs.append(pd.Series(0.0, index=row.index))
            continue
        scaled = raw * (budget / raw.sum())
        legs.append(_cap_row(scaled, policy.max_weight, policy.scaling))

    result = legs[0] - legs[1]
    return result


def enforce_policy(weights: pd.DataFrame, policy: PortfolioPolicy) -> PolicyResult:
    """Return weights satisfying ``policy``, plus per-constraint statistics.

    Constraints are applied row by row (each decision date is independent) and
    always (re)normalise to the policy's gross/net targets, so the result does not
    depend on the scale of the raw weights. The transformation is idempotent: a
    second pass over already-compliant weights is a no-op.
    """
    frame = weights.astype("float64")
    stats = {"short_dropped": 0, "weight_capped": 0, "gross_scaled": 0, "net_adjusted": 0}

    negatives = frame < -_EPS
    if bool(negatives.any().any()):
        stats["short_dropped"] = int(negatives.any(axis=1).sum())
        if not policy.allow_short:
            logger.warning(
                "policy dropped negative weights on %d date(s); allow_short is disabled",
                stats["short_dropped"],
            )

    raw_abs = frame.abs()
    if bool((raw_abs.max(axis=1) > policy.max_weight + _EPS).any()):
        stats["weight_capped"] = int((raw_abs.max(axis=1) > policy.max_weight + _EPS).sum())
    if bool((raw_abs.sum(axis=1) > policy.max_gross + _EPS).any()):
        stats["gross_scaled"] = int((raw_abs.sum(axis=1) > policy.max_gross + _EPS).sum())
    if policy.target_net is not None:
        off_target = (frame.sum(axis=1) - policy.target_net).abs() > 1e-6
        stats["net_adjusted"] = int(off_target.sum())

    constrained = frame.apply(lambda row: _enforce_row(row, policy), axis=1)
    constrained = constrained.reindex(index=frame.index, columns=frame.columns)
    constrained.index.name = frame.index.name
    return PolicyResult(weights=constrained.fillna(0.0), stats=stats)


__all__ = ["PolicyResult", "PortfolioPolicy", "enforce_policy"]
