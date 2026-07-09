"""
Durable SQLite-backed job queue for Sales Buddy.

Decouples background work (scheduled syncs, backups, prefetch) from the web
request process. A separate worker process claims pending jobs, runs them with
periodic heartbeats, and marks them done or failed. Because the queue lives in
SQLite, jobs survive a restart and a job that was running when the worker died
is detectable via a stale heartbeat and can be reclaimed and retried.

Design notes:
- There is realistically a single worker process, so claiming uses a simple
  guarded UPDATE rather than heavyweight distributed locking.
- All service-managed timestamps are stored as naive UTC to keep SQLite text
  comparisons correct (aware datetimes serialize with an offset that breaks
  ``<=`` string comparisons).
- Handlers are plain callables ``fn(payload: dict) -> Optional[dict]`` run
  inside an application context provided by the worker.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.models import Job, db

logger = logging.getLogger(__name__)

# How often the heartbeat pulse updates a running job's heartbeat_at. Must be
# comfortably below Job.HEARTBEAT_STALE_SECONDS so a healthy job never looks
# stale.
_HEARTBEAT_INTERVAL_SECONDS = 30

# Retry backoff bounds (seconds).
_BASE_BACKOFF_SECONDS = 5
_MAX_BACKOFF_SECONDS = 300

# Handler registry: job_type -> callable.
_HANDLERS: dict[str, Callable[[dict], Optional[dict]]] = {}


def _now() -> datetime:
    """Return the current time as naive UTC (for SQLite-safe comparisons)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

def register_handler(job_type: str, fn: Callable[[dict], Optional[dict]]) -> None:
    """Register a handler callable for a job type."""
    _HANDLERS[job_type] = fn


def job_handler(job_type: str):
    """Decorator form of :func:`register_handler`."""
    def _decorator(fn: Callable[[dict], Optional[dict]]):
        register_handler(job_type, fn)
        return fn
    return _decorator


def get_handler(job_type: str) -> Optional[Callable[[dict], Optional[dict]]]:
    """Return the handler for a job type, or None."""
    return _HANDLERS.get(job_type)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(
    job_type: str,
    payload: Optional[dict] = None,
    *,
    dedupe_key: Optional[str] = None,
    priority: int = 0,
    max_attempts: int = 3,
    available_at: Optional[datetime] = None,
) -> Job:
    """Add a job to the queue and return it.

    If ``dedupe_key`` is provided and another job with the same key is still
    pending or running, that existing job is returned instead of creating a
    duplicate (debounce / coalescing).

    Args:
        job_type: Registered handler key.
        payload: JSON-serializable dict passed to the handler.
        dedupe_key: Optional coalescing key.
        priority: Higher runs first.
        max_attempts: Total attempts (including retries) before giving up.
        available_at: Earliest run time (naive UTC). Defaults to now.
    """
    if dedupe_key:
        existing = (
            Job.query
            .filter(Job.dedupe_key == dedupe_key)
            .filter(Job.status.in_([Job.STATUS_PENDING, Job.STATUS_RUNNING]))
            .first()
        )
        if existing is not None:
            return existing

    job = Job(
        job_type=job_type,
        status=Job.STATUS_PENDING,
        payload=json.dumps(payload) if payload is not None else None,
        dedupe_key=dedupe_key,
        priority=priority,
        max_attempts=max_attempts,
        available_at=available_at,
    )
    db.session.add(job)
    db.session.commit()
    return job


# ---------------------------------------------------------------------------
# Claim / heartbeat / complete / fail
# ---------------------------------------------------------------------------

def claim_next(worker_id: str) -> Optional[Job]:
    """Atomically claim the next runnable job, or return None.

    Picks the highest-priority, oldest pending job whose ``available_at`` has
    passed, and flips it to running with a guarded UPDATE so two workers can
    never claim the same job.
    """
    now = _now()
    candidates = (
        Job.query
        .filter(Job.status == Job.STATUS_PENDING)
        .filter(db.or_(Job.available_at.is_(None), Job.available_at <= now))
        .order_by(Job.priority.desc(), Job.id.asc())
        .limit(5)
        .all()
    )
    for candidate in candidates:
        updated = (
            Job.query
            .filter(Job.id == candidate.id, Job.status == Job.STATUS_PENDING)
            .update(
                {
                    Job.status: Job.STATUS_RUNNING,
                    Job.claimed_by: worker_id,
                    Job.attempts: Job.attempts + 1,
                    Job.started_at: _now(),
                    Job.heartbeat_at: _now(),
                },
                synchronize_session=False,
            )
        )
        db.session.commit()
        if updated == 1:
            return db.session.get(Job, candidate.id)
    return None


def heartbeat(job_id: int) -> None:
    """Refresh a running job's heartbeat so it isn't reclaimed as stale."""
    job = db.session.get(Job, job_id)
    if job is not None and job.status == Job.STATUS_RUNNING:
        job.heartbeat_at = _now()
        db.session.commit()


