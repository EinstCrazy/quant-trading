"""Financial red flags: which of these names is carrying risk the price does not say.

The point is not to build a credit model. It is to answer one narrow question
before a name enters a screen: *is there an obvious reason this company might
not be around, or might not be telling the truth, in a year?* Four numbers do
most of that work — leverage, liquidity, profit, and whether the profit arrived
as cash.

Two things this module is deliberately strict about:

- **No report is not low risk.** A name with no filings is scored ``未知``, not
  ``低``. The absence of evidence is the reason to look, not the reason to relax.
- **The data is checked against itself.** BaoStock's ``liabilityToAsset`` has
  come back off by a hundredfold (2025Q2 Moutai: 0.0015, while ``assetToEquity``
  on the same row implies 0.1475). The equity multiplier is a ratio of two
  numbers and does not slip that way, so when the two disagree badly the implied
  value wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from quant_research.fundamentals.fetch import ReportPanel

__all__ = [
    "FinancialRisk",
    "RiskFlag",
    "Snapshot",
    "assess",
    "assess_many",
    "liability_ratio",
]

# Leverage lines are calibrated for non-financial industrials. Banks, insurers
# and developers run 85–95% by design and will be flagged wrongly — without an
# industry field there is no honest way to fix that here, so it is written down
# instead of guessed around.
LEVERAGE_LIMIT = 0.70
LIQUIDITY_FLOOR = 1.00
CASH_CONVERSION_FLOOR = 0.30
PROFIT_COLLAPSE = -0.30
INTEREST_COVER_FLOOR = 1.50

LEVEL_HIGH = "高"
LEVEL_MEDIUM = "中"
LEVEL_LOW = "低"
LEVEL_UNKNOWN = "未知"


def _value(row: pd.Series | None, key: str) -> float | None:
    if row is None or key not in row.index:
        return None
    value = row[key]
    if value is None or pd.isna(value):
        return None
    return float(value)


def liability_ratio(row: pd.Series | None) -> float | None:
    """Debt as a share of assets, preferring the value implied by the equity multiplier.

    ``assetToEquity = assets / equity``, so ``1 - 1/multiplier`` is the leverage.
    It is derived from two large numbers that must be consistent, which makes it
    harder to be off by a factor of a hundred than a single pre-computed ratio.
    """
    direct = _value(row, "liabilityToAsset")
    multiplier = _value(row, "assetToEquity")

    implied: float | None = None
    if multiplier is not None and multiplier > 1e-9:
        implied = 1.0 - 1.0 / multiplier

    plausible = implied is not None and 0.0 <= implied <= 1.0
    disagrees = direct is None or not 0.0 <= direct <= 1.0 or abs(direct - implied) > 0.10
    if plausible and disagrees:
        return implied
    if direct is not None and 0.0 <= direct <= 1.0:
        return direct
    return None


@dataclass(frozen=True)
class Snapshot:
    """The handful of numbers the flags are built from, already cleaned."""

    period: str
    published: str
    net_profit: float | None
    net_margin: float | None
    gross_margin: float | None
    roe: float | None
    liability_to_asset: float | None
    current_ratio: float | None
    cfo_to_profit: float | None
    net_profit_yoy: float | None
    interest_cover: float | None


@dataclass(frozen=True)
class RiskFlag:
    """One reason to be careful, with the number that tripped it."""

    code: str
    label: str
    value: float | None
    threshold: str
    weight: int
    detail: str


@dataclass(frozen=True)
class FinancialRisk:
    """A name's red flags, plus the ones deliberately not counted."""

    symbol: str
    snapshot: Snapshot | None
    flags: tuple[RiskFlag, ...]
    waived: tuple[RiskFlag, ...]
    score: float
    level: str
    #: gross margin absent while leverage is extreme: a bank or an insurer,
    #: where deposits are the raw material rather than a warning sign
    likely_financial: bool

    @property
    def reason(self) -> str:
        """A single line a human can act on, waived rules included.

        A name with no scored flags but a waived one must not read as "无红旗"
        followed by "不计 …" — the waived rule did fire, it just did not count.
        """
        parts = [f"{flag.label}（{flag.threshold}）" for flag in self.flags] or ["无红旗"]
        if self.waived:
            parts.append(f"金融口径，不计 {self.waived_reason}")
        return "；".join(parts)

    @property
    def waived_reason(self) -> str:
        if not self.waived:
            return ""
        return "、".join(flag.label for flag in self.waived)


def _snapshot(symbol: str, row: pd.Series) -> Snapshot:
    period = str(row.name) if isinstance(row.name, str) else ""
    published = row.get("published")
    return Snapshot(
        period=period,
        published="" if published is None or pd.isna(published) else str(published)[:10],
        net_profit=_value(row, "netProfit"),
        net_margin=_value(row, "npMargin"),
        gross_margin=_value(row, "gpMargin"),
        roe=_value(row, "roeAvg"),
        liability_to_asset=liability_ratio(row),
        current_ratio=_value(row, "currentRatio"),
        cfo_to_profit=_value(row, "CFOToNP"),
        net_profit_yoy=_value(row, "YOYNI"),
        interest_cover=_value(row, "ebitToInterest"),
    )


