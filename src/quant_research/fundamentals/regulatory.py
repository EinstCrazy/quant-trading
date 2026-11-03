"""Regulatory events: is this company under investigation *right now*?

A financial red flag says the numbers look wrong. This says the company is
already being looked at, which is a different and harder kind of problem: a
立案调查 freezes refinancing and restructuring approvals, terrifies suppliers
and lenders, and routinely ends in a restatement or a delisting. The price does
not always show it — the announcement is one line in a feed of three thousand.

There is no "currently under investigation" endpoint anywhere in the free data,
and the obvious place to look does not have one either: the CSRC site publishes
administrative penalties under 行政执法, but no 立案 column, and its list pages
are filled in by JavaScript rather than served as data. cninfo (巨潮资讯) is the
statutory disclosure platform those filings are published *through*, and it
supports a title search over a date range — so a year of filings costs about ten
requests instead of scanning two hundred and fifty trading days one at a time.

The state is derived, because no feed reports it directly:

    立案  →  investigation opens
    行政处罚决定书 / 终止调查 / 撤销立案  →  it closes

A name counts as under investigation when its latest opening has no closing
after it, and the opening is recent enough to still matter. Both halves of that
sentence are choices, and both are written down where the choice is made.

The titles are also noisy in a specific way worth knowing: 立案 also appears in
"法院立案受理" (a civil suit, not a regulator), and 调查 in "尽职调查" (an
adviser doing its job). Those are filtered out rather than merely tolerated,
because a false 立案 would exclude a healthy company from every screen.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.providers.base import ProviderError, ProviderUnavailable, normalize_symbol

__all__ = [
    "COMPANY",
    "INVESTIGATION",
    "PARTY",
    "RESOLUTION",
    "RegulatoryEvent",
    "active",
    "load",
    "load_manual",
    "merge",
    "search_events",
    "store",
]

INVESTIGATION = "investigation"
RESOLUTION = "resolution"

# cninfo's full-text endpoint, title mode. The statutory disclosure platform:
# every 立案告知书 a company is required to publish comes through here.
_CNINFO = "http://www.cninfo.com.cn/new/fulltextSearch/full"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "http://www.cninfo.com.cn/new/fulltextSearch",
}
# Beijing time, because the timestamps are epoch millis and a filing dated
# 2026-11-03 00:00 CST would otherwise land on 11-02 in UTC
_CST = timezone(timedelta(hours=8))

#: What to search for. One keyword per kind, title-only.
_OPEN_KEYWORDS = ("立案",)
_CLOSE_KEYWORDS = ("行政处罚决定书", "终止调查", "撤销立案")

# "立案" is the one word that appears in a CSRC filing notice and almost
# nowhere else. "调查" alone is useless: 尽职调查 shows up in every advisory
# opinion ever written.
_OPEN = re.compile("立案")
# A suit being accepted by a court is 立案受理 — and, in the wild, also
# 受理立案, which the first pattern misses. 纠纷 catches the variants that omit
# 受理 ("买卖合同纠纷案立案"). 立案审查/审查调查/留置 are the discipline
# commission's vocabulary, not the CSRC's, and a 留置 is not a securities case.
# 公安/侦查/检察: a criminal case is not a CSRC one either — "被公安机关立案
# 侦查" is a public-security matter, and only the regulator's case freezes
# refinancing.
_NOISE = re.compile(
    "立案受理|受理立案|纠纷|尽职调查|募集资金|三方|立案审查|审查调查|留置|公安|侦查|检察"
)
_CLOSE = re.compile("行政处罚决定书|结案|终止调查|撤销立案|不予处罚")
# A penalty from the finance ministry or the tax bureau is not the CSRC closing
# its case. Treating one as a closure hands a clean bill of health to a company
# still under investigation — the one direction of error worth avoiding here.
_OTHER_REGULATOR = re.compile("财政部|税务|环保|应急|海关|市场监管|银保监|人民银行|央行")

# Who the case is about. A 5% holder being investigated is not the company
# being investigated: excluding the stock for it would drop healthy names, and
# a good share of filings name only such a party.
#
# 控股股东 and 实际控制人 stay severe even when named alone, because their
# compliance gates the company's own refinancing and asset purchases — the same
# freeze a company-level case causes. A director or a minority holder does not.
#
# Note "公司关于" is *not* a useful test: it matches "…控股子公司关于…" as
# readily as "…公司关于…", and once mis-sorted a subsidiary's case as the
# issuer's.
_BLOCKER = re.compile("控股股东|实际控制人")
# "公司及股东" / "本公司" — the issuer is among the subjects
_INCLUDES_ISSUER = re.compile("公司及|公司、|本公司")
_PARTY = re.compile("股东|董事|监事|高管|高级管理人员|总经理|财务总监|个人|子公司|参股公司")

COMPANY = "company"
PARTY = "party"

_COLUMNS = ("symbol", "name", "kind", "date", "title", "url", "severity")
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class RegulatoryEvent:
    """One filing, either opening an investigation or closing one.

    ``severity`` separates "the issuer is under investigation" from "one of its
    shareholders or executives is". Both are worth knowing; only the first
    blocks a screen, because only the first stops the company itself from
    refinancing or restructuring.
    """

    symbol: str
    name: str
    kind: str
    date: str
    title: str
    url: str = ""
    severity: str = COMPANY

    @property
    def is_open(self) -> bool:
        return self.kind == INVESTIGATION

    @property
    def is_issuer(self) -> bool:
        return self.severity == COMPANY


def _requests() -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ProviderUnavailable(
            "requests is not installed. Install the collection extras with:\n"
            "    pip install -e '.[collect]'"
        ) from exc
    return requests


def _classify(title: str) -> str | None:
    if _CLOSE.search(title) and not _OTHER_REGULATOR.search(title):
        return RESOLUTION
    if _OPEN.search(title) and not _NOISE.search(title):
        return INVESTIGATION
    return None


def _severity(title: str) -> str:
    """Whether the filing is about the issuer or about somebody close to it."""
    if _BLOCKER.search(title):
        return COMPANY
    if _INCLUDES_ISSUER.search(title):
        return COMPANY
    if _PARTY.search(title):
        return PARTY
    return COMPANY


def _plain(text: str) -> str:
    """cninfo highlights the match with ``<em>``; the tag is not part of the title."""
    return _TAGS.sub("", text).strip()


def _day(stamp: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(stamp) / 1000, _CST).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _to_event(item: dict[str, Any]) -> RegulatoryEvent | None:
    title = _plain(str(item.get("announcementTitle", "")))
    kind = _classify(title)
    if kind is None:
        return None

    code = str(item.get("secCode", "")).strip()
    if not code.isdigit() or len(code) != 6:
        return None
    try:
        symbol = normalize_symbol(code)
    except ValueError:
        return None  # indexes and funds share the platform; not tradable names

    day = _day(item.get("announcementTime"))
    if day is None:
        return None

    adjunct = str(item.get("adjunctUrl", "") or "")
    return RegulatoryEvent(
        symbol=symbol,
        name=str(item.get("secName", "")).strip(),
        kind=kind,
        date=day,
        title=title,
        url=f"http://static.cninfo.com.cn/{adjunct}" if adjunct else "",
        severity=_severity(title),
    )


def _search_pages(
    keyword: str,
    start: date,
    end: date,
    page_size: int,
    max_pages: int,
    pause: float,
) -> list[dict[str, Any]]:
    """Every title containing ``keyword`` in [start, end], page by page."""
    requests = _requests()
    found: list[dict[str, Any]] = []
    total = None

    for page in range(1, max_pages + 1):
        params = {
            "searchkey": keyword,
            "sdate": start.isoformat(),
            "edate": end.isoformat(),
            "isfulltext": "false",
            "sortName": "nothing",
            "sortType": "desc",
            "pageNum": str(page),
            "pageSize": str(page_size),
        }
        try:
            response = requests.get(_CNINFO, params=params, headers=_HEADERS, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ProviderError(
                f"巨潮检索「{keyword}」第 {page} 页失败：{type(exc).__name__}: {exc}"
            ) from exc

        batch = payload.get("announcements") or []
        found.extend(batch)
        total = int(payload.get("totalAnnouncement") or 0)
        if not batch or len(found) >= total:
            break
        if pause:
            time.sleep(pause)

    if total is not None and len(found) < total:
        # a truncated search would look like "these are all the cases this year"
        raise ProviderError(
            f"巨潮检索「{keyword}」只取到 {len(found)}/{total} 条，"
            f"未翻完 {max_pages} 页上限；请调大 regulatory.max_pages"
        )
    return found


def search_events(
    days: int = 365,
    today: date | None = None,
    page_size: int = 50,
    max_pages: int = 40,
    pause: float = 0.0,
) -> list[RegulatoryEvent]:
    """Filings that open or close an investigation over the last ``days`` days.

    A year by default, because that is how long a case can stay open — a sixty
    day window would miss the companies that matter most, the ones put under
    investigation long ago and never cleared.
    """
    if days < 1:
        raise ValueError("days must be positive")
    moment = today or date.today()
    start = moment - timedelta(days=days)

    seen: dict[tuple[str, str, str, str], RegulatoryEvent] = {}
    for keyword in (*_OPEN_KEYWORDS, *_CLOSE_KEYWORDS):
        for item in _search_pages(keyword, start, moment, page_size, max_pages, pause):
            event = _to_event(item)
            if event is None:
                continue
            # one filing matches several keywords; keep it once
            seen.setdefault((event.symbol, event.kind, event.date, event.title), event)
    return sorted(seen.values(), key=lambda event: (event.date, event.symbol))


# --- storage ---------------------------------------------------------------


def _path(directory: Path | str) -> Path:
    return Path(directory) / "events.parquet"


def store(events: list[RegulatoryEvent], directory: Path | str) -> Path:
    """Append to the store, keeping every filing seen so far.

    Appending rather than replacing is the point: the search window is a year,
    but the history you accumulate is not. Shrinking the window later must not
    silently resurrect a company whose case was found under a wider one.
    """
    target = _path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    fresh = pd.DataFrame([event.__dict__ for event in events], columns=list(_COLUMNS))
    if target.is_file():
        old = pd.read_parquet(target)
        fresh = pd.concat([old, fresh], ignore_index=True)
    fresh = fresh.drop_duplicates(subset=["symbol", "kind", "date", "title"], keep="last")
    fresh = fresh.sort_values(["date", "symbol"]).reset_index(drop=True)
    fresh.to_parquet(target)
    return target


def load(directory: Path | str) -> pd.DataFrame:
    target = _path(directory)
    if not target.is_file():
        return pd.DataFrame(columns=list(_COLUMNS))
    frame = pd.read_parquet(target)
    if "severity" not in frame.columns:
        # a store written before severity existed: every filing in it was kept
        # under the issuer rule, which is what that rule used to be
        frame["severity"] = COMPANY
    return frame


def merge(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union of several stores, newest filing last."""
    present = [frame for frame in frames if frame is not None and not frame.empty]
    if not present:
        return pd.DataFrame(columns=list(_COLUMNS))
    combined = pd.concat(present, ignore_index=True)
    combined = combined.drop_duplicates(subset=["symbol", "kind", "date", "title"], keep="last")
    return combined.sort_values(["date", "symbol"]).reset_index(drop=True)


