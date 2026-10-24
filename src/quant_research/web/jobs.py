"""In-process job queue for the workbench.

A backtest over the full A-share universe, or a backfill of thousands of symbols,
takes minutes. Running that inside the request thread freezes the browser and gives
no feedback at all, so long operations are submitted as jobs: the client gets an id
back immediately and polls for progress.

Deliberately small: one process, a two-thread pool, results kept in memory and
archived as JSON. This is a single-user tool on one host — a real queue (Celery,
RQ) would be a different answer to a different problem.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_research.logging_setup import get_logger

logger = get_logger("web.jobs")

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
ARCHIVE_DIR = Path("reports/webui")
MAX_JOBS = 50


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Progress:
    """What a task sees: a way to report progress, nothing else."""

    _job: Job
    _lock: threading.Lock

    def update(self, done: int, message: str = "") -> None:
        with self._lock:
            self._job.progress_done = done
            if message:
                self._job.message = message


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "pending"
    progress_done: int = 0
    progress_total: int = 1
    message: str = ""
    submitted_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: Any | None = None
    error: str | None = None
    archive: str | None = None
    # True for a job that came back from an earlier process: no live state,
    # and the result stays on disk until someone asks for that one job.
    archived: bool = False

    def to_dict(self, *, with_result: bool = False) -> dict[str, Any]:
        total = self.progress_total or 1
        payload = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "progress": {
                "done": self.progress_done,
                "total": self.progress_total,
                "ratio": round(min(1.0, self.progress_done / total), 3),
            },
            "message": self.message,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "archive": self.archive,
            "archived": self.archived,
        }
        if with_result:
            payload["result"] = self.result
        return payload


# A backtest or collection returns one object; a comparison returns a list of rows.
TaskFn = Callable[[Progress, Callable[[], bool]], Any]


class JobStore:
    """Submit, track and cancel jobs. All state is in this process."""

    def __init__(
        self,
        workers: int = 2,
        archive_dir: Path | None = ARCHIVE_DIR,
        max_jobs: int = MAX_JOBS,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._cancel: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._archive_dir = archive_dir
        self._max_jobs = max_jobs
        self._archived: dict[str, Job] = {}
        if archive_dir is not None:
            self._archived = self._load_archived()

    # --- submission ----------------------------------------------------------

    def submit(self, kind: str, label: str, task: TaskFn, total: int = 1) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            label=label,
            status="pending",
            progress_total=max(1, total),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._cancel[job.id] = threading.Event()
            self._trim()

        self._pool.submit(self._run, job.id, task)
        return job

    def _run(self, job_id: str, task: TaskFn) -> None:
        job = self._jobs[job_id]
        cancelled = self._cancel[job_id]
        job.status = "running"
        job.started_at = _now()

        try:
            result = task(Progress(job, self._lock), cancelled.is_set)
        except Exception as exc:  # a failed job must not kill the worker
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("job %s (%s) failed", job_id, job.kind)
        else:
            # keep whatever the task produced: a stopped collection still reports
            # the symbols it finished, which is the point of stopping early
            job.result = result
            if cancelled.is_set():
                # progress stays where it stopped; claiming 100% would hide that
                job.status = "cancelled"
            else:
                # settle everything before the job becomes visible as done, so a
                # poller never sees a finished job whose result is not yet stored
                job.progress_done = job.progress_total
                job.archive = self._archive(job)
                job.status = "done"
        finally:
            job.finished_at = _now()
            self._cancel.pop(job_id, None)

    # --- reading -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        job = self._archived.get(job_id)
        if job is None:
            return None
        return self._read_result(job)

    def list(self, limit: int = 20) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))
        live = [self._jobs[i] for i in ids if i in self._jobs]
        restored = [job for key, job in self._archived.items() if key not in self._jobs]
        restored.sort(key=lambda job: job.submitted_at, reverse=True)
        return (live + restored)[:limit]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        event = self._cancel.get(job_id)
        if event is None:
            return False
        event.set()
        job.message = "取消请求已收到，稍后停止"
        return True

    # --- internals -----------------------------------------------------------

    def _load_archived(self) -> dict[str, Job]:
        """Read back jobs written by earlier runs of this process.

        Only the metadata comes back. Results stay on disk until someone opens a
        specific job, so a long history costs a few hundred bytes per job rather
        than a full backtest payload each.
        """
        jobs: dict[str, Job] = {}
        if self._archive_dir is None:
            return jobs
        try:
            paths = sorted(self._archive_dir.glob("*.json"), reverse=True)
        except OSError as exc:
            logger.warning("cannot read archive dir %s: %s", self._archive_dir, exc)
            return jobs

        for path in paths[: self._max_jobs]:
            try:
                meta = json.loads(path.read_text(encoding="utf-8")).get("job") or {}
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("skipping unreadable archive %s: %s", path.name, exc)
                continue
            progress = meta.get("progress") or {}
            job_id = meta.get("id") or path.stem
            jobs[job_id] = Job(
                id=job_id,
                kind=meta.get("kind", "unknown"),
                label=meta.get("label", path.stem),
                status=meta.get("status", "done"),
                progress_done=progress.get("done", 0),
                progress_total=progress.get("total", 1),
                message=meta.get("message", ""),
                submitted_at=meta.get("submitted_at", ""),
                started_at=meta.get("started_at"),
                finished_at=meta.get("finished_at"),
                error=meta.get("error"),
                archive=str(path),
                archived=True,
            )
        return jobs

    def _read_result(self, job: Job) -> Job:
        """Pull one archived result off disk, once, when it is asked for."""
        if job.result is not None or not job.archive:
            return job
        try:
            payload = json.loads(Path(job.archive).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            job.error = f"archive unreadable: {exc}"
            return job
        job.result = payload.get("result")
        return job

    def _trim(self) -> None:
        """Keep the newest jobs; older ones are already archived to disk."""
        while len(self._order) > self._max_jobs:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)
            self._cancel.pop(oldest, None)

    def _archive(self, job: Job) -> str | None:
        if self._archive_dir is None or job.result is None:
            return None
        try:
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            path = self._archive_dir / f"{job.id}_{job.kind}.json"
            meta = job.to_dict()
            # archiving happens just before the job is marked done, so the live
            # snapshot still says "running"; the file must not inherit that
            meta["status"] = "done"
            payload = {"job": meta, "result": job.result}
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            return str(path)
        except OSError as exc:
            logger.warning("could not archive job %s: %s", job.id, exc)
            return None


_store: JobStore | None = None
_store_lock = threading.Lock()


def get_store() -> JobStore:
    """Process-wide store, so jobs survive across requests."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = JobStore()
    return _store


def reset_store(store: JobStore | None = None) -> None:
    """Replace the global store; tests use this to get a clean, unarchived one."""
    global _store
    with _store_lock:
        _store = store or JobStore(archive_dir=None)


__all__ = ["Job", "JobStore", "Progress", "TaskFn", "get_store", "reset_store"]
