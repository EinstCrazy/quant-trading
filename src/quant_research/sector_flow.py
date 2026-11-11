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

__all__ = ["DIRECTORY", "delta", "fetch", "load", "save"]

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


def _snapshot_date(snapshot: dict[str, Any]) -> str:
    """快照的业务日期：东财「今日」口径，按本地抓取日留档。"""
    return snapshot["fetched_at"][:10]


def save(root: Path, snapshot: dict[str, Any]) -> Path:
    """按日留档 + 同步 latest（向后兼容）。

    单日快照只有「今天谁在流入」，两次之间才有「风向变没变」——
    按日留档让环比成为可能；断一天就有洞，洞是可见的（缺那天文件）。
    """
    directory = root / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    daily = directory / f"{_snapshot_date(snapshot)}.json"
    daily.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    latest = directory / "latest.json"
    latest.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return latest


def load_daily(root: Path, limit: int = 2) -> list[dict[str, Any]]:
    """按日期倒序读最近 limit 份日快照（latest.json 不参与，坏文件跳过）。"""
    directory = root / DIRECTORY
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("????-??-??.json"), reverse=True)[:limit]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload.get("sectors"), list):
            out.append(payload)
    return out


def delta(root: Path) -> dict[str, Any]:
    """每行业的「较上一份快照」主力净流入变化（亿）。

    只有日期不同才可比：同一天抓两次不算变化（同一口径的两次采样）；
    只有一份快照时 change=None——首次采集没有可比对象，如实标注。
    新行业（昨天快照里没有的）进 new 标注而不是编一个 0。
    """
    snapshots = load_daily(root)
    if not snapshots:
        return {"available": False, "reason": "还没有按日快照：刷新一次资金流开始积累"}
    latest = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None
    if previous is None or _snapshot_date(latest) == _snapshot_date(previous):
        return {
            "available": False,
            "as_of": _snapshot_date(latest),
            "reason": "只有一份快照（或同日重复）——风向对比需要两个不同交易日",
        }
    prev_map = {row["name"]: row["main_net"] for row in previous["sectors"]}
    rows: list[dict[str, Any]] = []
    new_sectors: list[str] = []
    for row in latest["sectors"]:
        base = prev_map.get(row["name"])
        if base is None:
            new_sectors.append(row["name"])
            continue
        rows.append(
            {
                "name": row["name"],
                "main_net": row["main_net"],
                "change": row["main_net"] - base,
            }
        )
    rows.sort(key=lambda row: row["change"], reverse=True)
    return {
        "available": bool(rows),
        "as_of": _snapshot_date(latest),
        "prev_as_of": _snapshot_date(previous),
        "rows": rows,
        "new": new_sectors,
        "note": "变化 = 今日主力净流入 - 上一交易日主力净流入（亿）；正数是风向转强，负数是转弱",
    }


def load(root: Path) -> dict[str, Any] | None:
    target = root / DIRECTORY / "latest.json"
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
