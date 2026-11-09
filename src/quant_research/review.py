"""持仓复盘：把「现在是什么状态」翻译成「明天该盯什么、遇到什么怎么办」。

这不是预测器，也不是买卖指令。规则只做三件事：

1. 把每只持仓的客观状态（盈亏、均线位置、超买超卖、距前高）归到互斥
   的一档——先看风险再看机会，顺序即优先级；
2. 给出明天值得盯的价位（成本线、MA20/MA60、ATR 止损参考、20 日高低），
   让「明天怎么办」落成几个具体数字，而不是一句感觉；
3. 给出**情景预案**：跌破哪里、站回哪里、放量突破哪里，各对应什么动作。
   预案是 if-then 的，不是预言——它把「盘中临时拍脑袋」提前到收盘后
   冷静的时候做。

档位是描述性的：「止损区」意味着「亏损已超阈值且趋势坏了」，不是
「必须卖」。卖不卖还取决于仓位大小、你的资金性质与判断——这些不在
这里，也不该假装在。预案同理：它说的是「如果发生 X，通常怎么做更稳」，
不是「必须这样做」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STANCE_LABELS",
    "STANCE_ORDER",
    "Plan",
    "ReviewRules",
    "Verdict",
    "advise",
    "discipline",
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

_STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2}


@dataclass(frozen=True)
class ReviewRules:
    """复盘阈值。默认值即 ``config/settings.yaml`` 的 ``review:`` 节。"""

    stop_loss_pct: float = -8.0
    take_profit_pct: float = 15.0
    overbought_rsi: float = 70.0
    extreme_rsi: float = 80.0
    near_high_pct: float = 3.0
    atr_stop_multiple: float = 2.0
    max_single_pct: float = 35.0
    max_risk_ratio_pct: float = 30.0


@dataclass(frozen=True)
class Plan:
    """One if-then: a specific trigger and what is usually done about it."""

    when: str
    then: str

    def to_dict(self) -> dict[str, str]:
        return {"when": self.when, "then": self.then}


@dataclass(frozen=True)
class Verdict:
    stance: str
    headline: str
    reasons: tuple[str, ...] = ()
    levels: dict[str, float] = field(default_factory=dict)
    plans: tuple[Plan, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "label": STANCE_LABELS.get(self.stance, self.stance),
            "headline": self.headline,
            "reasons": list(self.reasons),
            "levels": self.levels,
            "plans": [plan.to_dict() for plan in self.plans],
        }


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _plans(
    stance: str,
    *,
    kind: str | None = None,
    cost: float,
    ma20: float | None,
    atr_stop: float | None,
    high20: float | None,
    low20: float | None,
) -> tuple[Plan, ...]:
    """If-then plans for tomorrow: where to look, what it usually means.

    每条预案只在需要的价位存在时才生成——「跌破 MA20 —」不是预案，
    是一句废话。
    """
    plans: list[Plan] = []
    if stance == "nodata":
        return ()

    if stance == "stop":
        if ma20 is not None:
            plans.append(
                Plan(
                    f"放量继续跌、收在 MA20 {_f(ma20)} 下方",
                    "亏损继续扩大：先想清楚这笔钱能承受多少，减到能承受的部分"
                    "——摊薄换的是心理安慰，不是成本",
                )
            )
            plans.append(
                Plan(
                    f"缩量收回 MA20（{_f(ma20)}）上方",
                    "假摔的可能：允许再观察一天，但不加仓、不摊薄",
                )
            )
        plans.append(
            Plan(
                f"反弹到成本 {_f(cost)} 附近却站不上",
                "反弹当反抽：这是减仓离场的机会，不是解套的起点",
            )
        )

    elif stance == "reduce" and kind == "loss":
        if atr_stop is not None:
            plans.append(
                Plan(
                    f"跌破 ATR 止损参考 {_f(atr_stop)}",
                    "警戒升级为实质止损决策：对照你的承受能力，决定减多少",
                )
            )
        if ma20 is not None:
            plans.append(
                Plan(
                    f"放量站回 MA20 {_f(ma20)}",
                    "减仓压力解除：恢复持有观察，但别在亏损里加仓摊薄",
                )
            )
        plans.append(
            Plan(
                "继续阴跌且量能萎缩",
                "阴跌不言底：等放量止跌再说，别用时间换亏损",
            )
        )

    elif stance == "reduce" and kind == "overbought":
        if high20 is not None:
            plans.append(
                Plan(
                    f"高开冲高、放量却不过 20 日高 {_f(high20)}",
                    "分批兑现：先落袋一部分，别等回落才后悔",
                )
            )
            plans.append(
                Plan(
                    f"放量突破 {_f(high20)} 并收在上方",
                    "趋势加速：剩余仓位持有，"
                    + (f"止损上移到 MA20 {_f(ma20)}" if ma20 is not None else "止损上移"),
                )
            )
        if ma20 is not None:
            plans.append(
                Plan(
                    f"跌破 MA20 {_f(ma20)}",
                    "超买后第一道支撑丢了：减仓优先于补仓",
                )
            )

    elif stance == "reduce" and kind == "downtrend":
        if ma20 is not None:
            plans.append(
                Plan(
                    f"反弹到 MA20 {_f(ma20)} 附近放量不过",
                    "反抽确认：这是减仓位置，不是趋势反转",
                )
            )
            plans.append(
                Plan(
                    "放量收复 MA20 且次日不回落",
                    "趋势修复的早期信号：恢复观察，等第二个确认",
                )
            )
        if low20 is not None:
            plans.append(
                Plan(
                    f"跌破 20 日低 {_f(low20)}",
                    "下跌延续：别在下跌趋势里接刀，等结构走坏后再评估",
                )
            )

    elif stance == "take":
        if high20 is not None:
            plans.append(
                Plan(
                    f"高开冲高不过 20 日高 {_f(high20)}",
                    "分批止盈：先落袋一半是常见做法，剩下的用移动止损跟",
                )
            )
            plans.append(
                Plan(
                    f"放量突破 {_f(high20)}",
                    "剩余仓位持有，"
                    + (f"止损上移到 MA20 {_f(ma20)}" if ma20 is not None else "止损上移"),
                )
            )
        if ma20 is not None:
            plans.append(
                Plan(
                    f"跌破 MA20 {_f(ma20)}",
                    "超买行情结束的信号：剩余仓位止盈，别把利润还回去",
                )
            )

    elif stance == "watch":
        if ma20 is not None:
            plans.append(
                Plan(
                    f"收回 MA20 {_f(ma20)} 上方",
                    "观察解除：回到持有节奏，趋势没坏",
                )
            )
        if atr_stop is not None:
            plans.append(
                Plan(
                    f"继续跌破 ATR 止损参考 {_f(atr_stop)}",
                    "升级为减仓观察：短期走弱变成趋势走弱",
                )
            )
        plans.append(
            Plan(
                "缩量横盘在 MA20 下方",
                "不动，等方向：没有信号时最好的动作是不动作",
            )
        )

    elif stance == "hold":
        if ma20 is not None:
            plans.append(
                Plan(
                    f"缩量回踩 MA20 {_f(ma20)} 不破",
                    "正常波动：不动，趋势里的回踩是常态",
                )
            )
            plans.append(
                Plan(
                    f"放量跌破 MA20 {_f(ma20)}",
                    "转观察：看次日能否收回，别急着卖也别急着补",
                )
            )
        if high20 is not None:
            plans.append(
                Plan(
                    f"放量突破 20 日高 {_f(high20)}",
                    "趋势加速：止损上移，但不追高加仓",
                )
            )

    return tuple(plans)


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
    atr_stop = levels.get("atr_stop")

    def plans(stance: str, kind: str | None = None) -> tuple[Plan, ...]:
        return _plans(
            stance,
            kind=kind,
            cost=cost,
            ma20=ma20,
            atr_stop=atr_stop,
            high20=high20,
            low20=low20,
        )

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
            plans("stop"),
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
            plans("reduce", "loss"),
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
            plans("take"),
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
            plans("reduce", "overbought"),
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
            plans("reduce", "downtrend"),
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
            plans("watch"),
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
    return Verdict("hold", "持有：趋势健康，无需动作", tuple(reasons), levels, plans("hold"))


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


def discipline(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    rules: ReviewRules | None = None,
) -> list[dict[str, str]]:
    """组合层的纪律检查：与个股状态无关的几条结构性硬规矩。

    检查的是「仓位结构」而不是「观点」：集中度、风险档占比、重亏仓位、
    数据完整性。全过时不编一条「良好」——给一条明确的通过项，
    让人知道检查确实跑过。
    """
    rules = rules or ReviewRules()
    valued = [r for r in rows if r.get("close") is not None]
    items: list[dict[str, str]] = []

    unmatched = len(rows) - len(valued)
    if unmatched:
        items.append(
            {
                "item": "数据完整性",
                "status": "warn",
                "detail": f"{unmatched} 只取不到行情：这份复盘不完整，先补数据再谈结论",
            }
        )

    concentration = summary.get("concentration_pct")
    if concentration is not None:
        if concentration > rules.max_single_pct * 1.5:
            items.append(
                {
                    "item": "单一持仓集中度",
                    "status": "fail",
                    "detail": f"最大一只占 {concentration:.1f}%，超过 "
                    f"{rules.max_single_pct * 1.5:.1f}%——一次判断失误就能伤到组合",
                }
            )
        elif concentration > rules.max_single_pct:
            items.append(
                {
                    "item": "单一持仓集中度",
                    "status": "warn",
                    "detail": f"最大一只占 {concentration:.1f}%，超过 "
                    f"{rules.max_single_pct:.0f}% 的参考线",
                }
            )

    if valued:
        risky = sum(1 for r in valued if r["stance"] in ("stop", "reduce"))
        ratio = risky / len(valued) * 100
        if ratio > rules.max_risk_ratio_pct * 1.5:
            items.append(
                {
                    "item": "风险档占比",
                    "status": "fail",
                    "detail": f"{risky}/{len(valued)} 只处于止损或减仓观察档"
                    f"（{ratio:.0f}%）：组合整体在承压，先处理风险再找机会",
                }
            )
        elif ratio > rules.max_risk_ratio_pct:
            items.append(
                {
                    "item": "风险档占比",
                    "status": "warn",
                    "detail": f"{risky}/{len(valued)} 只处于风险档（{ratio:.0f}%），"
                    f"超过 {rules.max_risk_ratio_pct:.0f}% 的参考线",
                }
            )

    stops = [r for r in valued if r["stance"] == "stop"]
    if stops:
        items.append(
            {
                "item": "止损区仍有仓位",
                "status": "warn",
                "detail": f"{'、'.join(r['symbol'] for r in stops)}："
                "已经跌破 MA20 且亏损超阈值，明天需要给出明确决定",
            }
        )

    heavy = [r for r in valued if r["cost"] and (r["close"] / r["cost"] - 1) * 100 <= -20]
    if heavy:
        items.append(
            {
                "item": "重亏仓位",
                "status": "warn",
                "detail": f"{'、'.join(r['symbol'] for r in heavy)} 浮亏超过 20%："
                "这类亏损通常不是一天的错，回头看看买入理由还成立吗",
            }
        )

    if not items:
        items.append(
            {
                "item": "纪律检查",
                "status": "pass",
                "detail": "集中度、风险档占比、重亏仓位都没有触发参考线",
            }
        )
    return sorted(items, key=lambda item: _STATUS_ORDER.get(item["status"], 9))
