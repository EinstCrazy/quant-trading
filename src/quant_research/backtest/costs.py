"""Pure cost functions: commission and slippage.

Kept free of state so the numbers can be checked against a hand calculation.
"""

from __future__ import annotations

import math

from quant_research.backtest.config import CostConfig

BPS = 10_000.0


def execution_price(price: float, delta_shares: float, costs: CostConfig) -> float:
    """Fill price after slippage: buys fill higher, sells fill lower."""
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    direction = math.copysign(1.0, delta_shares) if delta_shares else 0.0
    return price * (1.0 + direction * costs.slippage_bps / BPS)


def commission(delta_shares: float, fill_price: float, costs: CostConfig) -> float:
    """Commission charged on the traded notional (both sides)."""
    amount = abs(delta_shares) * fill_price * costs.commission_bps / BPS
    if amount <= 0:
        return 0.0
    return max(amount, costs.min_commission)


def slippage_cost(delta_shares: float, price: float, costs: CostConfig) -> float:
    """Slippage expressed as a cash amount (always a cost, either direction)."""
    return abs(delta_shares) * price * costs.slippage_bps / BPS


def cash_impact(delta_shares: float, price: float, costs: CostConfig) -> float:
    """Cash spent (positive) or received (negative) for a trade, costs included."""
    fill = execution_price(price, delta_shares, costs)
    return delta_shares * fill + commission(delta_shares, fill, costs)


__all__ = ["cash_impact", "commission", "execution_price", "slippage_cost"]
