"""Financial reports and the red flags read from them.

Two layers, kept apart on purpose: :mod:`.fetch` talks to BaoStock and knows
nothing about risk; :mod:`.risk` reads a wide table and knows nothing about the
network. Either can be tested without the other, and swapping the source does
not touch the rules.
"""

from quant_research.fundamentals.fetch import (
    ReportPanel,
    collect,
    load,
    merge,
    quarters_back,
    store,
)
from quant_research.fundamentals.risk import (
    FinancialRisk,
    RiskFlag,
    Snapshot,
    assess,
    assess_many,
    liability_ratio,
)

__all__ = [
    "FinancialRisk",
    "ReportPanel",
    "RiskFlag",
    "Snapshot",
    "assess",
    "assess_many",
    "collect",
    "liability_ratio",
    "load",
    "merge",
    "quarters_back",
    "store",
]
