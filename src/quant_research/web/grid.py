"""Parameter sweeps: many configurations, one submission.

Trying twenty settings by hand means twenty clicks, twenty chances to mistype one,
and no record of which were tried. A grid turns that into a single submission that
runs every combination, files each one in the experiment ledger, and hands back a
table ranked by whichever metric matters.

This is a coarse filter, not an optimiser. Two parameters over a handful of values
will show which region is worth a closer look; it will not find an optimum, and
ranking a sweep by its own best result overstates that result. The sweep is here to
replace clicking, and to leave the evidence behind in the ledger.
"""

from __future__ import annotations

from itertools import product
from typing import Any

from .experiments import sort_key

# 200 backtests is minutes. 20 000 is a night, and the difference is one keystroke,
# so refuse the large grid instead of starting it.
MAX_COMBINATIONS = 200


def expand(params: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of every parameter's candidate values.

    Raises ValueError above the cap — before any work starts, not after.
    """
    keys = list(params)
    if not keys:
        return []  # nothing to sweep, rather than one run with no parameters
    combos = list(product(*(params[key] for key in keys)))
    if len(combos) > MAX_COMBINATIONS:
        raise ValueError(f"grid has {len(combos)} combinations, the limit is {MAX_COMBINATIONS}")
    return [dict(zip(keys, values)) for values in combos]


def rank(rows: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Best first. Rows where the metric could not be computed sink to the bottom."""
    return sorted(
        rows,
        key=lambda row: sort_key((row.get("metrics") or {}).get(metric)),
        reverse=True,
    )


__all__ = ["MAX_COMBINATIONS", "expand", "rank"]