def _complete_job(job: Job, result: Optional[Any] = None) -> None:
    job.status = Job.STATUS_DONE
    job.result = json.dumps(result) if result is not None else None
    job.finished_at = _now()
    job.last_error = None
    db.session.commit()


def _fail_job(job: Job, error: str, retry: bool = True) -> None:
    job.last_error = (error or "")[:2000]
    if retry and job.attempts < job.max_attempts:
        # Requeue with exponential backoff.
        backoff = min(
            _MAX_BACKOFF_SECONDS,
            _BASE_BACKOFF_SECONDS * (2 ** max(0, job.attempts - 1)),
        )
        job.status = Job.STATUS_PENDING
        job.claimed_by = None
        job.heartbeat_at = None
        job.available_at = _now() + timedelta(seconds=backoff)
    else:
        job.status = Job.STATUS_FAILED
        job.finished_at = _now()
    db.session.commit()


def complete(job_id: int, result: Optional[Any] = None) -> None:
    """Mark a job done (public helper)."""
    job = db.session.get(Job, job_id)
    if job is not None:
        _complete_job(job, result)


def fail(job_id: int, error: str, retry: bool = True) -> None:
    """Mark a job failed/retried (public helper)."""
    job = db.session.get(Job, job_id)
    if job is not None:
        _fail_job(job, error, retry=retry)


def reclaim_stale() -> int:
    """Requeue running jobs whose heartbeat has gone stale (worker died).

    Returns the number of jobs reclaimed.
    """
    cutoff = _now() - timedelta(seconds=Job.HEARTBEAT_STALE_SECONDS)
    stale = (
        Job.query
        .filter(Job.status == Job.STATUS_RUNNING)
        .filter(Job.heartbeat_at.isnot(None), Job.heartbeat_at < cutoff)
        .all()
    )
    for job in stale:
        _fail_job(job, "worker died (stale heartbeat)", retry=True)
    return len(stale)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class _HeartbeatPulse(threading.Thread):
    """Background thread that refreshes a job's heartbeat while it runs."""

    def __init__(self, app, job_id: int, interval: Optional[float] = None):
        super().__init__(daemon=True, name=f"job-heartbeat-{job_id}")
        self._app = app
        self._job_id = job_id
        self._interval = interval or _HEARTBEAT_INTERVAL_SECONDS
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._app.app_context():
                    heartbeat(self._job_id)
            except Exception:
                logger.debug("heartbeat pulse failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()


def _execute(job: Job, app=None, with_heartbeat: bool = False) -> None:
    """Run a claimed job's handler and record the outcome."""
    handler = get_handler(job.job_type)
    if handler is None:
        _fail_job(job, f"no handler registered for job_type={job.job_type}",
                  retry=False)
        return

    payload = json.loads(job.payload) if job.payload else {}

    pulse = None
    if with_heartbeat and app is not None:
        pulse = _HeartbeatPulse(app, job.id)
        pulse.start()

    try:
        result = handler(payload)
        _complete_job(job, result if isinstance(result, (dict, list)) else None)
    except Exception as exc:  # noqa: BLE001 - any handler error is a job failure
        logger.exception("Job %s (%s) failed", job.id, job.job_type)
        _fail_job(job, f"{type(exc).__name__}: {exc}", retry=True)
    finally:
        if pulse is not None:
            pulse.stop()
            pulse.join(timeout=5)


def process_one(worker_id: str = "worker", app=None,
                with_heartbeat: bool = False) -> Optional[Job]:
    """Claim and run a single job. Returns the job, or None if queue empty."""
    job = claim_next(worker_id)
    if job is None:
        return None
    _execute(job, app=app, with_heartbeat=with_heartbeat)
    return job


def run_worker_loop(
    app,
    worker_id: Optional[str] = None,
    poll_interval: float = 5.0,
    reclaim_interval: float = 60.0,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Run the job worker loop until ``stop_event`` is set.

    Periodically reclaims stale jobs, then drains pending jobs one at a time,
    sleeping ``poll_interval`` when the queue is empty. Each job runs inside an
    application context with a heartbeat pulse so a long or hung job is visible.
    """
    worker_id = worker_id or f"worker-{os.getpid()}"
    stop_event = stop_event or threading.Event()
    logger.info("Job worker %s started", worker_id)

    last_reclaim = 0.0
    while not stop_event.is_set():
        try:
            monotonic = time.monotonic()
            if monotonic - last_reclaim >= reclaim_interval:
                with app.app_context():
                    reclaimed = reclaim_stale()
                if reclaimed:
                    logger.warning("Reclaimed %d stale job(s)", reclaimed)
                last_reclaim = monotonic

            with app.app_context():
                job = process_one(worker_id, app=app, with_heartbeat=True)

            if job is None:
                stop_event.wait(poll_interval)
        except Exception:
            logger.exception("Job worker loop error")
            stop_event.wait(poll_interval)

    logger.info("Job worker %s stopped", worker_id)
