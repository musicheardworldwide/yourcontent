"""In-memory jobs, workers "gpu" and "ffmpeg", cancel (§4). Owner: WP2.

Each queue has one worker thread, so our own GPU jobs never overlap and renders run one at a time. Jobs live
in memory: a restart loses them, and `get()` of an id this process never made answers state "error" with
"YourContent restarted; this job was lost", so a reloaded page stops waiting.

A job function is `fn(job) -> dict`. It reports with `job.progress(p, phase)`, checks `job.cancelled`, and
registers what cancel must stop: `job.set_proc(popen)` (start it with start_new_session=True so the whole
process group is killed) and `job.set_comfy(pid)` (cancel then calls comfy.cancel(pid)).
"""
from __future__ import annotations

import os
import queue as _queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable

JID_RE = re.compile(r"^j_[0-9a-f]{12}$")
QUEUES = ("gpu", "ffmpeg")
MAX_QUEUED = 10          # §12.8: 10 queued jobs, then 429
KEEP_FINISHED = 200      # finished jobs remembered for polling
LOST = "YourContent restarted; this job was lost"


class QueueFull(RuntimeError):
    """Too many queued jobs. HTTP 429."""
    status = 429


class Cancelled(RuntimeError):
    """Raised by commit() when a cancel came before the job's result was kept."""


class Job:
    """Handed to a job function `fn(job)`. tests/util.FakeJob mirrors this interface."""

    def __init__(self, jid: str, queue: str, kind: str, label: str, supersede: str | None = None):
        self.id = jid
        self.queue = queue
        self.kind = kind
        self.label = label
        self.supersede = supersede
        self.state = "queued"
        self.phase = "queued"
        self._progress: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self._cancel = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._comfy: list[str] = []
        self._committed = False
        self._lock = threading.Lock()

    # --- interface for job functions
    def progress(self, p: float | None, phase: str) -> None:
        with self._lock:
            if p is not None:
                try:
                    p = max(0.0, min(1.0, float(p)))
                except (TypeError, ValueError):
                    p = None
            self._progress = p
            self.phase = str(phase)[:40]

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def commit(self, can_discard: bool = True) -> None:
        """Call right before the result is kept for good (os.replace, sidecar write). With can_discard, a cancel
        that came first raises Cancelled (the caller removes its temporaries). After this, a late cancel cannot
        stop anything, so the job ends "done" rather than "cancelled" while its file exists."""
        with self._lock:
            if can_discard and self._cancel.is_set():
                raise Cancelled("cancelled")
            self._committed = True

    @property
    def committed(self) -> bool:
        return self._committed

    def set_proc(self, popen: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(popen)
        if self.cancelled:
            _kill(popen)

    def set_comfy(self, pid: str) -> None:
        with self._lock:
            self._comfy.append(pid)
        if self.cancelled:
            _comfy_cancel(pid)

    # --- internal
    def request_cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            procs, pids = list(self._procs), list(self._comfy)
        for p in procs:
            _kill(p)
        for pid in pids:
            _comfy_cancel(pid)

    def view(self) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "state": self.state, "phase": self.phase,
                    "progress": self._progress, "label": self.label, "result": self.result, "error": self.error}


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            p.kill()
        except OSError:
            pass


def _comfy_cancel(pid: str) -> None:
    try:
        from . import comfy
        comfy.cancel(pid)
    except Exception:  # noqa: BLE001 - ComfyUI off or already finished: nothing to stop
        pass


_lock = threading.RLock()
_jobs: dict[str, Job] = {}
_order: list[str] = []
_queues: dict[str, _queue.Queue] = {}
_workers: dict[str, threading.Thread] = {}


def _worker(name: str) -> None:
    q = _queues[name]
    while True:
        job = q.get()
        if job is None:  # reset() sentinel
            return
        # check and transition under job._lock: cancel()/submit() finish a queued job under the same lock,
        # so exactly one side wins (a job reported cancelled never flips back to running)
        with job._lock:
            fn = job._fn  # type: ignore[attr-defined]
            start = job.state == "queued" and not job.cancelled and fn is not None
            if start:
                job.state, job.started = "running", time.time()
                if job.phase == "queued":
                    job.phase = "starting"
        if not start:
            _cancel_if_queued(job)
            continue
        try:
            result = fn(job)
            if job.cancelled and not job.committed:
                _finish(job, "cancelled")
            else:
                job.result = result if isinstance(result, dict) else {"value": result}
                _finish(job, "done")
        except Exception as exc:  # noqa: BLE001 - every failure becomes a plain error on the job
            if (job.cancelled and not job.committed) or type(exc).__name__ == "Cancelled":  # media/comfy/jobs
                _finish(job, "cancelled")
            else:
                job.error = plain_error(exc)
                if not isinstance(exc, (ValueError, LookupError, NotImplementedError)) and not hasattr(exc, "status"):
                    traceback.print_exc(file=sys.stderr)
                _finish(job, "error")


