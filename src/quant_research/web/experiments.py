"""A ledger of backtests worth keeping.

Job history answers "what did I run"; it is a log of submissions, ordered by time
and addressed by id. Research needs a different question: "which configurations
have I tried, and what did they return?" — so every backtest also lands here, as
one line carrying its configuration and its headline metrics.

Deliberately thin. Only what a table needs to compare runs is stored: no curves, no
trades. Those stay in the job result, reachable through ``job_id``; a run made
through the synchronous endpoint has no job, and shows up here without one. The
point is to see fifty attempts at once and pick the two worth reopening — not to
build a second copy of every result.

Notes are the reason this is a ledger rather than a log: a number alone does not
record why a run was abandoned.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_research.logging_setup import get_logger

logger = get_logger("web.experiments")

INDEX_PATH = Path("reports/experiments/index.jsonl")

# Metrics offered for sorting; anything else falls back to submission order.
SORTABLE_METRICS = ("sharpe", "cagr", "cumulative_return", "max_drawdown", "calmar")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Experiment:
    id: str
    created_at: str
    dataset: str
    strategy: str
    metrics: dict[str, Any] = field(default_factory=dict)
    final_equity: float = 0.0
    trade_count: int = 0
    total_costs: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    weight_method: str | None = None
    rebalance: dict[str, Any] | None = None
    # Present when the run went through the job queue: opens the full result.
    job_id: str | None = None
    label: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sort_key(value: Any) -> tuple[bool, float]:
    """Order by metric, but keep "could not be computed" at the bottom.

    Not 0.0: a metric that could not be computed is unknown, and sorting unknown
    next to a genuine zero misrepresents both.
    """
    if not isinstance(value, (int, float)):
        return (False, 0.0)
    return (True, float(value))


class ExperimentStore:
    """Append-only index, rewritten in place when a line changes."""

    def __init__(self, path: Path | None = INDEX_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path | None:
        return self._path

    def record(
        self,
        *,
        dataset: str,
        strategy: str,
        metrics: dict[str, Any],
        final_equity: float,
        trade_count: int,
        total_costs: float,
        params: dict[str, Any] | None = None,
        weight_method: str | None = None,
        rebalance: dict[str, Any] | None = None,
        job_id: str | None = None,
        label: str = "",
    ) -> Experiment | None:
        """Append one run. Returns None when the ledger is disabled."""
        experiment = Experiment(
            id=job_id or uuid.uuid4().hex[:12],
            created_at=_now(),
            dataset=dataset,
            strategy=strategy,
            metrics=dict(metrics),
            final_equity=float(final_equity),
            trade_count=int(trade_count),
            total_costs=float(total_costs),
            params=dict(params or {}),
            weight_method=weight_method,
            rebalance=dict(rebalance) if rebalance else None,
            job_id=job_id,
            label=label,
        )
        with self._lock:
            items = self._read_all()
            items.append(experiment)
            self._write_all(items)
        return experiment

    def recent(
        self,
        *,
        dataset: str | None = None,
        strategy: str | None = None,
        sort: str = "created_at",
        limit: int = 50,
    ) -> list[Experiment]:
        """Experiments matching the filters, newest or best first.

        Named ``recent`` rather than ``list``: inside this class, a method called
        ``list`` would shadow the builtin in every annotation below it.
        """
        items = self._read_all()
        if dataset:
            items = [item for item in items if item.dataset == dataset]
        if strategy:
            items = [item for item in items if item.strategy == strategy]

        if sort in SORTABLE_METRICS:
            items.sort(key=lambda item: sort_key(item.metrics.get(sort)), reverse=True)
        else:
            items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    def get(self, experiment_id: str) -> Experiment | None:
        for item in self._read_all():
            if item.id == experiment_id:
                return item
        return None

    def update(
        self, experiment_id: str, *, label: str | None = None, note: str | None = None
    ) -> Experiment | None:
        with self._lock:
            items = self._read_all()
            for item in items:
                if item.id == experiment_id:
                    if label is not None:
                        item.label = label
                    if note is not None:
                        item.note = note
                    self._write_all(items)
                    return item
        return None

    def delete(self, experiment_id: str) -> bool:
        with self._lock:
            items = self._read_all()
            kept = [item for item in items if item.id != experiment_id]
            if len(kept) == len(items):
                return False
            self._write_all(kept)
            return True

    # --- storage -------------------------------------------------------------

    def _read_all(self) -> list[Experiment]:
        if self._path is None or not self._path.exists():
            return []
        items: list[Experiment] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(Experiment(**json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("skipping malformed experiment line %d: %s", number, exc)
        return items

    def _write_all(self, items: list[Experiment]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(
            json.dumps(item.to_dict(), ensure_ascii=False, default=str) for item in items
        )
        # Write-then-replace: a crash mid-write must not truncate the ledger.
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
        tmp.replace(self._path)


_store: ExperimentStore | None = None
_store_lock = threading.Lock()


def get_store() -> ExperimentStore:
    """Process-wide ledger.

    Reads on every call, so an entry written by another process (the CLI, a second
    workbench) shows up without a restart.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ExperimentStore()
    return _store


def reset_store(path: Path | None = None) -> None:
    """Point the ledger somewhere else; tests use this to keep the repo clean."""
    global _store
    with _store_lock:
        _store = ExperimentStore(path)


__all__ = ["Experiment", "ExperimentStore", "get_store", "reset_store"]
