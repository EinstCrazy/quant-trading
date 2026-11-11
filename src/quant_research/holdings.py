"""持仓账本：用户输入的成本与股数，不是行情缓存。

只存「我以什么价格拿了多少」，现价、盈亏、技术状态一律从面板现读——
存下来的价格永远是过期的价格。与自选股同一个形态（JSON、人可读、可
git diff），但语义不同：自选是「关心」，持仓是「真金白银」。

（本模块叫 holdings 而不是 portfolio：``quant_research.portfolio`` 已经是
组合构建包——决定「每只票该配多少」。这里是账本：记录「已经拿了多少」，
两者不是一回事。）

结构::

    {
      "positions": {
        "600519.SH": {"shares": 100, "cost": 1500.0, "note": "",
                      "added_at": "2026-11-06T10:00:00"}
      }
    }

成本价与股数必须为正：负数股数（融券）与零成本（送股后改成本）这里的
口径都表达不了，宁可拒绝写入，也不存一个分析时会算错的数。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_research.data.providers.base import normalize_symbol
from quant_research.logging_setup import get_logger

logger = get_logger("holdings")

__all__ = ["empty", "load", "path", "remove", "save", "upsert"]

_FILENAME = "holdings.json"


def empty() -> dict[str, Any]:
    return {"positions": {}}


def path(directory: Path | str) -> Path:
    return Path(directory).expanduser() / _FILENAME


def load(directory: Path | str) -> dict[str, Any]:
    """The stored positions, or an empty book — a missing file is a fresh start."""
    file = path(directory)
    if not file.is_file():
        return empty()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("持仓文件读不出来（%s），按空表处理", exc)
        return empty()
    return {"positions": dict(data.get("positions") or {})}


def save(directory: Path | str, data: dict[str, Any]) -> Path:
    """Atomic replacement: a half-written book must never be readable."""
    file = path(directory)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return file


def upsert(
    directory: Path | str,
    symbol: str,
    *,
    shares: float,
    cost: float,
    note: str | None = None,
) -> dict[str, Any]:
    """Add or replace one position; the same symbol means one lot, not two.

    同一只票分两次买入的情形，合并成一条（加权成本自己算好再录）——
    一个标的只有一条持仓是最不容易读错的模型。
    """
    if shares <= 0:
        raise ValueError("股数必须为正")
    if cost <= 0:
        raise ValueError("成本价必须为正")
    canonical = normalize_symbol(symbol)
    data = load(directory)
    existing = data["positions"].get(canonical) or {}
    data["positions"][canonical] = {
        "shares": float(shares),
        "cost": float(cost),
        "note": existing.get("note", "") if note is None else note,
        "added_at": existing.get("added_at") or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    save(directory, data)
    return data


def remove(directory: Path | str, symbol: str) -> dict[str, Any]:
    canonical = normalize_symbol(symbol)
    data = load(directory)
    data["positions"].pop(canonical, None)
    save(directory, data)
    return data


def import_positions(
    directory: Path | str,
    entries: list[dict[str, Any]],
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """批量录入持仓：一次读写，逐条报告。

    默认跳过已有持仓——批量粘贴的旧表格覆盖真实持仓是最贵的错误；
    想覆盖就用单条 upsert（那是明确的「我要改这只」）。skip_existing=False
    表示「这些行就是当前真相」，才允许替换。
    """
    data = load(directory)
    added: list[dict[str, Any]] = []
    duplicates: list[str] = []
    invalid: list[dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        raw = str(entry.get("symbol", "")).strip()
        if not raw:
            invalid.append({"line": str(index), "reason": "代码为空"})
            continue
        try:
            canonical = normalize_symbol(raw)
            shares = float(entry.get("shares"))
            cost = float(entry.get("cost"))
        except (ValueError, TypeError) as exc:  # None/空串交给同一处报告
            invalid.append({"line": str(index), "reason": f"{raw or '—'}: {exc}"})
            continue
        if shares <= 0 or cost <= 0:
            invalid.append({"line": str(index), "reason": f"{canonical}: 股数与成本必须为正"})
            continue
        if canonical in data["positions"] and skip_existing:
            duplicates.append(canonical)
            continue
        data["positions"][canonical] = {
            "shares": shares,
            "cost": cost,
            "note": "",
            "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        added.append({"symbol": canonical, "shares": shares, "cost": cost})
    save(directory, data)
    return {"added": added, "duplicates": duplicates, "invalid": invalid}
