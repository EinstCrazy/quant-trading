"""Regulatory events: is this company under investigation *right now*?

A financial red flag says the numbers look wrong. This says the company is
already being looked at, which is a different and harder kind of problem: a
立案调查 freezes refinancing and restructuring approvals, terrifies suppliers
and lenders, and routinely ends in a restatement or a delisting. The price does
not always show it — the announcement is one line in a feed of three thousand.

There is no "currently under investigation" endpoint anywhere in the free data.
What exists is the announcement stream, so the state has to be **derived**:

    立案  →  investigation opens
    行政处罚决定书 / 结案 / 终止调查  →  it closes

A name counts as under investigation when its latest opening has no closing
after it, and the opening is recent enough to still matter. Both halves of that
sentence are choices, and both are written down where the choice is made.

The stream is also noisy in a specific way worth knowing: 立案 also appears in
"法院立案受理" (a civil suit, not a regulator) and 调查 in "尽职调查" (an
adviser doing its job). Those are filtered out rather than merely tolerated,
because a false 立案 would exclude a healthy company from every screen.
"""

from __future__ import annotations

import io
import json
import re
import time
from contextlib import redirect_stderr
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from quant_research.data.providers.base import ProviderUnavailable, normalize_symbol

__all__ = [
    "INVESTIGATION",
    "RESOLUTION",
    "RegulatoryEvent",
    "ScanOutcome",
    "active",
    "load",
    "load_manual",
    "load_state",
    "merge",
    "recent_sessions",
    "save_state",
    "scan",
    "store",
]

INVESTIGATION = "investigation"
RESOLUTION = "resolution"

# "立案" is the one word that appears in a CSRC filing notice and almost
# nowhere else. "调查" alone is useless: 尽职调查 shows up in every advisory
# opinion ever written.
_OPEN = re.compile("立案")
# A suit being accepted by a court is 立案受理; it has nothing to do with the
# regulator and would otherwise flag half the companies in litigation. 纠纷
# catches the variants that omit 受理 ("买卖合同纠纷案立案").
_NOISE = re.compile("立案受理|纠纷|尽职调查|募集资金|三方")
_CLOSE = re.compile("行政处罚决定书|结案|终止调查|撤销立案|不予处罚")
# A penalty from the finance ministry or the tax bureau is not the CSRC closing
# its case. Treating one as a closure hands a clean bill of health to a company
# still under investigation — the one direction of error worth avoiding here.
_OTHER_REGULATOR = re.compile("财政部|税务|环保|应急|海关|市场监管|银保监|人民银行|央行")

_COLUMNS = ("symbol", "name", "kind", "date", "title", "url")


@dataclass(frozen=True)
class RegulatoryEvent:
    """One filing, either opening an investigation or closing one."""

    symbol: str
    name: str
    kind: str
    date: str
    title: str
    url: str = ""

    @property
    def is_open(self) -> bool:
        return self.kind == INVESTIGATION


@dataclass(frozen=True)
class ScanOutcome:
    """What a scan produced, and which days it could not read.

    ``scanned`` excludes ``failed`` on purpose: the state file is written from
    this list, so a day that errored is retried next run instead of being
    silently marked done. One bad response must not cost the other forty days.
    """

    events: list[RegulatoryEvent]
    scanned: list[str]
    failed: list[str]


def _akshare() -> Any:
    try:
        import akshare
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ProviderUnavailable(
            "akshare is not installed. Install the collection extras with:\n"
            "    pip install -e '.[collect]'"
        ) from exc
    return akshare


def recent_sessions(days: int, today: date | None = None) -> list[str]:
    """Weekdays in the last ``days`` calendar days, newest first.

    Weekdays, not trading days: a public holiday costs one wasted request and
    no correctness, whereas a holiday calendar costs a dependency that goes
    stale every year.
    """
    if days < 1:
        raise ValueError("days must be positive")
    moment = today or date.today()
    out: list[str] = []
    for offset in range(days):
        day = moment - timedelta(days=offset)
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
    return out


def _classify(title: str) -> str | None:
    if _CLOSE.search(title) and not _OTHER_REGULATOR.search(title):
        return RESOLUTION
    if _OPEN.search(title) and not _NOISE.search(title):
        return INVESTIGATION
    return None


