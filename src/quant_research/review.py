"""持仓复盘：把「现在是什么状态」翻译成「明天该盯什么」。

这不是预测器，也不是买卖指令。规则只做两件事：

1. 把每只持仓的客观状态（盈亏、均线位置、超买超卖、距前高）归到互斥
   的一档——先看风险再看机会，顺序即优先级；
2. 给出明天值得盯的价位（成本线、MA20/MA60、ATR 止损参考、20 日高低），
   让「明天怎么办」落成几个具体数字，而不是一句感觉。

档位是描述性的：「止损区」意味着「亏损已超阈值且趋势坏了」，不是
「必须卖」。卖不卖还取决于仓位大小、你的资金性质与判断——这些不在
这里，也不该假装在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STANCE_LABELS",
    "STANCE_ORDER",
    "ReviewRules",
    "Verdict",
    "advise",
    "summarize",
]

STANCE_LABELS = {
    "stop": "止损区",
    "reduce": "减仓观察",
    "take": "止盈区",
    "watch": "观察",
    "hold": "持有",
    "nodata": "数据不足",
}

# 前端按这个顺序排列：先风险，后机会
STANCE_ORDER = {"stop": 0, "reduce": 1, "take": 2, "watch": 3, "hold": 4, "nodata": 5}


@dataclass(frozen=True)
class ReviewRules:
    """复盘阈值。默认值即 ``config/settings.yaml`` 的 ``review:`` 节。"""

    stop_loss_pct: float = -8.0
    take_profit_pct: float = 15.0
    overbought_rsi: float = 70.0
    extreme_rsi: float = 80.0
    near_high_pct: float = 3.0
    atr_stop_multiple: float = 2.0


@dataclass(frozen=True)
class Verdict:
    stance: str
    headline: str
    reasons: tuple[str, ...] = ()
    levels: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "label": STANCE_LABELS.get(self.stance, self.stance),
            "headline": self.headline,
            "reasons": list(self.reasons),
            "levels": self.levels,
        }


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def advise(
    *,
    close: float | None,
    cost: float,
    rsi: float | None,
    ma20: float | None,
    ma60: float | None,
    atr: float | None,
    high20: float | None,
    low20: float | None,
    rules: ReviewRules | None = None,
) -> Verdict:
    """One position in, one verdict out. Order matters — risk first."""
    rules = rules or ReviewRules()
    if close is None or cost <= 0:
        return Verdict("nodata", "没有行情数据", ("面板里没有这只票，或历史不足两天",))

    pnl_pct = (close / cost - 1) * 100
    levels: dict[str, float] = {"cost": round(cost, 3), "close": round(close, 3)}
    if ma20 is not None:
        levels["ma20"] = round(ma20, 3)
    if ma60 is not None:
        levels["ma60"] = round(ma60, 3)
    if atr is not None:
        levels["atr_stop"] = round(close - rules.atr_stop_multiple * atr, 3)
    if high20 is not None:
        levels["high20"] = round(high20, 3)
    if low20 is not None:
        levels["low20"] = round(low20, 3)

    below_ma20 = ma20 is not None and close < ma20
    below_ma60 = ma60 is not None and close < ma60
    rsi_text = f"RSI {rsi:.0f}" if rsi is not None else "RSI 不可用"

    # 1) 亏损超阈值 + 跌破 MA20：趋势与盈亏同时报警
    if pnl_pct <= rules.stop_loss_pct and below_ma20:
        return Verdict(
            "stop",
            f"止损区：浮亏 {_pct(pnl_pct)}，且收盘已跌破 MA20",
            (
                f"亏损 {_pct(pnl_pct)}，超过 {_pct(rules.stop_loss_pct)} 的警戒线",
                f"收盘 {close:.2f} 低于 MA20 {ma20:.2f}——支撑已丢",
                "先决定这笔亏损是否在接受范围内：止损区的意义是逼自己面对，不是替你下单",
            ),
            levels,
        )

    # 2) 亏损超阈值但仍在 MA20 上方：趋势未坏，风险在盈亏本身
    if pnl_pct <= rules.stop_loss_pct:
        return Verdict(
            "reduce",
            f"警戒：浮亏 {_pct(pnl_pct)}，但价格仍在 MA20 上方",
            (
                f"亏损 {_pct(pnl_pct)} 已超警戒线",
                f"收盘仍在 MA20 {(ma20 or 0):.2f} 上方——趋势暂时没坏，是仓位与成本的问题",
                "把它当「要不要摊薄」来想，而不是「要不要割」：摊薄前想清楚理由是什么",
            ),
            levels,
        )

    # 3) 浮盈厚 + 极端超买：落袋的窗口
    if pnl_pct >= rules.take_profit_pct and rsi is not None and rsi >= rules.extreme_rsi:
        return Verdict(
            "take",
            f"止盈区：浮盈 {_pct(pnl_pct)}，{rsi_text} 极端超买",
            (
                f"浮盈 {_pct(pnl_pct)} 已超过 {_pct(rules.take_profit_pct)}",
                f"{rsi_text}——极端超买之后经常有回踩，不是必然下跌，但盈亏比在变差",
                "分批比清仓更常见：把利润先装进口袋一部分，剩下的用移动止损跟",
            ),
            levels,
        )

    # 4) 超买且贴着 20 日高点：放量滞涨要警惕
    if (
        rsi is not None
        and rsi >= rules.overbought_rsi
        and high20 is not None
        and high20 > 0
        and (high20 - close) / high20 * 100 <= rules.near_high_pct
    ):
        return Verdict(
            "reduce",
            f"减仓观察：{rsi_text} 超买，且贴着 20 日高点",
            (
                f"距 20 日高点 {high20:.2f} 只有 {(high20 - close) / high20 * 100:.1f}%",
                "超买 + 前高附近 = 最容易放量滞涨的位置：明天看量，不看价",
                "放量不过前高是减仓信号；缩量回踩不破 MA20 反而健康",
            ),
            levels,
        )

    # 5) 双均线下方：中期趋势已坏
    if below_ma20 and below_ma60:
        return Verdict(
            "reduce",
            "减仓观察：收盘同时低于 MA20 与 MA60",
            (
                f"收盘 {close:.2f} 在 MA20 {(ma20 or 0):.2f} 与 MA60 {(ma60 or 0):.2f} 下方",
                "短中期趋势同向走坏，反弹先当反抽看",
                f"若反弹能站回 MA20（{(ma20 or 0):.2f}）再谈持有，比现在猜底更稳",
            ),
            levels,
        )

    # 6) 跌破 MA20 但 MA60 仍在上方：短期走弱，中期未定
    if below_ma20:
        return Verdict(
            "watch",
            "观察：收盘跌破 MA20，中期均线尚未失守",
            (
                f"收盘 {close:.2f} 低于 MA20 {(ma20 or 0):.2f}",
                "短期动能转弱，但 MA60 还托着——先看能不能收回，不急着下结论",
                f"明天关注 MA20（{(ma20 or 0):.2f}）：收回且缩量是假摔，放量继续跌要减",
            ),
            levels,
        )

    # 7) 其余：趋势健康
    reasons: list[str] = [f"收盘 {close:.2f} 在 MA20 {(ma20 or close):.2f} 上方，趋势没坏"]
    if pnl_pct > 0:
        reasons.append(f"浮盈 {_pct(pnl_pct)}，趋势与盈亏同向，持有成本最低")
    else:
        reasons.append(f"浮亏 {_pct(pnl_pct)}，但在止损阈值内：这是正常波动区间")
    if rsi is not None:
        reasons.append(
            f"{rsi_text}——" + ("偏热，留意冲高回落" if rsi >= 60 else "中性偏冷，等方向")
        )
    return Verdict("hold", "持有：趋势健康，无需动作", tuple(reasons), levels)


def summarize(
    rows: list[dict[str, Any]],
    rules: ReviewRules | None = None,
) -> dict[str, Any]:
    """组合层的一句话：钱在哪、风险在哪。

    ``rows`` 每项含 symbol/stance/shares/cost/close（close 可为 None）。
    """
    valued = [r for r in rows if r.get("close") is not None]
    total_cost = sum(r["shares"] * r["cost"] for r in valued)
    total_value = sum(r["shares"] * r["close"] for r in valued)
    total_pnl = total_value - total_cost
    pnl_pct = (total_value / total_cost - 1) * 100 if total_cost else None
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["stance"]] = counts.get(row["stance"], 0) + 1
    worst = min(valued, key=lambda r: r["close"] / r["cost"], default=None)
    largest = max(valued, key=lambda r: r["shares"] * r["close"], default=None)
    concentration = (
        largest["shares"] * largest["close"] / total_value * 100
        if largest and total_value
        else None
    )

    risky = counts.get("stop", 0) + counts.get("reduce", 0)
    if not valued:
        note = "持仓里还没有能取到行情的票：先采集，再谈复盘"
    elif counts.get("stop"):
        note = f"{counts['stop']} 只处于止损区——明天的第一件事是它们，不是涨幅榜"
    elif risky:
        note = f"{risky} 只需要盯（止损/减仓观察），其余按节奏持有"
    elif pnl_pct is not None and pnl_pct > 0:
        note = f"结构健康：浮盈 {pnl_pct:+.1f}%，没有仓位处于风险档"
    else:
        note = "结构尚可：没有仓位处于风险档，浮亏在正常波动内"

    return {
        "n_positions": len(rows),
        "n_valued": len(valued),
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": None if pnl_pct is None else round(pnl_pct, 2),
        "counts": counts,
        "worst": None
        if worst is None
        else {
            "symbol": worst["symbol"],
            "pnl_pct": round((worst["close"] / worst["cost"] - 1) * 100, 2),
        },
        "concentration_pct": None if concentration is None else round(concentration, 1),
        "note": note,
    }