def _flags(snapshot: Snapshot) -> list[RiskFlag]:
    """Every rule that fires, in a fixed order so two runs read the same."""
    found: list[RiskFlag] = []

    if snapshot.net_profit is not None and snapshot.net_profit < 0:
        found.append(
            RiskFlag(
                code="loss",
                label="亏损",
                value=snapshot.net_profit,
                threshold="净利润 < 0",
                weight=30,
                detail="亏损的公司没有可估的市盈率，也最有动机靠一次性收益或应计项目修饰报表",
            )
        )

    if snapshot.liability_to_asset is not None and snapshot.liability_to_asset > LEVERAGE_LIMIT:
        found.append(
            RiskFlag(
                code="over_leveraged",
                label="资产负债率过高",
                value=snapshot.liability_to_asset,
                threshold=f"> {LEVERAGE_LIMIT:.0%}",
                weight=25,
                detail="高杠杆意味着利率与再融资窗口的微小变化都能吞掉一年的利润；"
                "金融与地产天生高负债，没有行业口径时这条会误伤",
            )
        )

    if snapshot.cfo_to_profit is not None and snapshot.cfo_to_profit < CASH_CONVERSION_FLOOR:
        found.append(
            RiskFlag(
                code="profit_without_cash",
                label="利润不含现金",
                value=snapshot.cfo_to_profit,
                threshold=f"经营现金流/净利润 < {CASH_CONVERSION_FLOOR}",
                weight=20,
                detail="利润是账面数字，现金才是到手的钱；长期低于 0.3 通常来自应收账款堆积",
            )
        )

    if snapshot.current_ratio is not None and snapshot.current_ratio < LIQUIDITY_FLOOR:
        found.append(
            RiskFlag(
                code="thin_liquidity",
                label="短期偿债吃紧",
                value=snapshot.current_ratio,
                threshold=f"流动比率 < {LIQUIDITY_FLOOR}",
                weight=15,
                detail="流动负债超过流动资产，短期要靠续贷或变卖存货维持",
            )
        )

    if snapshot.net_profit_yoy is not None and snapshot.net_profit_yoy < PROFIT_COLLAPSE:
        found.append(
            RiskFlag(
                code="profit_collapse",
                label="净利润大幅下滑",
                value=snapshot.net_profit_yoy,
                threshold=f"同比 < {PROFIT_COLLAPSE:.0%}",
                weight=15,
                detail="单期腰斩先分清是一次性计提还是需求真的走了，前者会修复，后者不会",
            )
        )

    if snapshot.interest_cover is not None and snapshot.interest_cover < INTEREST_COVER_FLOOR:
        found.append(
            RiskFlag(
                code="weak_interest_cover",
                label="利息保障倍数过低",
                value=snapshot.interest_cover,
                threshold=f"< {INTEREST_COVER_FLOOR}",
                weight=10,
                detail="经营利润快盖不住利息支出，缓冲薄到一次加息或一轮价格战就能击穿",
            )
        )

    return found


def _level(score: float) -> str:
    if score >= 50:
        return LEVEL_HIGH
    if score >= 25:
        return LEVEL_MEDIUM
    return LEVEL_LOW


def _is_financial(snapshot: Snapshot) -> bool:
    """Whether this looks like a bank, an insurer or a broker.

    A financial firm reports no gross margin — it has no cost of goods — and it
    funds itself with deposits, so 90% leverage is the business model rather than
    a warning. Both signals must be present: leverage alone would catch a
    distressed industrial, and a missing margin alone could simply be missing.
    """
    if snapshot.gross_margin is not None:
        return False
    return snapshot.liability_to_asset is not None and snapshot.liability_to_asset > 0.85


#: Rules that measure an industrial balance sheet and mean nothing for a bank.
_INDUSTRIAL_ONLY = frozenset({"over_leveraged", "thin_liquidity"})


def assess(symbol: str, row: pd.Series | None) -> FinancialRisk:
    """Score one name. A missing report comes back ``未知``, never ``低``."""
    if row is None:
        return FinancialRisk(
            symbol=symbol,
            snapshot=None,
            flags=(),
            waived=(),
            score=0.0,
            level=LEVEL_UNKNOWN,
            likely_financial=False,
        )

    snapshot = _snapshot(symbol, row)
    found = _flags(snapshot)
    financial = _is_financial(snapshot)

    waived: tuple[RiskFlag, ...] = ()
    if financial:
        # not "the rule does not apply, so delete it" — the flag is still shown,
        # it just does not add to a score that is calibrated for manufacturers
        kept = [flag for flag in found if flag.code not in _INDUSTRIAL_ONLY]
        waived = tuple(flag for flag in found if flag.code in _INDUSTRIAL_ONLY)
        found = kept

    score = float(min(100, sum(flag.weight for flag in found)))
    return FinancialRisk(
        symbol=symbol,
        snapshot=snapshot,
        flags=tuple(found),
        waived=waived,
        score=score,
        level=_level(score),
        likely_financial=financial,
    )


def assess_many(
    panel: ReportPanel,
    symbols: list[str] | None = None,
    as_of: Any = None,
) -> list[FinancialRisk]:
    """Score a list of names against the newest report public on ``as_of``."""
    if symbols is not None:
        wanted = list(symbols)
    elif panel.frame.empty or "symbol" not in panel.frame.index.names:
        wanted = []
    else:
        wanted = list(panel.frame.index.get_level_values("symbol").unique())
    return [assess(symbol, panel.latest(symbol, as_of=as_of)) for symbol in wanted]