def commit(job, can_discard: bool = True) -> None:
    """job.commit() for a job function's `job`, which may be None (CLI) or a test fake without commit()."""
    fn = getattr(job, "commit", None)
    if fn is not None:
        fn(can_discard)


def plain_error(exc: BaseException) -> str:
    if isinstance(exc, NotImplementedError):
        return "this feature isn't ready yet"
    from .store import scrub
    return scrub(str(exc).strip() or "something went wrong")[:500]


def _finish(job: Job, state: str) -> None:
    with job._lock:
        _finish_locked(job, state)
    _prune()


def _finish_locked(job: Job, state: str) -> None:
    job.state, job.ended = state, time.time()
    if state == "done":
        job._progress, job.phase = 1.0, "done"
    elif state == "cancelled":
        job.phase = "cancelled"
    elif state == "error":
        job.phase = "error"
    job._fn = None  # type: ignore[attr-defined]


def _cancel_if_queued(job: Job) -> bool:
    """Finish a still-queued job as cancelled, atomically against the worker's queued->running step."""
    with job._lock:
        done = job.state == "queued"
        if done:
            _finish_locked(job, "cancelled")
    if done:
        _prune()
    return done


def _prune() -> None:
    with _lock:
        finished = [j for j in _order if _jobs[j].state in ("done", "error", "cancelled")]
        for jid in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            _order.remove(jid)
            _jobs.pop(jid, None)


def _ensure_worker(name: str) -> None:
    t = _workers.get(name)
    if t is None or not t.is_alive():
        _queues.setdefault(name, _queue.Queue())
        t = threading.Thread(target=_worker, args=(name,), name=f"medialab-{name}", daemon=True)
        _workers[name] = t
        t.start()


def submit(queue: str, kind: str, fn: Callable[[Job], dict], *, label: str, supersede: str | None = None) -> str:
    """Queue fn on a worker; returns the job id. With supersede (e.g. "preview:p_…"), an older queued or
    running job with the same key is cancelled. Raises QueueFull past 10 queued jobs."""
    if queue not in QUEUES:
        raise ValueError(f"unknown queue {queue!r}")
    with _lock:
        if supersede:
            for jid in _order:
                old = _jobs[jid]
                if old.supersede == supersede and old.state in ("queued", "running"):
                    old.request_cancel()
                    _cancel_if_queued(old)   # finish it now: drop its closure, and it no longer looks queued
        waiting = sum(1 for j in _jobs.values() if j.state == "queued" and not j.cancelled)
        if waiting >= MAX_QUEUED:
            raise QueueFull("too many jobs waiting; try again when some finish")
        jid = "j_" + secrets.token_hex(6)
        job = Job(jid, queue, kind, str(label or kind)[:120], supersede)
        job._fn = fn  # type: ignore[attr-defined]
        _jobs[jid] = job
        _order.append(jid)
        _ensure_worker(queue)
        _queues[queue].put(job)
    return jid


def get(jid: str) -> dict:
    """The job's view. An unknown but well-formed id answers state "error" (lost in a restart)."""
    if not isinstance(jid, str) or not JID_RE.fullmatch(jid):
        from .types import BadRef
        raise BadRef("that job id isn't valid")
    with _lock:
        job = _jobs.get(jid)
    if job is None:
        return {"id": jid, "kind": None, "state": "error", "phase": "lost", "progress": None, "label": "",
                "result": None, "error": LOST}
    return job.view()


def active() -> list[dict]:
    """Queued and running jobs, oldest first (a reloaded page resumes its tickers)."""
    with _lock:
        jobs = [_jobs[j] for j in _order]
    return [j.view() for j in jobs if j.state in ("queued", "running")]


def cancel(jid: str) -> str:
    """Ask a job to stop. A queued job is cancelled at once; a running one stops its process / ComfyUI prompt
    and turns "cancelled" when its function returns, unless it had already committed its result (then "done",
    since the file it made is kept). Returns the state after the request."""
    get(jid)  # validates the id
    with _lock:
        job = _jobs.get(jid)
    if job is None or job.state in ("done", "error", "cancelled"):
        return job.state if job else "error"
    job.request_cancel()
    _cancel_if_queued(job)
    return job.state


def wait(jid: str, timeout: float = 10.0) -> dict:
    """Block until a job finishes (tests and the CLI)."""
    end = time.time() + timeout
    while time.time() < end:
        v = get(jid)
        if v["state"] in ("done", "error", "cancelled"):
            return v
        time.sleep(0.01)
    return get(jid)


def reset() -> None:
    """Forget every job and stop the workers (tests)."""
    with _lock:
        for j in _jobs.values():
            j.request_cancel()
        for name, q in _queues.items():
            q.put(None)
        _jobs.clear()
        _order.clear()
        _queues.clear()
        _workers.clear()
