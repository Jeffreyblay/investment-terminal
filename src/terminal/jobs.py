"""Background extraction jobs for the web API.

Reading a 10-K with the local LLM takes minutes, so the API doesn't do it inside a request.
`JobManager.submit()` queues a company and returns at once; one worker thread processes the queue
in order (the GPU runs one model call at a time anyway), and `status()` reports progress that the
Explorer polls to show "reading passage 12/30" and to redraw the network as relationships arrive.

Jobs live in memory: restarting `itx serve` forgets the queue, but everything already extracted
is in SQLite, and companies are marked as processed only when their filing is finished.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable


class JobManager:
    def __init__(self, make_edgar: Callable, make_store: Callable, make_llm: Callable,
                 make_resolver: Callable, max_passages: int = 30):
        self._make_edgar, self._make_store = make_edgar, make_store
        self._make_llm, self._make_resolver = make_llm, make_resolver
        self.max_passages = max_passages
        self._jobs: dict[str, dict] = {}            # entity_id -> job state
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ---------- public ----------
    def submit(self, entity_id: str, ticker: str, refresh: bool = False) -> dict:
        """Queue a company unless it's already queued/running (or done, without refresh)."""
        with self._lock:
            job = self._jobs.get(entity_id)
            if job and job["status"] in ("queued", "running"):
                return dict(job)
            if job and job["status"] == "done" and not refresh:
                return dict(job)
            job = {"entity_id": entity_id, "ticker": ticker, "status": "queued", "step": "queued",
                   "done": 0, "total": 0, "found": 0, "error": None, "refresh": refresh,
                   "queued_at": time.time(), "started_at": None, "finished_at": None}
            self._jobs[entity_id] = job
            self._queue.put(entity_id)
            self._ensure_worker()
            return dict(job)

    def status(self, entity_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(entity_id)
            if not job:
                return None
            out = dict(job)
            if job["status"] == "queued":   # how many jobs are ahead of this one
                out["ahead"] = sum(1 for j in self._jobs.values()
                                   if j["status"] in ("queued", "running") and j["queued_at"] < job["queued_at"])
            return out

    def wait(self, entity_id: str, timeout: float = 10) -> dict | None:
        """Block until a job finishes (tests / scripts)."""
        end = time.time() + timeout
        while time.time() < end:
            s = self.status(entity_id)
            if s and s["status"] in ("done", "failed"):
                return s
            time.sleep(0.02)
        return self.status(entity_id)

    # ---------- worker ----------
    def _ensure_worker(self) -> None:          # called with self._lock held
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="extract-worker", daemon=True)
            self._worker.start()

    def _update(self, entity_id: str, **kw) -> None:
        with self._lock:
            self._jobs[entity_id].update(kw)

    def _run(self) -> None:
        while True:
            try:
                entity_id = self._queue.get(timeout=30)
            except queue.Empty:
                # Idle: end the thread, unless a job slipped in. Checked under the same lock that
                # submit() uses, so a new job either sees no worker (and starts one) or gets picked up.
                with self._lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                continue
            self._update(entity_id, status="running", step="starting", started_at=time.time())
            try:
                found = self._extract(entity_id)
                self._update(entity_id, status="done", step="done", found=found, finished_at=time.time())
            except Exception as e:                    # report it; the worker keeps serving the queue
                self._update(entity_id, status="failed", step="failed", error=str(e), finished_at=time.time())

    def _extract(self, entity_id: str) -> int:
        from .cli import _latest_filing, process_filing
        st = self._make_store()
        try:
            row = _latest_filing(st, entity_id)
            if not row:
                raise RuntimeError("No 10-K or 20-F annual report on file for this company.")

            def progress(step, done, total, found):
                self._update(entity_id, step=step, done=done, total=total, found=found)

            return process_filing(self._make_edgar(), st, self._make_llm(), self._make_resolver(),
                                  dict(row), max_passages=self.max_passages, progress=progress)
        finally:
            st.close()