def scan(
    dates: list[str],
    pause: float = 0.0,
    skip: set[str] | None = None,
) -> ScanOutcome:
    """Read the whole-market announcement feed for each date and keep filings.

    One request per date returns every announcement in the market that day —
    three thousand rows — which is why ``skip`` exists: a store that already
    covers a date should never pay for it twice.

    A date that errors or comes back in an unexpected shape is recorded as
    failed and retried next run, instead of aborting the other forty days and
    losing the work. One bad response costing a whole scan is how a sixty-day
    first run never finishes.
    """
    if not dates:
        return ScanOutcome(events=[], scanned=[], failed=[])
    akshare = _akshare()
    covered = skip or set()
    events: list[RegulatoryEvent] = []
    scanned: list[str] = []
    failed: list[str] = []

    for day in dates:
        if day in covered:
            continue
        try:
            # akshare renders a tqdm bar per page over a few dozen pages;
            # left alone it buries the actual report
            with redirect_stderr(io.StringIO()):
                frame = akshare.stock_notice_report(symbol="全部", date=day)
        except Exception:  # noqa: BLE001 - one bad date must not stop the scan
            failed.append(day)
            continue

        if frame is None:
            failed.append(day)
            continue
        if frame.empty:
            scanned.append(day)  # a holiday answering "nothing" is a result
            continue
        if {"代码", "公告标题"} - set(frame.columns):
            # the API changed shape or handed back an error page; retrying
            # tomorrow is cheaper than guessing what the columns meant
            failed.append(day)
            continue

        scanned.append(day)
        titles = frame["公告标题"].fillna("")
        kinds = titles.map(_classify)
        keep = kinds.notna()
        subset = frame[keep]
        for position, (_, row) in enumerate(subset.iterrows()):
            code = str(row.get("代码", "")).strip()
            if not code.isdigit() or len(code) != 6:
                continue
            try:
                symbol = normalize_symbol(code)
            except ValueError:
                continue  # indexes and funds share the feed; they are not tradable names
            events.append(
                RegulatoryEvent(
                    symbol=symbol,
                    name=str(row.get("名称", "")).strip(),
                    kind=str(kinds[keep].iloc[position]),
                    date=str(row.get("公告日期", day)),
                    title=str(row.get("公告标题", "")).strip(),
                    url=str(row.get("网址", "") or ""),
                )
            )
        if pause:
            time.sleep(pause)

    return ScanOutcome(events=events, scanned=scanned, failed=failed)


# --- storage ---------------------------------------------------------------


def _path(directory: Path | str) -> Path:
    return Path(directory) / "events.parquet"


def store(events: list[RegulatoryEvent], directory: Path | str) -> Path:
    """Append to the store, keeping every filing seen so far.

    Appending rather than replacing is the whole point: the state "under
    investigation since March" cannot be reconstructed from a thirty-day
    window, so the history has to accumulate across runs.
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
    return pd.read_parquet(target)


def merge(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union of several stores, newest filing last."""
    present = [frame for frame in frames if frame is not None and not frame.empty]
    if not present:
        return pd.DataFrame(columns=list(_COLUMNS))
    combined = pd.concat(present, ignore_index=True)
    combined = combined.drop_duplicates(subset=["symbol", "kind", "date", "title"], keep="last")
    return combined.sort_values(["date", "symbol"]).reset_index(drop=True)


def load_state(directory: Path | str) -> set[str]:
    """Days already scanned, whether or not they produced a filing.

    Derived from the event dates this would be wrong: six scanned days that
    yielded five event dates would leave one day to be scanned again forever.
    A day with no 立案 is a result, not a gap.
    """
    target = Path(directory) / "scan_state.json"
    if not target.is_file():
        return set()
    payload = json.loads(target.read_text(encoding="utf-8"))
    return {str(day) for day in payload.get("dates", [])}


def save_state(days: list[str], directory: Path | str) -> Path:
    """Remember which days are done, so the next run only fetches new ones."""
    target = Path(directory) / "scan_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = sorted(load_state(directory) | {str(day) for day in days})
    # keep the file from growing without bound: older days are re-derivable
    # from the store, and a year of ISO dates is still small
    target.write_text(
        json.dumps({"dates": merged[-400:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


# --- manual list -----------------------------------------------------------


def load_manual(path: Path | str | None) -> pd.DataFrame:
    """Facts the scanner cannot see: a case opened before the scan window.

    The store only knows what it watched. A company put under investigation in
    2024 and still waiting is invisible to a sixty-day scan, and pretending
    otherwise would give a false all-clear on exactly the names that matter.
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
        )
    return found
