"""大盘广度快照：涨跌家数 + 涨跌停家数，按日留档。

量能回答「多少钱在动」，广度回答「多少人赚钱」——3300 家上涨和 3300
家下跌可以是同一个成交额，这两个数字必须在同一张卡上见面。

口径说明（诚实优先）：涨跌家数来自乐咕乐股的全市场统计，与本项目
面板样本（自选/持仓/成交额前 N）不是同一个分母——面板量比只覆盖
样本，广度是全市场；两者并排放，不硬算成同一个数。

约定与资金流快照一致：GET 只读缓存不联网，联网抓取只发生在显式动作
（POST 刷新、nightly）里；接口失败进 missing，有几项给几项。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from math import isnan
from pathlib import Path
from typing import Any

__all__ = ["DIRECTORY", "fetch", "load", "load_daily", "save"]

DIRECTORY = Path("processed/market_breadth")

# 中文行名 → 结构化键：接口换字段名时这里先炸，别让 0 冒充真数据
_LABEL_MAP = {
    "上涨": "up",
    "下跌": "down",
    "平盘": "flat",
    "停牌": "halted",
    "涨停": "limit_up",
    "跌停": "limit_down",
    "真实涨停": "limit_up_real",
    "真实跌停": "limit_down_real",
}


def _int(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if isnan(number) else int(number)


def fetch() -> dict[str, Any]:
    """抓乐咕乐股全市场活跃度统计：涨跌家数 + 涨跌停家数 + 活跃度。

    单接口一次给全，失败就整份 missing——广度没有「半份」可言，
    给一半再补另一半只会让两个数字的时点对不上。
    """
    import time

    import akshare as ak

    missing: list[str] = []
    counts: dict[str, int] = {}
    activity_pct: float | None = None
    trade_date: str | None = None
    frame = None
    # 乐咕接口间歇性「No tables found」——网页表格偶尔加载不出，
    # 重试两次比把抖动当失败诚实（数据源是真波动，不是我们的错）
    for attempt in range(3):
        try:
            frame = ak.stock_market_activity_legu()
            break
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试，最后一次仍败才记缺
            if attempt == 2:
                missing.append(f"涨跌家数：{type(exc).__name__}: {str(exc)[:80]}")
            else:
                time.sleep(2)
    if frame is not None:
        try:
            for _, row in frame.iterrows():
                label = str(row.iloc[0]).strip()
                if label == "活跃度":
                    raw = str(row.iloc[1]).replace("%", "")
                    activity_pct = float(raw) if raw.replace(".", "").isdigit() else None
                elif label == "统计日期":
                    trade_date = str(row.iloc[1]) or None
                elif label in _LABEL_MAP:
                    number = _int(row.iloc[1])
                    counts[_LABEL_MAP[label]] = number if number is not None else 0
        except Exception as exc:  # noqa: BLE001
            missing.append(f"涨跌家数解析：{type(exc).__name__}: {str(exc)[:80]}")

    up = counts.get("up")
    down = counts.get("down")
    # 上涨占比（有涨有跌才算得出来）：广度最省眼的单一数字
    up_ratio = (
        round(up / (up + down) * 100, 1)
        if up is not None and down is not None and up + down > 0
        else None
    )
    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "up": up,
        "down": down,
        "flat": counts.get("flat"),
        "halted": counts.get("halted"),
        "limit_up": counts.get("limit_up"),
        "limit_down": counts.get("limit_down"),
        "limit_up_real": counts.get("limit_up_real"),
        "limit_down_real": counts.get("limit_down_real"),
        "activity_pct": activity_pct,
        "up_ratio": up_ratio,
        "missing": missing,
    }


def _snapshot_date(snapshot: dict[str, Any]) -> str:
    """快照的业务日期：按本地抓取日留档（同资金流快照语义）。"""
    return snapshot["fetched_at"][:10]


def save(root: Path, snapshot: dict[str, Any]) -> Path:
    """按日留档 + 同步 latest：断一天就有洞，洞是可见的（缺那天文件）。"""
    directory = root / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    daily = directory / f"{_snapshot_date(snapshot)}.json"
    daily.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    latest = directory / "latest.json"
    latest.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return latest


def load(root: Path) -> dict[str, Any] | None:
    """最近一次广度快照（latest.json；坏文件返回 None，不抛）。"""
    path = root / DIRECTORY / "latest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_daily(root: Path, limit: int = 10) -> list[dict[str, Any]]:
    """按日期倒序读最近 limit 份日快照（latest.json 不参与，坏文件跳过）。"""
    directory = root / DIRECTORY
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("????-??-??.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out
