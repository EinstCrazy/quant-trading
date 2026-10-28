"""Provider registry.

Keeping construction behind a name means the collector never imports a source
module directly: adding ``tushare`` or ``baostock`` later is one branch here.
"""

from __future__ import annotations

from typing import Any

from quant_research.data.providers.base import (
    STANDARD_COLUMNS,
    MarketDataProvider,
    ProviderError,
    ProviderUnavailable,
    normalise_bars,
    normalize_symbol,
    to_bare_symbol,
)
from quant_research.data.providers.mock import MockProvider

AVAILABLE_PROVIDERS: tuple[str, ...] = ("akshare", "baostock", "sse", "mock")


def get_provider(name: str, **kwargs: Any) -> MarketDataProvider:
    """Build a provider by name; each source module is only imported on request."""
    key = name.strip().lower()
    if key == "mock":
        return MockProvider(**kwargs)
    if key == "akshare":
        from quant_research.data.providers.akshare_provider import AkshareProvider

        return AkshareProvider(**kwargs)
    if key == "baostock":
        from quant_research.data.providers.baostock_provider import BaostockProvider

        return BaostockProvider(**kwargs)
    if key == "sse":
        from quant_research.data.providers.sse_provider import SseProvider

        return SseProvider(**kwargs)
    raise ValueError(f"unknown provider {name!r}; available: {list(AVAILABLE_PROVIDERS)}")


__all__ = [
    "AVAILABLE_PROVIDERS",
    "STANDARD_COLUMNS",
    "MarketDataProvider",
    "MockProvider",
    "ProviderError",
    "ProviderUnavailable",
    "get_provider",
    "normalise_bars",
    "normalize_symbol",
    "to_bare_symbol",
]
