"""外围盘：隔夜美股、港股、亚太、欧洲、汇率商品与美债，对 A 股的影响路径。

三个立场，决定了这个模块长什么样：

1. **外围只影响开盘情绪，不改变 A 股自身的趋势。** 所以这里不写「美股涨
   所以买科技」，只写「外围给了一个偏暖/偏冷的开盘环境，A 股自己的量能与
   板块结构才是决定性的」。每条提示都带「前提是 A 股自身××」。
2. **只描述传导路径，不做相关性数字。** 纳指跌 → 成长股情绪承压，这条路径
   是市场结构给的；「相关性 0.7」需要长周期统计，而且会随样本期漂移。没有
   统计就不假装有。
3. **数据可能拿不到。** 外围接口依赖外网，抓到几项算几项，缺的进 ``missing``，
   没有数据的地方不写「持平」——「没数据」和「没变化」是两回事。

落盘只存快照（``data/overseas/latest.json``）：外盘行情是「当下的环境」，
历史价值在于对比昨天，本模块不负责存历史序列。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DIRECTORY",
    "analyze",
    "fetch",
    "latest_path",
    "load",
    "save",
    "sector_link",
]

DIRECTORY = "overseas"
_FILENAME = "latest.json"

# 关注清单：代码 → 分组。不追求全，只留对 A 股有传导路径的。
_INDEX_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("美股", ("DJIA", "SPX", "NDX")),
    ("港股", ("HSI", "HSCEI", "HSTECH")),
    ("亚太", ("N225", "KS11", "TWII", "AS51")),
    ("欧洲", ("SX5E", "GDAXI", "FCHI", "FTSE")),
    ("汇率与商品", ("UDI", "CRB")),
)

# 外盘商品：黄金避险、原油成本端、铜需求端、白银跟黄金。
_COMMODITY_CODES = ("GC", "CL", "HG", "SI")
# 接口只回名称不回代码，按名称对回关注清单里的代码——板块关联按代码找锚
_COMMODITY_CODE_BY_NAME = {
    "COMEX黄金": "GC",
    "NYMEX原油": "CL",
    "COMEX铜": "HG",
    "COMEX白银": "SI",
}

# 展示顺序：分组按上面的清单，组内按清单里的代码顺序——接口返回的顺序
# 是按行情活跃度排的，读起来会跳。
_GROUP_ORDER = {name: index for index, (name, _) in enumerate(_INDEX_GROUPS)} | {"大宗商品": 99}
_CODE_ORDER = {code: index for _, codes in _INDEX_GROUPS for index, code in enumerate(codes)}
_COMMODITY_ORDER = {
    name: index for index, name in enumerate(("COMEX黄金", "NYMEX原油", "COMEX铜", "COMEX白银"))
}

# 情绪分权重：美股是隔夜情绪的主源，港股是 A/H 联动的近邻，欧洲与亚太
# 只做背景。美元指数反向（走强 = 风险偏好降、人民币与外资承压）。
_WEIGHTS = {"美股": 0.5, "港股": 0.2, "亚太": 0.1, "欧洲": 0.1, "汇率与商品": 0.1}

_STALE_HOURS = 12.0


def _num(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits)


def latest_path(directory: Path | str) -> Path:
    return Path(directory).expanduser() / DIRECTORY / _FILENAME


# --- 板块 × 外围 --------------------------------------------------------------


# 板块名里的关键词 → 外围品种 → 一句联动说明。只挂有真实传导路径的：
# 内需板块（食品、白酒）与外围相关性弱，匹配不到就显示 —，不硬挂一个
# 品种上去充数。US10Y 是伪代码，取的是 rates 里的美债收益率。
_SECTOR_LINKS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        ("石油", "天然气", "油服"),
        ("CL",),
        "原油是开采板块的收入端；油价下行时化工、航空的成本端反而受益",
    ),
    (("黄金", "贵金属"), ("GC",), "金价直接决定黄金股的利润与情绪"),
    (("有色", "铜"), ("HG",), "铜价跟踪全球制造业需求，是周期板块的先行指标"),
    (
        ("计算机", "通信", "电子", "半导体", "软件"),
        ("NDX", "SPX"),
        "纳指是全球科技股风向标：隔夜科技行情直接影响开盘情绪",
    ),
    (
        ("银行", "保险", "证券", "货币金融", "非银"),
        ("US10Y",),
        "美债长端利率影响保险投资收益与成长/价值风格的切换",
    ),
    (("航空", "航运", "物流"), ("CL",), "原油是成本端（反向）：油价大跌对航空反而是利好"),
)


def sector_link(sector_name: str, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """一个板块在外围有哪些锚品种，各自最新涨跌多少。

    匹配不到关键词、或者锚品种的行情没抓到，都返回 ``None``——宁可不显示，
    也不挂一个「持平」上去冒充有关联。
    """
    if not snapshot:
        return None
    quotes = snapshot.get("quotes") or []
    rates = snapshot.get("rates") or {}
    for keywords, codes, note in _SECTOR_LINKS:
        if not any(word in sector_name for word in keywords):
            continue
        items: list[dict[str, Any]] = []
        for code in codes:
            if code == "US10Y":
                if rates.get("us10y") is not None:
                    change = rates.get("us10y_change_bp")
                    items.append(
                        {
                            "name": "美债10Y",
                            "change_pct": None,
                            "change_bp": change,
                            "as_of": rates.get("date"),
                        }
                    )
                continue
            row = next(
                (q for q in quotes if q["code"] == code and q.get("change_pct") is not None),
                None,
            )
            if row is not None:
                items.append(
                    {
                        "name": row["name"],
                        "change_pct": row["change_pct"],
                        "change_bp": None,
                        "as_of": row["as_of"],
                    }
                )
        if not items:
            return None
        return {"sector": sector_name, "items": items, "note": note}
    return None


# --- 采集 ----------------------------------------------------------------------


def fetch() -> dict[str, Any]:
    """抓一次外围快照。三个接口各自独立失败，缺的进 ``missing``。

    延迟导入 akshare：没装或导入失败时这个模块仍然可用（读缓存、做分析），
    只是不能刷新。
    """
    quotes: list[dict[str, Any]] = []
    missing: list[str] = []
    rates: dict[str, Any] = {}

    try:
        import akshare as ak

        frame = ak.index_global_spot_em()
        # 港股单独抓（见下）：这个接口对港股只回名称，价格涨跌幅常是空占位，
        # 混进来就是一行有名无实的记录——看着像有数据，其实全是 None
        wanted = {
            code
            for _, codes in _INDEX_GROUPS
            for code in codes
            if code not in ("HSI", "HSCEI", "HSTECH")
        }
        for _, row in frame.iterrows():
            code = str(row["代码"]).strip()
            if code not in wanted:
                continue
            group = next(name for name, codes in _INDEX_GROUPS if code in codes)
            quotes.append(
                {
                    "group": group,
                    "code": code,
                    "name": str(row["名称"]).strip(),
                    "price": _num(row["最新价"]),
                    "change_pct": _num(row["涨跌幅"]),
                    "as_of": str(row["最新行情时间"]).strip(),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 网络/接口都可能坏，不能拖垮整个快照
        missing.append(f"全球指数：{type(exc).__name__} {exc}")

    try:
        import akshare as ak

        # 港股走新浪：A/H 联动的近邻，信号与情绪分都指望它有真实数值
        frame = ak.stock_hk_index_spot_sina()
        hk_codes = {"HSI", "HSCEI", "HSTECH"}
        for _, row in frame.iterrows():
            code = str(row["代码"]).strip()
            if code not in hk_codes:
                continue
            quotes.append(
                {
                    "group": "港股",
                    "code": code,
                    "name": str(row["名称"]).strip(),
                    "price": _num(row["最新价"]),
                    "change_pct": _num(row["涨跌幅"]),
                    # 接口不带行情时间：用抓取时刻近似（港股盘中 15 分钟延迟内）
                    "as_of": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        missing.append(f"港股指数：{type(exc).__name__} {str(exc)[:60]}")

    try:
        import akshare as ak

        frame = ak.futures_foreign_commodity_realtime(symbol=list(_COMMODITY_CODES))
        for _, row in frame.iterrows():
            quotes.append(
                {
                    "group": "大宗商品",
                    "code": _COMMODITY_CODE_BY_NAME.get(str(row["名称"]).strip(), "COMMODITY"),
                    "name": str(row["名称"]).strip(),
                    "price": _num(row["最新价"]),
                    "change_pct": _num(row["涨跌幅"]),
                    "as_of": f"{row['日期']} {row['行情时间']}",
                }
            )
    except Exception as exc:  # noqa: BLE001
        missing.append(f"外盘商品：{type(exc).__name__} {exc}")

    try:
        import akshare as ak

        frame = ak.bond_zh_us_rate()
        frame = frame.dropna(subset=["美国国债收益率10年"])
        row = frame.iloc[-1]
        previous = frame.iloc[-2] if len(frame) > 1 else row
        rates = {
            "date": str(row["日期"]),
            "us10y": _num(row["美国国债收益率10年"]),
            "cn10y": _num(row["中国国债收益率10年"]),
            "us10y_change_bp": _num(
                (float(row["美国国债收益率10年"]) - float(previous["美国国债收益率10年"])) * 100, 1
            ),
        }
    except Exception as exc:  # noqa: BLE001
        missing.append(f"美债收益率：{type(exc).__name__} {exc}")

    return {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "quotes": quotes,
        "rates": rates,
        "missing": missing,
    }


def save(directory: Path | str, snapshot: dict[str, Any]) -> Path:
    """同一天重复刷新直接覆盖：这是快照，不是历史。"""
    target = latest_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def load(directory: Path | str) -> dict[str, Any] | None:
    target = latest_path(directory)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


# --- 分析 ----------------------------------------------------------------------


def _age_hours(fetched_at: str | None) -> float | None:
    if not fetched_at:
        return None
    try:
        moment = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - moment).total_seconds() / 3600, 1)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _group_rows(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = [name for name, _ in _INDEX_GROUPS] + ["大宗商品"]
    groups: list[dict[str, Any]] = []
    for name in order:
        rows = [row for row in quotes if row.get("group") == name]
        if not rows:
            continue
        changes = [row["change_pct"] for row in rows if row.get("change_pct") is not None]
        groups.append(
            {
                "name": name,
                "avg_change": _num(_mean(changes)),
                "rows": rows,
            }
        )
    return groups


def _tone(groups: list[dict[str, Any]]) -> dict[str, Any]:
    """A weighted read of the overnight backdrop — descriptive, not predictive."""
    weighted = 0.0
    total = 0.0
    for group in groups:
        weight = _WEIGHTS.get(group["name"])
        if weight is None or group["avg_change"] is None:
            continue
        # 美元指数走强是收紧，不是宽松——所以它在「汇率与商品」里反向计入
        if group["name"] == "汇率与商品":
            usd = next(
                (
                    row
                    for row in group["rows"]
                    if row["code"] == "UDI" and row["change_pct"] is not None
                ),
                None,
            )
            if usd is not None:
                weighted -= usd["change_pct"] * weight
                total += weight
            continue
        weighted += group["avg_change"] * weight
        total += weight
    score = round(weighted / total, 2) if total else None
    if score is None:
        label = "数据不足"
    elif score >= 0.5:
        label = "风险偏好回升"
    elif score <= -0.5:
        label = "风险偏好回落"
    else:
        label = "分化"
    return {"label": label, "score": score}


def _signals(quotes: list[dict[str, Any]], rates: dict[str, Any]) -> list[dict[str, Any]]:
    """每条：谁动了多少 → 影响 A 股哪一块 → 需要 A 股自身什么配合才成立。"""
    signals: list[dict[str, Any]] = []

    def find(code: str) -> dict[str, Any] | None:
        return next((row for row in quotes if row["code"] == code), None)

    nasdaq = find("NDX")
    if nasdaq and nasdaq["change_pct"] is not None:
        change = nasdaq["change_pct"]
        if change <= -1:
            signals.append(
                {
                    "market": nasdaq["name"],
                    "change": change,
                    "impact": "科技成长开盘承压：低开别急着抄，等前 30 分钟量能是否缩",
                    "condition": "若低开缩量、且 A 股自身科技板块昨日没有放量出货，视为情绪冲击而非趋势转折",
                }
            )
        elif change >= 1:
            signals.append(
                {
                    "market": nasdaq["name"],
                    "change": change,
                    "impact": "科技成长情绪偏暖：高开是情绪，能不能延续看 A 股自己的量",
                    "condition": "高开若在 10:00 前量能不跟（缩量），高开低走风险大",
                }
            )

    hsi = find("HSI")
    if hsi and hsi["change_pct"] is not None and abs(hsi["change_pct"]) >= 1:
        direction = (
            "走弱 → A/H 联动下港股通标的有压力，北向情绪偏冷"
            if hsi["change_pct"] < 0
            else "走强 → A/H 倒挂收窄，港股通标的相对占优"
        )
        signals.append(
            {
                "market": hsi["name"],
                "change": hsi["change_pct"],
                "impact": direction,
                "condition": "A/H 溢价本身已在极端位置时，联动会减弱",
            }
        )

    hstech = find("HSTECH")
    if hstech and hstech["change_pct"] is not None and abs(hstech["change_pct"]) >= 1.5:
        direction = (
            "恒生科技走弱 → 隔夜/盘中科技情绪直接传导，A 股成长（半导体、AI 硬件、互联网）开盘承压"
            if hstech["change_pct"] < 0
            else "恒生科技走强 → 科技情绪回暖，A 股成长开盘有联动支撑"
        )
        signals.append(
            {
                "market": hstech["name"],
                "change": hstech["change_pct"],
                "impact": direction,
                "condition": "港股科技与 A 股成长的联动靠南向资金与共同权重股（腾讯、阿里、美团）；若南向逆势净买入，联动减弱",
            }
        )

    usd = find("UDI")
    if usd and usd["change_pct"] is not None and abs(usd["change_pct"]) >= 0.3:
        direction = (
            "美元走强 → 人民币承压、外资流出压力上升，高估值成长与外资重仓股更敏感"
            if usd["change_pct"] > 0
            else "美元走弱 → 人民币压力缓解，外资流入环境改善，成长与外资重仓股受益"
        )
        signals.append(
            {
                "market": usd["name"],
                "change": usd["change_pct"],
                "impact": direction,
                "condition": "看人民币中间价是否跟随；只有汇率同步才谈得上资金流动",
            }
        )

    us10y = rates.get("us10y")
    us10y_change = rates.get("us10y_change_bp")
    if us10y is not None and us10y_change is not None and abs(us10y_change) >= 5:
        direction = (
            "美债长端上行 → 全球久期资产承压，A 股高估值成长（PE 高位、无盈利支撑的题材）最敏感"
            if us10y_change > 0
            else "美债长端下行 → 折现率压力缓解，成长股估值环境改善"
        )
        signals.append(
            {
                "market": "美债 10 年期",
                "change": us10y_change,
                "impact": direction,
                "condition": f"当前 {us10y:.2f}%：高位区域的边际变化比绝对值更有意义",
            }
        )

    oil = find("NYMEX原油") or next((row for row in quotes if "原油" in row["name"]), None)
    if oil and oil["change_pct"] is not None and abs(oil["change_pct"]) >= 2:
        direction = (
            "原油大涨 → 石油石化/油服情绪偏暖，但航空、化工、物流成本端承压"
            if oil["change_pct"] > 0
            else "原油大跌 → 石油石化承压，成本端敏感的航空/化工/物流相对受益"
        )
        signals.append(
            {
                "market": oil["name"],
                "change": oil["change_pct"],
                "impact": direction,
                "condition": "板块是否有资金愿意接，仍要看 A 股自身的量能",
            }
        )

    gold = next((row for row in quotes if "黄金" in row["name"]), None)
    if gold and gold["change_pct"] is not None and gold["change_pct"] >= 1:
        signals.append(
            {
                "market": gold["name"],
                "change": gold["change_pct"],
                "impact": "黄金走强 → 黄金股情绪偏暖；同时是避险情绪的信号，对高 beta 板块不友好",
                "condition": "黄金股跟涨与否看 A 股黄金板块昨日的量能位置",
            }
        )

    return signals


def _advice(
    groups: list[dict[str, Any]],
    tone: dict[str, Any],
    signals: list[dict[str, Any]],
    missing: list[str],
) -> str:
    if not groups:
        return "外围数据一项都没拿到——先刷新；在拿到之前，任何对外围的判断都是猜的，不做。"
    score = tone["score"]
    parts: list[str] = []
    if score is None:
        parts.append("外围只有部分数据，情绪分算不出来，下面按分组单独看。")
    elif score >= 0.5:
        parts.append(
            f"隔夜外围整体偏暖（情绪分 {score:+.2f}）：A 股开盘环境不差。"
            "但外围给的是开盘价，不是全天走势——高开之后能不能守住，看 A 股自己的量能与板块接力。"
        )
    elif score <= -0.5:
        parts.append(
            f"隔夜外围整体偏冷（情绪分 {score:+.2f}）：开盘承压的概率高。"
            "低开不等于坏事——低开缩量、且 A 股自身没有放量出货的板块，往往是情绪冲击；"
            "低开放量才是真的要减。"
        )
    else:
        parts.append(
            f"隔夜外围分化（情绪分 {score:+.2f}）：没有一致方向，外围不构成今日的主要变量，"
            "按 A 股自己的板块与量能走。"
        )
    if signals:
        strongest = max(signals, key=lambda item: abs(item["change"]))
        parts.append(
            f"最能带动情绪的是{strongest['market']}（{strongest['change']:+.2f}{'bp' if strongest['market'] == '美债 10 年期' else '%'}）：{strongest['impact']}。"
        )
    if missing:
        parts.append(
            f"注意：{len(missing)} 项数据没抓到（{'、'.join(item.split('：')[0] for item in missing)}），相关判断缺依据。"
        )
    return " ".join(parts)


def analyze(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把一份快照读成「分组 + 情绪 + 传导 + 一句话建议」。"""
    quotes = [row for row in snapshot.get("quotes", []) if row.get("change_pct") is not None]
    rates = snapshot.get("rates") or {}
    groups = _group_rows(quotes)
    tone = _tone(groups)
    signals = _signals(quotes, rates)
    age = _age_hours(snapshot.get("fetched_at"))
    return {
        "fetched_at": snapshot.get("fetched_at"),
        "age_hours": age,
        "stale": age is not None and age > _STALE_HOURS,
        "missing": list(snapshot.get("missing") or []),
        "groups": groups,
        "rates": rates,
        "tone": tone,
        "signals": signals,
        "advice": _advice(groups, tone, signals, list(snapshot.get("missing") or [])),
        "n_quotes": len(quotes),
    }
