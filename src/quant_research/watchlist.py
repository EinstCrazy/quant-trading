"""自选股与分类。

自选是用户标注的集合，不是行情的缓存：它只记「我关心哪些票、怎么分组」，
价格与涨跌永远从面板现读。存成一个 JSON 而不是 parquet——几十行、人要直接
看得懂、git diff 得出来，才是这类文件该有的样子。

结构::

    {
      "categories": ["白酒", "科技"],
      "stocks": {
        "600519.SH": {"name": "贵州茅台", "categories": ["白酒"],
                      "note": "", "added_at": "2026-11-06T10:00:00"}
      }
    }

一只票可以属于多个分类；分类删除时引用一并摘除，不留悬空标签。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_research.data.providers.base import normalize_symbol
from quant_research.logging_setup import get_logger

logger = get_logger("watchlist")

__all__ = [
    "add_stock",
    "empty",
    "load",
    "remove_category",
    "remove_stock",
    "save",
    "upsert_category",
]

_FILENAME = "watchlist.json"


def empty() -> dict[str, Any]:
    return {"categories": [], "stocks": {}}


def path(directory: Path | str) -> Path:
    return Path(directory).expanduser() / _FILENAME


def load(directory: Path | str) -> dict[str, Any]:
    """The stored list, or an empty one — a missing file is a fresh start."""
    file = path(directory)
    if not file.is_file():
        return empty()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("自选股文件读不出来（%s），按空表处理", exc)
        return empty()
    return {
        "categories": list(data.get("categories") or []),
        "stocks": dict(data.get("stocks") or {}),
    }


def save(directory: Path | str, data: dict[str, Any]) -> Path:
    """Atomic replacement: a half-written list must never be readable."""
    file = path(directory)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return file


def upsert_category(directory: Path | str, name: str) -> dict[str, Any]:
    data = load(directory)
    name = name.strip()
    if name and name not in data["categories"]:
        data["categories"].append(name)
        save(directory, data)
    return data


def remove_category(directory: Path | str, name: str) -> dict[str, Any]:
    """Deleting a category takes its references with it: no dangling tags."""
    data = load(directory)
    if name not in data["categories"]:
        return data
    data["categories"] = [c for c in data["categories"] if c != name]
    for stock in data["stocks"].values():
        stock["categories"] = [c for c in stock.get("categories", []) if c != name]
    save(directory, data)
    return data


def add_stock(
    directory: Path | str,
    symbol: str,
    *,
    name: str = "",
    categories: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Add or update one stock; unknown categories are created on the spot."""
    canonical = normalize_symbol(symbol)
    data = load(directory)
    wanted = [c.strip() for c in (categories or []) if c.strip()]
    for category in wanted:
        if category not in data["categories"]:
            data["categories"].append(category)
    existing = data["stocks"].get(canonical) or {}
    data["stocks"][canonical] = {
        "name": name or existing.get("name", ""),
        "categories": wanted or existing.get("categories", []),
        "note": existing.get("note", "") if note is None else note,
        "added_at": existing.get("added_at") or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    save(directory, data)
    return data


def remove_stock(directory: Path | str, symbol: str) -> dict[str, Any]:
    canonical = normalize_symbol(symbol)
    data = load(directory)
    data["stocks"].pop(canonical, None)
    save(directory, data)
    return data


def import_stocks(
    directory: Path | str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """批量加入自选：一次读写，逐条报告，幂等不重复。

    「已在自选里」的票不重复改动——批量粘贴最常见的翻车是把自己
    已有的分类备注覆盖掉。invalid 逐条带原因（行号 + 错误），成功的
    不用猜哪条进来了。
    """
    data = load(directory)
    added: list[dict[str, Any]] = []
    duplicates: list[str] = []
    invalid: list[dict[str, str]] = []
    seen_in_batch: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        raw = str(entry.get("symbol", "")).strip()
        if not raw:
            invalid.append({"line": str(index), "reason": "代码为空"})
            continue
        try:
            canonical = normalize_symbol(raw)
        except ValueError as exc:
            invalid.append({"line": str(index), "reason": f"{raw}: {exc}"})
            continue
        if canonical in data["stocks"] or canonical in seen_in_batch:
            duplicates.append(canonical)
            continue
        seen_in_batch.add(canonical)
        name = str(entry.get("name") or "").strip()
        categories = [c.strip() for c in (entry.get("categories") or []) if str(c).strip()]
        for category in categories:
            if category not in data["categories"]:
                data["categories"].append(category)
        data["stocks"][canonical] = {
            "name": name,
            "categories": categories,
            "note": "",
            "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        added.append({"symbol": canonical, "name": name})
    save(directory, data)
    return {"added": added, "duplicates": duplicates, "invalid": invalid}
