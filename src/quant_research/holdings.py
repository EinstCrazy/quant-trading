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
    "account_details",
    "accounts",
    "create_account",
    "empty",
    "load",
    "path",
    "remove",
    "remove_account",
    "rename_account",
    "save",
    "symbols",
    "update",
    "upsert",
]

_FILENAME = "holdings.json"
_SEPARATOR = "::"
DEFAULT_ACCOUNT = "默认"


def empty() -> dict[str, Any]:
    return {"positions": {}, "accounts": []}


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
    raw_accounts = data.get("accounts")
    accounts: list[dict[str, Any]] = []
    for item in raw_accounts or []:
        if isinstance(item, dict) and item.get("name"):
            accounts.append({"name": str(item["name"]), "note": str(item.get("note") or "")})
        elif isinstance(item, str) and item:
            accounts.append({"name": item, "note": ""})
    return {
        "positions": _migrate(dict(data.get("positions") or {})),
        "accounts": accounts,
    }


def save(directory: Path | str, data: dict[str, Any]) -> Path:
    """Atomic replacement: a half-written book must never be readable.

    data 缺 "accounts" 键时从现有文件继承：只动持仓的旧调用不该悄悄
    清空显式账户列表。
    """
    if "accounts" not in data:
        file_probe = path(directory)
        try:
            prior = (
                json.loads(file_probe.read_text(encoding="utf-8")) if file_probe.is_file() else {}
            )
        except (OSError, json.JSONDecodeError):
            prior = {}
        data = {**data, "accounts": prior.get("accounts") or []}
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
    """全部可选账户：显式创建的 ∪ 持仓里出现过的（排序，默认账户在前）。

    显式列表支持「先建户、后录入」；历史持仓的账户照样可选——旧数据
    不因换了录入方式而消失。
    """
    names = {
        str(entry.get("account") or DEFAULT_ACCOUNT)
        for entry in (data.get("positions") or {}).values()
    }
    names |= {str(a.get("name")) for a in data.get("accounts") or [] if a.get("name")}
    return sorted(names, key=lambda name: (name != DEFAULT_ACCOUNT, name))


def create_account(directory: Path | str, name: str, *, note: str | None = None) -> dict[str, Any]:
    """先建户、后录入：账户是显式管理的，不是打字打出来的。

    重名拒绝（含与已有持仓的账户重名）——两个「华泰」比一个报错贵得多。
    空账户合法：建了还没录持仓，录入时的下拉里就该看得见。
    """
    who = _normalise_account(name)
    data = load(directory)
    if who in accounts(data):
        raise ValueError(f"账户「{who}」已存在")
    data["accounts"].append({"name": who, "note": (note or "").strip()})
    save(directory, data)
    return data


def account_details(data: dict[str, Any]) -> list[dict[str, Any]]:
    """账户明细（默认账户在前）：名字、备注、持仓数——下拉与汇总共用。"""
    counts: dict[str, int] = {}
    for entry in (data.get("positions") or {}).values():
        who = str(entry.get("account") or DEFAULT_ACCOUNT)
        counts[who] = counts.get(who, 0) + 1
    notes = {
        str(a.get("name")): str(a.get("note") or "")
        for a in data.get("accounts") or []
        if a.get("name")
    }
    return [
        {
            "name": name,
            "note": notes.get(name, ""),
            "n_positions": counts.get(name, 0),
        }
        for name in accounts(data)
    ]


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


def _locate(data: dict[str, Any], symbol: str, account: str | None) -> str:
    """按代码（可选账户）定位唯一条目键；歧义时拒绝而不是猜。"""
    canonical = normalize_symbol(symbol)
    if account is not None:
        key = entry_key(_normalise_account(account), canonical)
        if key not in data["positions"]:
            raise ValueError(f"{canonical} 在账户「{_normalise_account(account)}」里没有持仓")
        return key
    keys = [key for key, entry in data["positions"].items() if entry.get("symbol") == canonical]
    if not keys:
        raise ValueError(f"{canonical} 不在持仓里")
    if len(keys) > 1:
        holders = "、".join(str(data["positions"][key].get("account")) for key in keys)
        raise ValueError(f"{canonical} 在多个账户里都有持仓（{holders}）——指定账户再改")
    return keys[0]


