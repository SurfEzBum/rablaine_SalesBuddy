"""Single source of truth for where the SQLite database lives.

The **production** database is deliberately stored OUTSIDE the install directory
(a sibling of it) so that no installer / upgrade / uninstall path that deletes
the install dir can ever destroy user data. This is the structural fix for the
2026-07-22 data-loss incident. See docs/PLAN_db_outside_install_dir.md.

Resolution precedence (``resolve_db_path``):
  1. ``DATABASE_URL``      - explicit ``sqlite:///`` override (tests, power users).
  2. ``SALESBUDDY_DATA_DIR`` - explicit data-directory override.
  3. Derive from ``FLASK_ENV``:
       - ``production``  -> ``%LOCALAPPDATA%/SalesBuddy-data/salesbuddy.db``
                           (a SIBLING of the install dir; survives its deletion).
       - otherwise       -> ``<repo>/data/salesbuddy.db`` (dev, and tests with
                           no explicit override).

Everything that needs the DB path (the Flask app, the telemetry shipper, the
FY cutover archiver, the admin backup route, and - via ``data-path.txt`` - the
PowerShell scripts and the C# installer) resolves it through here so the three
languages can never drift.
"""
from __future__ import annotations

import gc
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_FILENAME = 'salesbuddy.db'

# A DB smaller than this is treated as "empty / fresh" - never worth migrating
# and never a valid migration target to skip over.
_MIN_REAL_DB_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    """Repo root == install dir (app/db_paths.py -> app/ -> root)."""
    return Path(__file__).resolve().parent.parent


def _is_production() -> bool:
    return (os.environ.get('FLASK_ENV') or 'production').strip().lower() == 'production'


def _prod_data_dir() -> Path:
    """Production data dir: a sibling of the install dir that survives any
    install / upgrade / uninstall that deletes the install dir."""
    local = os.environ.get('LOCALAPPDATA')
    if local:
        return Path(local) / 'SalesBuddy-data'
    # Non-Windows / LOCALAPPDATA missing: a stable per-user dir.
    return Path.home() / '.salesbuddy-data'


def _db_path_from_url(url: str) -> Path:
    """Parse a ``sqlite:///`` (relative/Windows-abs) or ``sqlite:////`` (POSIX
    absolute) URL into a filesystem Path; relative paths resolve under the repo."""
    if url.startswith('sqlite:////'):
        return Path('/' + url[len('sqlite:////'):])
    if url.startswith('sqlite:///'):
        raw = url[len('sqlite:///'):]
    else:
        raw = url
    path = Path(raw)
    if not path.is_absolute():
        path = _repo_root() / path
    return path


def resolve_data_dir() -> Path:
    """The directory that holds ``salesbuddy.db`` and its sidecar files."""
    url = os.environ.get('DATABASE_URL')
    if url and url.startswith('sqlite:'):
        return _db_path_from_url(url).parent
    override = os.environ.get('SALESBUDDY_DATA_DIR')
    if override:
        return Path(override)
    if _is_production():
        return _prod_data_dir()
    return _repo_root() / 'data'


def resolve_db_path() -> Path:
    """Absolute path to the SQLite database file."""
    url = os.environ.get('DATABASE_URL')
    if url and url.startswith('sqlite:'):
        return _db_path_from_url(url)
    return resolve_data_dir() / DB_FILENAME


def resolve_db_url() -> str:
    """SQLAlchemy ``sqlite:///`` URL for the resolved database path."""
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    return 'sqlite:///' + str(resolve_db_path())


def legacy_db_path() -> Path:
    """The pre-move location: ``<install dir>/data/salesbuddy.db``."""
    return _repo_root() / 'data' / DB_FILENAME


def write_data_path_file() -> None:
    """Publish the resolved DB path to ``<install dir>/data-path.txt`` so the
    PowerShell scripts and the C# installer read one source of truth instead of
    re-deriving it. Best-effort; never raises."""
    try:
        target = _repo_root() / 'data-path.txt'
        target.write_text(str(resolve_db_path()), encoding='utf-8')
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug('write_data_path_file failed: %s', exc)


def _customer_count(con: sqlite3.Connection) -> int:
    return con.execute('SELECT COUNT(*) FROM customers').fetchone()[0]


def _rename_with_retry(src: Path, dst: Path, log: logging.Logger,
                       attempts: int = 5) -> bool:
    """``os.replace`` with a short backoff + ``gc.collect()`` between tries.

    On Windows a freshly closed SQLite connection can briefly keep the underlying
    file locked (the classic ``WinError 32`` "used by another process"). Forcing a
    gc pass drops any lingering connection object and a couple of retries clear the
    transient lock. Returns True on success, False after exhausting ``attempts``.
    """
    for i in range(attempts):
        try:
            os.replace(str(src), str(dst))
            return True
        except OSError as exc:
            if i == attempts - 1:
                log.warning('DB migration: could not rename %s -> %s after %d tries: %s',
                            src.name, dst.name, attempts, exc)
                return False
            gc.collect()
            time.sleep(0.3)
    return False


def _drop_legacy_sidecars(legacy: Path) -> None:
    """Delete the now-orphaned WAL/SHM sidecars next to a moved legacy DB."""
    for ext in ('-wal', '-shm'):
        side = Path(str(legacy) + ext)
        if side.exists():
            try:
                side.unlink()
            except Exception:
                pass


