"""Configuration objects for the backtest engine.

Every assumption that materially changes a result lives here: capital, costs,
execution price, execution lag and share rounding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from quant_research.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quant_research.config import BacktestSettings, Settings

logger = get_logger("backtest.config")


@dataclass(frozen=True)
class CostConfig:
    """Transaction costs, in basis points of traded notional."""

    commission_bps: float = 5.0
    slippage_bps: float = 2.0
    min_commission: float = 0.0

    def __post_init__(self) -> None:
        if self.commission_bps < 0:
            raise ValueError(f"commission_bps must be non-negative, got {self.commission_bps}")
        if self.slippage_bps < 0:
            raise ValueError(f"slippage_bps must be non-negative, got {self.slippage_bps}")
        if self.min_commission < 0:
            raise ValueError(f"min_commission must be non-negative, got {self.min_commission}")

    @property
    def total_bps(self) -> float:
        return self.commission_bps + self.slippage_bps


@dataclass(frozen=True)
class ExecutionConfig:
    """When and how orders are filled.

    ``price="close"`` means the order decided after bar *t-1*'s close is filled
    at that same close — the standard daily-backtest assumption, free of
    look-ahead. ``open`` / ``vwap`` fill during bar *t* and require the
    corresponding price panel to be supplied to the engine.
    """

    price: Literal["close", "open", "vwap"] = "close"
    lag: int = 1
    sizing: Literal["exec_price", "prev_close"] = "exec_price"
    integer_shares: bool = False
    lot_size: int = 1
    clip_to_cash: bool = True

    def __post_init__(self) -> None:
        if self.price not in ("close", "open", "vwap"):
            raise ValueError(f"price must be 'close', 'open' or 'vwap', got {self.price!r}")
        if self.lag < 1:
            raise ValueError(f"lag must be at least 1, got {self.lag}")
        if self.sizing not in ("exec_price", "prev_close"):
            raise ValueError(f"sizing must be 'exec_price' or 'prev_close', got {self.sizing!r}")
        if self.lot_size < 1:
            raise ValueError(f"lot_size must be positive, got {self.lot_size}")


@dataclass(frozen=True)
class RebalancePolicy:
    """Controls that suppress unnecessary trading.

    The default values (``0``) mean "trade on every weight change". Both controls
    need the live book, so they are engine-level rather than portfolio-level.

    Attributes:
        buffer: skip a name when the target weight differs from its current weight
            by this fraction or less. This is the standard cure for the small,
            price-drift-driven trades a daily-rebalanced book would otherwise place.
        min_trade_notional: skip orders whose traded notional is below this amount.
    """

    buffer: float = 0.0
    min_trade_notional: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.buffer < 1.0:
            raise ValueError(f"buffer must be in [0, 1), got {self.buffer}")
        if self.min_trade_notional < 0:
            raise ValueError(
                f"min_trade_notional must be non-negative, got {self.min_trade_notional}"
            )


@dataclass(frozen=True)
class BacktestConfig:
    """Full engine configuration."""

    initial_cash: float = 100_000.0
    costs: CostConfig = field(default_factory=CostConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    rebalance: RebalancePolicy = field(default_factory=RebalancePolicy)
    allow_short: bool = False
    accounting: Literal["shares", "weights"] = "shares"

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError(f"initial_cash must be positive, got {self.initial_cash}")
        if self.accounting not in ("shares", "weights"):
            raise ValueError(f"accounting must be 'shares' or 'weights', got {self.accounting!r}")
        if self.accounting == "weights" and (
            self.rebalance.buffer > 0 or self.rebalance.min_trade_notional > 0
        ):
            logger.warning(
                "rebalance controls need the live book; they are ignored in "
                "accounting='weights' because it is a vectorised approximation"
            )

    @classmethod
    def from_settings(
        cls, settings: Settings | BacktestSettings, **overrides: object
    ) -> BacktestConfig:
        """Build an engine config from ``config/settings.yaml`` values."""
        backtest = cast("BacktestSettings", getattr(settings, "backtest", settings))
        kwargs: dict[str, object] = {
            "initial_cash": backtest.initial_cash,
            "costs": CostConfig(
                commission_bps=backtest.commission_bps,
                slippage_bps=backtest.slippage_bps,
            ),
            "allow_short": backtest.allow_short,
        }
        kwargs.update(overrides)
        return cls(**kwargs)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {
            "initial_cash": self.initial_cash,
            "costs": {
                "commission_bps": self.costs.commission_bps,
                "slippage_bps": self.costs.slippage_bps,
                "min_commission": self.costs.min_commission,
            },
            "execution": {
                "price": self.execution.price,
                "lag": self.execution.lag,
                "sizing": self.execution.sizing,
                "integer_shares": self.execution.integer_shares,
                "lot_size": self.execution.lot_size,
                "clip_to_cash": self.execution.clip_to_cash,
            },
            "rebalance": {
                "buffer": self.rebalance.buffer,
                "min_trade_notional": self.rebalance.min_trade_notional,
            },
            "allow_short": self.allow_short,
            "accounting": self.accounting,
        }


__all__ = ["BacktestConfig", "CostConfig", "ExecutionConfig", "RebalancePolicy"]