def update(
    directory: Path | str,
    symbol: str,
    *,
    account: str | None = None,
    shares: float | None = None,
    cost: float | None = None,
    note: str | None = None,
    to_account: str | None = None,
) -> dict[str, Any]:
    """改一条持仓：股数 / 成本 / 备注 / 挪账户，一次读写一次落盘。

    编辑不能靠「删了重加」拼出来——两步之间崩溃会留下半截状态，而且
    added_at 会被重置成「刚买的」。to_account 是把这条持仓挪到另一个
    账户：目标键已存在时拒绝（同票已在那个账户，合并会丢口径）。
    """
    data = load(directory)
    key = _locate(data, symbol, account)
    entry = data["positions"][key]
    if shares is not None:
        if shares <= 0:
            raise ValueError("股数必须为正")
        entry["shares"] = float(shares)
    if cost is not None:
        if cost <= 0:
            raise ValueError("成本价必须为正")
        entry["cost"] = float(cost)
    if note is not None:
        entry["note"] = note
    if to_account is not None:
        who = _normalise_account(to_account)
        target = entry_key(who, str(entry["symbol"]))
        if target != key and target in data["positions"]:
            raise ValueError(f"{entry['symbol']} 在账户「{who}」里已有持仓——先删那边再挪")
        data["positions"].pop(key)
        entry["account"] = who
        data["positions"][target] = entry
    save(directory, data)
    return data


def rename_account(directory: Path | str, old: str, new: str) -> dict[str, Any]:
    """重命名账户：该账户下所有持仓整体迁移，added_at 与备注原样保留。

    目标账户已存在且两边有同一只票时拒绝——合并会把「哪个账户该卖
    多少」弄丢；没有重叠才允许迁移。重命名是账本操作，行情、复盘的
    账户口径由调用方各自跟进（复盘存档键也带账户）。
    """
    who_old = _normalise_account(old)
    who_new = _normalise_account(new)
    if who_old == who_new:
        return load(directory)
    data = load(directory)
    if who_old not in accounts(data):
        raise ValueError(f"账户「{who_old}」不存在")
    keys = [key for key, entry in data["positions"].items() if entry.get("account") == who_old]
    symbols_old = {str(data["positions"][key]["symbol"]) for key in keys}
    overlap = [
        entry["symbol"]
        for key, entry in data["positions"].items()
        if entry.get("account") == who_new and entry["symbol"] in symbols_old
    ]
    if overlap:
        raise ValueError(
            f"账户「{who_new}」里已有同样的票（{'、'.join(sorted(overlap))}）——先处理重叠再重命名"
        )
    for key in keys:
        entry = data["positions"].pop(key)
        entry["account"] = who_new
        data["positions"][entry_key(who_new, str(entry["symbol"]))] = entry
    data["accounts"] = [
        ({**a, "name": who_new} if a.get("name") == who_old else a)
        for a in data.get("accounts") or []
    ]
    save(directory, data)
    return data


def remove_account(directory: Path | str, name: str) -> dict[str, Any]:
    """删除账户：该账户下所有持仓一并移除。

    这是「清空这个券商户头」——单只票的移除走 remove。账户不存在时
    如实报错；默认账户同样可删（删光即空账本），不搞特殊豁免。
    """
    who = _normalise_account(name)
    data = load(directory)
    keys = [key for key, entry in data["positions"].items() if entry.get("account") == who]
    explicit = [a for a in data.get("accounts") or [] if a.get("name") == who]
    if not keys and not explicit:
        raise ValueError(f"账户「{who}」不存在")
    for key in keys:
        data["positions"].pop(key)
    data["accounts"] = [a for a in data.get("accounts") or [] if a.get("name") != who]
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