def _cleanup_leftover_legacy(legacy: Path, log: logging.Logger) -> None:
    """Rename a leftover in-install legacy DB out of the way after the migration
    has already happened.

    A prior boot may have copied + verified the DB to the external location but
    then failed to rename the original (a transient ``WinError 32`` handle lock).
    Because the idempotency check short-circuits once the new DB exists, that
    orphan would otherwise linger forever in the install dir where an old build
    could accidentally reopen it. Clean it up here. Best-effort; never raises.
    """
    try:
        if not legacy.exists() or legacy.stat().st_size < _MIN_REAL_DB_BYTES:
            return
        ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        preserved = legacy.with_name(legacy.name + f'.migrated-{ts}.bak')
        if _rename_with_retry(legacy, preserved, log):
            _drop_legacy_sidecars(legacy)
            log.info('DB migration: cleaned up leftover legacy DB (-> %s)',
                     preserved.name)
    except Exception as exc:  # pragma: no cover - best effort
        log.debug('DB migration: leftover legacy cleanup skipped: %s', exc)



def _verify_copy(legacy_path: Path, copy_path: Path,
                 log: logging.Logger) -> bool:
    """Verify the migrated copy: PRAGMA integrity_check == ok AND the customer
    row count matches the source. Any error -> not verified (stay on legacy)."""
    try:
        con = sqlite3.connect(str(copy_path))
        try:
            result = con.execute('PRAGMA integrity_check').fetchone()
            if not result or str(result[0]).lower() != 'ok':
                log.error('DB migration: integrity_check = %r', result)
                return False
            copy_count = _customer_count(con)
        finally:
            con.close()

        src = sqlite3.connect(str(legacy_path))
        try:
            legacy_count = _customer_count(src)
        finally:
            src.close()

        if copy_count != legacy_count:
            log.error('DB migration: customer count mismatch legacy=%d copy=%d',
                      legacy_count, copy_count)
            return False
        log.info('DB migration: verified (integrity ok, customers=%d)', copy_count)
        return True
    except Exception as exc:
        log.error('DB migration: verification error: %s', exc)
        return False


def migrate_db_to_new_location(log: Optional[logging.Logger] = None, *,
                               new_path: Optional[Path] = None,
                               legacy: Optional[Path] = None) -> bool:
    """One-time, verified, idempotent move of the DB from the legacy in-install
    location to the resolved (external) location.

    Safe by construction:
      - No-op when the resolved path equals the legacy path (dev / tests).
      - No-op when the new DB already exists and is non-trivial (idempotent).
      - No-op when there's no real legacy DB to move (fresh install).
      - Copies via the SQLite online-backup API (folds WAL, no torn copy).
      - Verifies integrity + customer row parity BEFORE trusting the copy.
      - NEVER deletes the original - renames it to ``*.migrated-<ts>.bak``.
      - On ANY failure, leaves the legacy DB untouched and returns False so the
        caller falls back to the legacy location for this run.

    ``new_path`` / ``legacy`` are injectable for tests; production passes neither.

    Returns True only when a migration actually happened.
    """
    log = log or logger
    new_path = Path(new_path) if new_path is not None else resolve_db_path()
    legacy = Path(legacy) if legacy is not None else legacy_db_path()

    try:
        if new_path.resolve() == legacy.resolve():
            return False  # dev / tests / already in place
    except Exception:
        # resolve() can fail on non-existent paths on some setups; fall through
        # to the string compare as a backstop.
        if str(new_path) == str(legacy):
            return False

    if new_path.exists() and new_path.stat().st_size >= _MIN_REAL_DB_BYTES:
        # Already migrated. A prior run may have left the legacy DB behind if it
        # couldn't be renamed at the time (a transient WinError 32 file lock), so
        # sweep that orphan now before short-circuiting.
        _cleanup_leftover_legacy(legacy, log)
        return False  # already migrated

    if not legacy.exists() or legacy.stat().st_size < _MIN_REAL_DB_BYTES:
        return False  # nothing meaningful to migrate (fresh install)

    log.info('DB migration: moving %s -> %s', legacy, new_path)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = new_path.with_name(new_path.name + '.migrating')
    try:
        if tmp.exists():
            tmp.unlink()

        # Consistent snapshot copy via the SQLite backup API (folds the WAL in,
        # no torn read even if a stale writer is mid-transaction).
        src = sqlite3.connect(str(legacy))
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        if not _verify_copy(legacy, tmp, log):
            log.error('DB migration: verification failed - keeping legacy DB')
            try:
                tmp.unlink()
            except Exception:
                pass
            return False

        # Atomically put the verified copy in place at the new path.
        os.replace(str(tmp), str(new_path))

        # Preserve the original (NEVER delete). Rename with a UTC timestamp and
        # drop the now-orphaned WAL/SHM sidecars so the legacy dir holds no live
        # database that an old build could accidentally reopen. The rename retries
        # through transient Windows file locks (the freshly-closed backup handle).
        ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        preserved = legacy.with_name(legacy.name + f'.migrated-{ts}.bak')
        if _rename_with_retry(legacy, preserved, log):
            _drop_legacy_sidecars(legacy)
            log.info('DB migration: complete (legacy preserved as %s)', preserved.name)
        else:
            # Copy is in place and verified; failing to rename the original is not
            # fatal. The next boot's idempotency path (_cleanup_leftover_legacy)
            # sweeps the orphan once the lock clears.
            log.warning('DB migration: copy in place but legacy rename deferred to next boot')
        return True
    except Exception as exc:
        log.error('DB migration: failed (%s); falling back to legacy location', exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False
