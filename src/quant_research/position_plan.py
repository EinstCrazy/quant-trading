"""明日仓位预案：开盘位置（高/平/低）× 盘中量能（放/缩）→ 仓位动作。

纪律先于预测：预案是条件表——「如果明天低开破止损 → 减半，收盘仍低
→ 清」，不是「明天会跌」。所有价位都来自今天的数据（ATR 止损、MA20、
成本、20 日高低）；明天的量能没人知道，所以量能只出现在条件里
（「放量则…」「缩量则…」），不出现在结论里。

仓位以「留几成」表达（1 = 全持有，0 = 清仓）——账本没有现金数据，
不给「总仓位几成」这种编造的数字；整体影响用持仓市值的变化区间
表达，怎么算的一目了然。
"""

from __future__ import annotations

from typing import Any

__all__ = ["plan_positions"]


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _volume_note(ratio: float | None) -> str:
    """今日量能状态——明天的量能未知，只作为条件的参照系。"""
    if ratio is None:
        return "量能未知（无成交量数据）"
    if ratio >= 1.5:
        return f"今日放量（量比 {ratio:g}）"
    if ratio <= 0.7:
        return f"今日缩量（量比 {ratio:g}）"
    return f"今日量能中性（量比 {ratio:g}）"


def _branch(stance: str, lv: dict[str, float]) -> dict[str, str]:
    """三情景 × 量能条件的动作文案。价位缺失时那条分支降级为纪律提醒。

    每条动作都带具体价位（来自今天的盘面），不给「注意风险」这种
    没法执行的话。
    """
    cost = lv.get("cost")
    ma20 = lv.get("ma20")
    stop = lv.get("atr_stop")
    high20 = lv.get("high20")
    ma20_txt = _f(ma20)
    high_txt = _f(high20)

    if stance == "exit":
        return {
            "gap_up": (
                f"高开是离场窗口：反弹到成本 {_f(cost)} 附近分批卖，不恋战；"
                "放量冲高也别把离场计划改成加仓理由"
            ),
            "flat": "平开按离场计划执行：开盘减半，收盘前清",
            "gap_down": (
                f"低开是计划赶不上的信号：破止损 {_f(stop)} 直接减半，"
                "收盘仍低清仓——离场档不看量能手软"
            ),
        }

    if stance == "stop":
        return {
            "gap_up": (
                f"反弹到成本 {_f(cost)} 站不上 → 减仓离场；"
                f"放量站回 MA20 {ma20_txt} 才允许多观察一天，不加仓不摊薄"
            ),
            "flat": (
                f"平开不主动动作：收复 MA20 {ma20_txt} 再谈持有，"
                "否则逢反弹继续减——止损区的仓位只出不进"
            ),
            "gap_down": (f"低开继续破位：减到能承受的仓位；收盘前仍低于 {_f(stop)} → 清仓"),
        }

    if stance == "reduce":
        return {
            "gap_up": (
                f"高开是减仓窗口：接近 20 日高 {high_txt} 或成本上方分批减 1/3–1/2；"
                "缩量冲高更要减——缩量的新高靠不住"
            ),
            "flat": (f"平开按减仓计划：盘中反弹分批减；放量站回 MA20 {ma20_txt} 才暂停减仓"),
            "gap_down": (f"低开破 ATR 止损 {_f(stop)} → 减半；缩量低开未破 → 持有观察一天"),
        }

    if stance == "take":
        return {
            "gap_up": (
                f"高开先兑现：接近 20 日高 {high_txt} 分批止盈 1/4–1/3；"
                "放量突破并收在上方 → 剩余持有，止损上移到 MA20"
            ),
            "flat": (
                f"平开持有让利润跑：收在 MA20 {ma20_txt} 上方不动；"
                "放量滞涨（价不新高量还在放）→ 先减 1/4"
            ),
            "gap_down": (
                f"低开回吐：破 ATR 止损 {_f(stop)} → 减半保利润，收盘仍低清仓；缩量回踩未破 → 不动"
            ),
        }

    # hold / watch / nodata：纪律一致的持有预案
    return {
        "gap_up": (
            f"高开缩量冲高不追：接近 20 日高 {high_txt} 分批止盈 1/4；放量突破并站稳 → 继续持有"
        ),
        "flat": (f"平开持有：收在 MA20 {ma20_txt} 上方不动；收盘跌破 → 减 1/3"),
        "gap_down": (
            f"低开破 ATR 止损 {_f(stop)} → 先减一半，收盘仍低 → 清；缩量低开未破 → 不动等收复"
        ),
    }


def _worst_ratio(stance: str) -> float:
    """谨慎分支（该减的都触发）后留下的成数——整体市值区间的输入。"""
    if stance in ("exit", "stop"):
        return 0.0
    if stance == "nodata":
        return 1.0  # 没数据就没有动作依据，预案不假装能管理它
    return 0.5


def _per_row(row: dict[str, Any]) -> dict[str, Any]:
    lv = dict(row.get("levels") or {})
    lv.setdefault("cost", row.get("cost"))
    shares = float(row.get("shares") or 0)
    close = row.get("close")
    stop = lv.get("atr_stop")
    # 贴着止损 = 收盘距止损不到 5%：明天一个普通低开就会碰到
    near_stop = bool(
        stop is not None and close is not None and close > 0 and (close - stop) / close < 0.05
    )
    return {
        "symbol": row.get("symbol"),
        "account": row.get("account"),
        "name": row.get("name", ""),
        "label": row.get("label") or row.get("stance"),
        "close": close,
        "volume_note": _volume_note(row.get("volume_ratio")),
        "branches": _branch(str(row.get("stance")), lv),
        "worst_ratio": _worst_ratio(str(row.get("stance"))),
        "near_stop": near_stop,
        "market_value": row.get("market_value"),
        "worst_value": (
            None
            if close is None or not shares
            else round(shares * close * _worst_ratio(str(row.get("stance"))), 2)
        ),
    }


def plan_positions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """复盘 rows → 明日仓位预案（每票三情景 + 整体市值区间）。"""
    plans = [_per_row(row) for row in rows if row.get("close") is not None]
    # 全部清仓的极端预案 worst=0：0 是合法结果不是「没数据」——
    # 用 plans 是否为空判断，别让 if 0.0 把结论吞了
    current = round(sum(float(p["market_value"] or 0) for p in plans), 2) if plans else None
    worst = round(sum(float(p["worst_value"] or 0) for p in plans), 2) if plans else None
    return {
        "positions": plans,
        "overall": {
            "current_value": current,
            "worst_value": worst,
            "worst_drawdown_pct": (
                None if not plans or not current else round((worst / current - 1) * 100, 2)
            ),
            "n_near_stop": sum(1 for p in plans if p["near_stop"]),
            "n_managed": len(plans),
        },
        "note": (
            "预案是条件表，不是预测：明天开盘对照执行，纪律优先于观点。"
            "没有现金数据，仓位用「留几成」表达；整体影响用持仓市值区间表达。"
        ),
    }
