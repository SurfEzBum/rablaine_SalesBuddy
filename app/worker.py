"""
Background worker process entrypoint for Sales Buddy.

Runs the heavy background schedulers (MSX / WorkIQ / meeting aura) and the
durable job-queue consumer in a process **separate** from the web server, so a
slow or hung background job can never wedge the UI. The web server (waitress or
``flask run``) runs with the default ``web`` role and no longer starts these
schedulers.

Launch:

    python -m app.worker

Dev convenience wrapper: ``scripts/worker.ps1``.
"""
import logging
import os
import signal
import threading

logger = logging.getLogger(__name__)

# SyncStatus sync_type used as the worker liveness heartbeat that /health reads.
WORKER_HEARTBEAT_KEY = "worker_process"
_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _start_worker_heartbeat(app, stop_event: threading.Event) -> None:
    """Record a periodic worker heartbeat in SyncStatus for /health to read."""
    from app.models import SyncStatus

    with app.app_context():
        SyncStatus.mark_started(WORKER_HEARTBEAT_KEY)
        # Beat immediately so /health reports the worker alive right away,
        # instead of 'stale' for the first heartbeat interval (which would make
        # the supervisor falsely restart a healthy, just-started worker).
        SyncStatus.update_heartbeat(WORKER_HEARTBEAT_KEY)

    def _beat() -> None:
        while not stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                with app.app_context():
                    SyncStatus.update_heartbeat(WORKER_HEARTBEAT_KEY)
            except Exception:
                logger.debug("worker heartbeat failed", exc_info=True)

    threading.Thread(target=_beat, daemon=True, name="worker-heartbeat").start()


def main() -> None:
    """Boot the worker process and run the job loop until signalled."""
    # Mark this process as the worker BEFORE create_app so it starts the heavy
    # schedulers and tags lifecycle events with role='worker'.
    os.environ["SALESBUDDY_ROLE"] = "worker"

    from app import create_app
    from app.services.job_queue import run_worker_loop

    app = create_app()

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("Worker received signal %s, shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    _start_worker_heartbeat(app, stop_event)

    logger.info("Sales Buddy worker starting job loop")
    run_worker_loop(app, stop_event=stop_event)
    logger.info("Sales Buddy worker exited")


if __name__ == "__main__":
    main()