# --- manual list -----------------------------------------------------------


def load_manual(path: Path | str | None) -> pd.DataFrame:
    """Facts the search cannot see: a case opened before the window.

    A year covers most open cases. It does not cover the ones that started
    earlier and never closed, and those are exactly the names that matter.
    """
    if path is None:
        return pd.DataFrame(columns=list(_COLUMNS))
    target = Path(path)
    if not target.is_file():
        return pd.DataFrame(columns=list(_COLUMNS))
    import yaml

    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    rows = payload.get("investigations") or []
    out = []
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        out.append(
            {
                "symbol": normalize_symbol(symbol),
                "name": str(row.get("name", "") or ""),
                "kind": INVESTIGATION,
                "date": str(row.get("date", "") or ""),
                "title": str(row.get("note", "") or "手工登记"),
                "url": "",
                "severity": COMPANY if row.get("issuer", True) else PARTY,
            }
        )
    return pd.DataFrame(out, columns=list(_COLUMNS))


# --- the state -------------------------------------------------------------


def active(
    frame: pd.DataFrame,
    as_of: date | str | None = None,
    within_days: int = 365,
) -> dict[str, RegulatoryEvent]:
    """Names whose latest 立案 has no closing after it and is still recent.

    ``within_days`` bounds how long an open case keeps a name excluded. The
    CSRC has no statutory deadline, and cases have run for years; a year is a
    deliberate compromise — long enough that nothing falls out mid-investigation,
    short enough that a company cleared in silence is not banned forever.
    """
    if frame is None or frame.empty:
        return {}

    limit = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(date.today())
    horizon = limit - pd.Timedelta(days=within_days)

    work = frame.copy()
    work["stamp"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["stamp"])
    # an announcement dated in the future is a data error, not a scoop
    work = work[work["stamp"] <= limit]
    if work.empty:
        return {}

    found: dict[str, RegulatoryEvent] = {}
    for symbol, own in work.groupby("symbol", sort=False):
        opens = own[own["kind"] == INVESTIGATION].sort_values("stamp")
        if opens.empty:
            continue
        latest_open = opens.iloc[-1]
        closes = own[(own["kind"] == RESOLUTION) & (own["stamp"] >= latest_open["stamp"])]
        if not closes.empty:
            continue
        if latest_open["stamp"] < horizon:
            continue
        found[str(symbol)] = RegulatoryEvent(
            symbol=str(symbol),
            name=str(latest_open.get("name", "")),
            kind=INVESTIGATION,
            date=str(latest_open["stamp"].date()),
            title=str(latest_open.get("title", "")),
            url=str(latest_open.get("url", "") or ""),
            severity=str(latest_open.get("severity", COMPANY)),
        )
    return found
