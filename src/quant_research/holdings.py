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
        "默认::600519.SH": {"symbol": "600519.SH", "account": "默认",
                            "shares": 100, "cost": 1500.0, "note": "",
                            "added_at": "2026-11-06T10:00:00"}
      }
    }

同一个代码可以在多个账户各有一条持仓（不同券商分开记），键是
「账户::代码」；symbol 与 account 同时写进条目内部，任何下游读取
都不需要拆键。旧文件（键就是代码、无 account）读入时自动迁移到
「默认」账户，不写回——下一次 save 自然落成新格式。

成本价与股数必须为正：负数股数（融券）与零成本（送股后改成本）这里的
口径都表达不了，宁可拒绝写入，也不存一个分析时会算错的数。

账户只是一个分组标签：券商账户的现金余额、资金划转不在这里，也不该
假装在这里——本账本只回答「这只票我以什么成本拿了多少」。
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

__all__ = [
    "DEFAULT_ACCOUNT",
    "accounts",
    "empty",
    "load",
    "path",
    "remove",
    "save",
    "symbols",
    "upsert",
]

_FILENAME = "holdings.json"
_SEPARATOR = "::"
DEFAULT_ACCOUNT = "默认"


def empty() -> dict[str, Any]:
    return {"positions": {}}


def path(directory: Path | str) -> Path:
    return Path(directory).expanduser() / _FILENAME


def entry_key(account: str, symbol: str) -> str:
    """账本条目的键：账户::代码。账户名不含分隔符（upsert 校验拒绝）。"""
    return f"{account}{_SEPARATOR}{symbol}"


def _normalise_account(account: str | None) -> str:
    name = (account or "").strip()
    if not name:
        return DEFAULT_ACCOUNT
    if _SEPARATOR in name:
        raise ValueError(f"账户名不能包含分隔符「{_SEPARATOR}」")
    if len(name) > 24:
        raise ValueError("账户名最长 24 个字符")
    return name


def _migrate(positions: dict[str, Any]) -> dict[str, Any]:
    """旧格式（键=代码、无 account）→ 新格式（键=账户::代码）。

    只做读时迁移，不写盘：旧文件保持原样，直到下一次 save 才落成新
    格式——读路径不该有副作用。
    """
    migrated: dict[str, Any] = {}
    for key, entry in positions.items():
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or key)
        account = str(entry.get("account") or DEFAULT_ACCOUNT)
        migrated[entry_key(account, symbol)] = {**entry, "symbol": symbol, "account": account}
    return migrated


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
    return {"positions": _migrate(dict(data.get("positions") or {}))}


def save(directory: Path | str, data: dict[str, Any]) -> Path:
    """Atomic replacement: a half-written book must never be readable."""
    file = path(directory)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return file


def symbols(data: dict[str, Any]) -> list[str]:
    """账本里出现过的代码（去重）——同一票两账户只算一只。"""
    seen = {entry.get("symbol") for entry in (data.get("positions") or {}).values()}
    return sorted(symbol for symbol in seen if symbol)


def accounts(data: dict[str, Any]) -> list[str]:
    """账本里出现过的账户名（排序，默认账户在前）。"""
    names = {
        str(entry.get("account") or DEFAULT_ACCOUNT)
        for entry in (data.get("positions") or {}).values()
    }
    return sorted(names, key=lambda name: (name != DEFAULT_ACCOUNT, name))


def upsert(
    directory: Path | str,
    symbol: str,
    *,
    shares: float,
    cost: float,
    note: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Add or replace one lot in one account.

    同一个（账户, 代码）只留一条：分次买入请合并成加权成本再录。同一只
    票在不同账户是两条独立的持仓——券商账户分开记，合并成一个平均成本
    会把「哪个账户该卖多少」这件事弄丢。
    """
    if shares <= 0:
        raise ValueError("股数必须为正")
    if cost <= 0:
        raise ValueError("成本价必须为正")
    canonical = normalize_symbol(symbol)
    who = _normalise_account(account)
    data = load(directory)
    key = entry_key(who, canonical)
    existing = data["positions"].get(key) or {}
    data["positions"][key] = {
        "symbol": canonical,
        "account": who,
        "shares": float(shares),
        "cost": float(cost),
        "note": existing.get("note", "") if note is None else note,
        "added_at": existing.get("added_at") or datetime.now(UTC).isoformat(timespec="seconds"),
    }
    save(directory, data)
    return data


def remove(directory: Path | str, symbol: str, *, account: str | None = None) -> dict[str, Any]:
    """Remove one lot.

    account=None 时：该代码只有一条持仓才删；多个账户各持一份时报错，
    要求指定账户——一次点击删掉两个账户的持仓是最贵的误操作。
    """
    canonical = normalize_symbol(symbol)
    data = load(directory)
    if account is None:
        keys = [key for key, entry in data["positions"].items() if entry.get("symbol") == canonical]
        if len(keys) > 1:
            holders = "、".join(str(data["positions"][key].get("account")) for key in keys)
            raise ValueError(f"{canonical} 在多个账户里都有持仓（{holders}）——指定账户再删")
        for key in keys:
            data["positions"].pop(key, None)
    else:
        data["positions"].pop(entry_key(_normalise_account(account), canonical), None)
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
    表示「这些行就是当前真相」，才允许替换。entry 可带 account（缺省
    默认账户），同一票不同账户是不同条目。
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
            who = _normalise_account(entry.get("account"))
        except (ValueError, TypeError) as exc:  # None/空串交给同一处报告
            invalid.append({"line": str(index), "reason": f"{raw or '—'}: {exc}"})
            continue
        if shares <= 0 or cost <= 0:
            invalid.append({"line": str(index), "reason": f"{canonical}: 股数与成本必须为正"})
            continue
        key = entry_key(who, canonical)
        if key in data["positions"] and skip_existing:
            duplicates.append(f"{canonical}（{who}）")
            continue
        data["positions"][key] = {
            "symbol": canonical,
            "account": who,
            "shares": shares,
            "cost": cost,
            "note": "",
            "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        added.append({"symbol": canonical, "account": who, "shares": shares, "cost": cost})
    save(directory, data)
    return {"added": added, "duplicates": duplicates, "invalid": invalid}
