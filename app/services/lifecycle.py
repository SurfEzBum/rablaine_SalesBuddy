"""
Structured application lifecycle and crash logging for Sales Buddy.

Emits newline-delimited JSON (JSONL) events to a rotating log file so we can
reconstruct "what happened before it died" as a timeline instead of guessing.
Also detects un-clean shutdowns: each boot drops a run-marker file that only a
clean shutdown removes, so the next boot can tell whether the previous run
crashed or was force-killed.

Log location (first path that resolves):
    1. %SALESBUDDY_HOME%\\logs        (set by scripts/server.ps1)
    2. %LOCALAPPDATA%\\SalesBuddy\\logs
    3. <repo>/logs                    (dev fallback)

Events (the ``event`` field):
    boot          - process started (commit, pid, flask_env, schedulers, prev_exit)
    shutdown      - clean shutdown reached (reason, flushed_backups)
    crash         - uncaught exception (main thread or a background thread)
    dirty_start   - previous run left a stale marker => it crashed / was killed

This module is intentionally defensive: nothing here should ever be able to
break application boot, so every I/O path swallows its own errors.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOG_FILENAME = "lifecycle.jsonl"
_MARKER_FILENAME = "running.marker"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
_BACKUP_COUNT = 3

_lifecycle_logger: Optional[logging.Logger] = None
_marker_path_cache: Optional[Path] = None
_shutdown_recorded = threading.Event()
_init_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# Paths and logger
# ---------------------------------------------------------------------------

def _resolve_log_dir() -> Path:
    """Return a writable log directory, creating it if needed.

    Resolution order:
        1. SALESBUDDY_HOME\\logs        (set by scripts/server.ps1; dev uses
           the isolated SalesBuddyDev home, prod uses SalesBuddy)
        2. repo ``logs/``               (only when FLASK_ENV=development and
           SALESBUDDY_HOME is unset, e.g. plain ``flask run`` - keeps dev
           events out of the production %LOCALAPPDATA%\\SalesBuddy folder)
        3. %LOCALAPPDATA%\\SalesBuddy\\logs
        4. repo ``logs/``               (final fallback)
        5. OS temp dir                  (last resort)
    """
    repo_logs = Path(__file__).resolve().parent.parent.parent / "logs"
    is_dev = os.environ.get("FLASK_ENV", "").strip().lower() == "development"

    candidates = []
    home = os.environ.get("SALESBUDDY_HOME")
    if home:
        candidates.append(Path(home) / "logs")
    # In dev without an explicit home, isolate into the repo logs/ folder so
    # we never write a run marker into the production LocalAppData folder.
    if is_dev:
        candidates.append(repo_logs)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "SalesBuddy" / "logs")
    candidates.append(repo_logs)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue

    import tempfile
    fallback = Path(tempfile.gettempdir()) / "salesbuddy_logs"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fallback


def _get_logger() -> logging.Logger:
    """Return the cached JSONL rotating-file logger."""
    global _lifecycle_logger
    if _lifecycle_logger is not None:
        return _lifecycle_logger

    lg = logging.getLogger("salesbuddy.lifecycle")
    lg.setLevel(logging.INFO)
    lg.propagate = False  # keep lifecycle events out of the app log
    try:
        handler = RotatingFileHandler(
            _resolve_log_dir() / _LOG_FILENAME,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
    except Exception:
        logger.debug("lifecycle: could not attach file handler", exc_info=True)

    _lifecycle_logger = lg
    return lg


def _marker_path() -> Path:
    """Return the run-marker file path (cached)."""
    global _marker_path_cache
    if _marker_path_cache is None:
        _marker_path_cache = _resolve_log_dir() / _MARKER_FILENAME
    return _marker_path_cache


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def _emit(event: str, **fields: Any) -> None:
    """Write a single JSONL lifecycle event. Never raises."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "pid": os.getpid(),
    }
    record.update(fields)
    try:
        _get_logger().info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        logger.debug("lifecycle: emit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Run marker (dirty-shutdown detection)
# ---------------------------------------------------------------------------

def _read_marker() -> Optional[dict]:
    """Return the parsed run marker, or None if absent/unreadable."""
    try:
        path = _marker_path()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("lifecycle: marker read failed", exc_info=True)
    return None


def _write_marker(**fields: Any) -> None:
    """Write the run marker for the current process. Never raises."""
    record = {
        "pid": os.getpid(),
        "boot_ts": datetime.now(timezone.utc).isoformat(),
    }
    record.update(fields)
    try:
        _marker_path().write_text(
            json.dumps(record, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("lifecycle: marker write failed", exc_info=True)


def _remove_marker() -> None:
    """Delete the run marker (marks a clean shutdown). Never raises."""
    try:
        path = _marker_path()
        if path.exists():
            path.unlink()
    except Exception:
        logger.debug("lifecycle: marker remove failed", exc_info=True)


def _detect_dirty_start() -> str:
    """Detect whether the previous run shut down cleanly.

    If a stale marker is found, the previous process did not remove it, so it
    crashed or was force-killed. Emits a ``dirty_start`` event in that case.

    Returns:
        ``"dirty"`` if a stale marker was found, otherwise ``"clean"``.
    """
    previous = _read_marker()
    if previous:
        _emit(
            "dirty_start",
            prev_pid=previous.get("pid"),
            prev_boot_ts=previous.get("boot_ts"),
            prev_commit=previous.get("commit"),
        )
        return "dirty"
    return "clean"


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

def record_clean_shutdown(reason: str = "exit") -> None:
    """Record a clean shutdown and flush pending work. Idempotent.

    Flushes any pending debounced customer backups so the most recent edits
    land before the process exits, emits a ``shutdown`` event, and removes the
    run marker so the next boot knows this exit was clean.

    Args:
        reason: Short label for why the process is shutting down
            (e.g. ``"admin_shutdown"``, ``"signal_15"``, ``"atexit"``).
    """
    if _shutdown_recorded.is_set():
        return
    _shutdown_recorded.set()

    flushed: Optional[int] = None
    try:
        from app.services.backup import flush_pending_backups
        flushed = flush_pending_backups()
    except Exception:
        logger.debug("lifecycle: backup flush on shutdown failed", exc_info=True)

    _emit("shutdown", reason=reason, flushed_backups=flushed)
    _remove_marker()


def _install_excepthooks() -> None:
    """Record uncaught exceptions (main thread and background threads)."""
    previous_hook = sys.excepthook

    def _main_hook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            _emit(
                "crash",
                where="main",
                exc_type=getattr(exc_type, "__name__", str(exc_type)),
                message=str(exc_value),
            )
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _main_hook

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_hook(args):
            if not issubclass(args.exc_type, SystemExit):
                _emit(
                    "crash",
                    where="thread",
                    thread=getattr(args.thread, "name", None),
                    exc_type=getattr(args.exc_type, "__name__", str(args.exc_type)),
                    message=str(args.exc_value),
                )
            previous_thread_hook(args)

        threading.excepthook = _thread_hook


def _install_signal_handlers() -> None:
    """Best-effort clean-shutdown recording on SIGTERM / SIGINT.

    Signal handlers can only be installed from the main thread, and on Windows
    a SIGTERM delivered via ``os.kill`` is a hard TerminateProcess that cannot
    be caught. This is a belt-and-suspenders measure; the admin shutdown/update
    routes call :func:`record_clean_shutdown` directly for the reliable path.
    """
    def _handler(signum, _frame):
        record_clean_shutdown(reason=f"signal_{signum}")
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, AttributeError):
            # Not in main thread, or signal unsupported on this platform.
            pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def init_lifecycle_logging(app, schedulers_started: Optional[list] = None) -> None:
    """Initialize lifecycle logging for the running process.

    Detects an un-clean previous shutdown, emits a ``boot`` event, writes the
    run marker, and installs crash/shutdown hooks. Safe to call once per
    process; subsequent calls are no-ops. Does nothing under TESTING so the
    test suite (which builds many apps) is not polluted with boot spam or
    process-global hooks.

    Args:
        app: The Flask application.
        schedulers_started: Names of background schedulers that were started,
            recorded on the boot event for observability.
    """
    if app.config.get("TESTING"):
        return

    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

    prev_exit = _detect_dirty_start()

    _emit(
        "boot",
        commit=app.config.get("BOOT_COMMIT"),
        commit_date=app.config.get("BOOT_COMMIT_DATE"),
        flask_env=os.environ.get("FLASK_ENV"),
        python=sys.version.split()[0],
        schedulers=schedulers_started or [],
        prev_exit=prev_exit,
    )

    _write_marker(commit=app.config.get("BOOT_COMMIT"))
    _install_excepthooks()
    _install_signal_handlers()
    atexit.register(record_clean_shutdown, "atexit")
