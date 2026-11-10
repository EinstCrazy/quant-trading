"""行业资金流快照：主力资金今天在往哪些行业去。

口径说明（诚实优先）：东财的行业分类（数百个细分行业）与 baostock 的证监
会行业分类不是一套——「主力净流入最大的电子」对不上面板里的「计算机、
通信和其他电子设备制造业」。所以本快照**不与板块表做跨口径匹配**，在
板块页独立成卡，标注口径；硬做模糊匹配只会产出看似精确的错配。

约定与外围快照一致：GET 只读缓存不联网，联网抓取只发生在显式动作
（POST 刷新按钮、nightly）里；接口失败进 missing，有几项给几项。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from math import isnan
from pathlib import Path
from typing import Any

__all__ = ["DIRECTORY", "fetch", "load", "save"]

DIRECTORY = Path("processed/sector_flow")

# 读数只看头尾：流入前 10、流出前 5——中段的行业既不领先也不垫底
_TOP_IN = 10
_TOP_OUT = 5


def _num(value: Any) -> float | None:
    """转换失败或 NaN（用 math.isnan 判定）一律 None——列缺失不写成 0。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(number) else number


def fetch() -> dict[str, Any]:
    """抓东财行业资金流排行（今日口径），接口失败进 missing。"""
    import akshare as ak

    missing: list[str] = []
    sectors: list[dict[str, Any]] = []
    try:
        frame = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        for _, row in frame.iterrows():
            net = _num(row.get("今日主力净流入-净额"))
            if net is None:
                continue
            sectors.append(
                {
                    "name": str(row["名称"]),
                    "main_net": net / 1e8,  # 元 → 亿
                    "main_net_pct": _num(row.get("今日主力净流入-净占比")),
                    "change_pct": _num(row.get("今日涨跌幅")),
                    "leader": str(row.get("今日主力净流入最大股") or ""),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 单接口失败按缺数据处理
        missing.append(f"行业资金流：{type(exc).__name__}: {str(exc)[:80]}")

    sectors.sort(key=lambda row: row["main_net"], reverse=True)
    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sectors": sectors,
        "top_in": sectors[:_TOP_IN],
        "top_out": list(reversed(sectors[-_TOP_OUT:])) if sectors else [],
        "missing": missing,
    }


def save(root: Path, snapshot: dict[str, Any]) -> Path:
    target = root / DIRECTORY / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return target


def load(root: Path) -> dict[str, Any] | None:
    target = root / DIRECTORY / "latest.json"
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
