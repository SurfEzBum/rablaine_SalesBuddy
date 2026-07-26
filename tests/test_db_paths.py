"""Tests for app.db_paths - the DB-location resolver and the one-time,
verified, idempotent migration that moves the DB outside the install dir."""
import sqlite3
from pathlib import Path

import pytest

from app import db_paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Each test fully controls its own environment - clear the DB-location
    vars so a leaked DATABASE_URL/FLASK_ENV from another fixture can't skew
    resolution."""
    for var in ('DATABASE_URL', 'SALESBUDDY_DATA_DIR', 'FLASK_ENV', 'LOCALAPPDATA'):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_db(path: Path, customers: int = 0, pad: bool = True) -> None:
    """Create a small SQLite DB with a customers table (and optional padding to
    push it past the 64 KB 'real DB' threshold)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)')
        for i in range(customers):
            con.execute('INSERT INTO customers (name) VALUES (?)', (f'c{i}',))
        if pad:
            con.execute('CREATE TABLE _pad (b BLOB)')
            con.execute('INSERT INTO _pad (b) VALUES (?)', (b'x' * (200 * 1024),))
        con.commit()
    finally:
        con.close()


def _count(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Resolver precedence
# ---------------------------------------------------------------------------

def test_database_url_wins_over_flask_env(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "x.db"}')
    monkeypatch.setenv('FLASK_ENV', 'production')
    assert db_paths.resolve_db_path() == tmp_path / 'x.db'


def test_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv('SALESBUDDY_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('FLASK_ENV', 'production')
    assert db_paths.resolve_db_path() == tmp_path / 'salesbuddy.db'


def test_production_derivation_uses_localappdata_sibling(monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert db_paths.resolve_db_path() == tmp_path / 'SalesBuddy-data' / 'salesbuddy.db'


def test_dev_derivation_uses_repo_data(monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    expected = db_paths._repo_root() / 'data' / 'salesbuddy.db'
    assert db_paths.resolve_db_path() == expected


def test_resolve_db_url_is_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    url = db_paths.resolve_db_url()
    assert url.startswith('sqlite:///') and url.endswith('salesbuddy.db')


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migrate_noop_when_paths_equal(tmp_path):
    p = tmp_path / 'salesbuddy.db'
    _make_db(p, customers=3)
    assert db_paths.migrate_db_to_new_location(new_path=p, legacy=p) is False


def test_migrate_noop_when_legacy_missing(tmp_path):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False
    assert not new.exists()


def test_migrate_noop_when_legacy_too_small(tmp_path):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(legacy, customers=1, pad=False)  # tiny, < 64 KB
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False
    assert not new.exists()


def test_migrate_noop_when_new_already_populated(tmp_path):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(new, customers=1)
    _make_db(legacy, customers=99)
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False
    assert _count(new) == 1  # untouched


def test_migrate_happy_path(tmp_path):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(legacy, customers=42)

    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is True

    assert new.exists()
    assert _count(new) == 42
    # original preserved (never deleted), not left in place
    assert not legacy.exists()
    baks = list(legacy.parent.glob('salesbuddy.db.migrated-*.bak'))
    assert len(baks) == 1
    assert _count(baks[0]) == 42


def test_migrate_idempotent(tmp_path):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(legacy, customers=5)
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is True
    # second run: new is populated -> no-op
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False


def test_migrate_verification_failure_keeps_legacy(tmp_path, monkeypatch):
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(legacy, customers=10)
    monkeypatch.setattr(db_paths, '_verify_copy', lambda *a, **k: False)

    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False
    assert legacy.exists()      # untouched
    assert not new.exists()     # bad copy cleaned up


def test_migrate_cleans_leftover_legacy_on_next_boot(tmp_path):
    """If a prior migration copied+verified the DB but couldn't rename the legacy
    (a transient WinError 32 lock), the next no-op call sweeps the orphan - and
    its WAL/SHM sidecars - out of the install dir."""
    new = tmp_path / 'new' / 'salesbuddy.db'
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    _make_db(new, customers=42)     # already migrated
    _make_db(legacy, customers=42)  # orphan left behind last boot
    Path(str(legacy) + '-wal').write_bytes(b'x')
    Path(str(legacy) + '-shm').write_bytes(b'x')

    # No-op migration (new already populated) still cleans the orphan.
    assert db_paths.migrate_db_to_new_location(new_path=new, legacy=legacy) is False
    assert not legacy.exists()
    assert not Path(str(legacy) + '-wal').exists()
    assert not Path(str(legacy) + '-shm').exists()
    baks = list(legacy.parent.glob('salesbuddy.db.migrated-*.bak'))
    assert len(baks) == 1
    assert _count(baks[0]) == 42
    assert _count(new) == 42  # new untouched


def test_rename_with_retry_succeeds(tmp_path):
    import logging
    src = tmp_path / 'a.txt'
    src.write_text('x')
    dst = tmp_path / 'b.txt'
    assert db_paths._rename_with_retry(src, dst, logging.getLogger('t')) is True
    assert dst.exists() and not src.exists()


def test_rename_with_retry_gives_up(tmp_path, monkeypatch):
    import logging

    def _boom(*a, **k):
        raise OSError('locked')

    src = tmp_path / 'a.txt'
    src.write_text('x')
    dst = tmp_path / 'b.txt'
    monkeypatch.setattr(db_paths.os, 'replace', _boom)
    assert db_paths._rename_with_retry(
        src, dst, logging.getLogger('t'), attempts=2) is False


def test_verify_copy_rejects_row_mismatch(tmp_path):
    """_verify_copy must reject a copy whose customer count differs from source."""
    import logging
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    other = tmp_path / 'other.db'
    _make_db(legacy, customers=7)
    _make_db(other, customers=6)
    assert db_paths._verify_copy(legacy, other, logging.getLogger('t')) is False


def test_verify_copy_accepts_matching(tmp_path):
    import logging
    legacy = tmp_path / 'old' / 'salesbuddy.db'
    copy = tmp_path / 'copy.db'
    _make_db(legacy, customers=7)
    _make_db(copy, customers=7)
    assert db_paths._verify_copy(legacy, copy, logging.getLogger('t')) is True


# ---------------------------------------------------------------------------
# WAL-safe backup (backup_database)
# ---------------------------------------------------------------------------

def test_backup_happy_path(tmp_path):
    src = tmp_path / 'salesbuddy.db'
    _make_db(src, customers=12)
    dest = tmp_path / 'out' / 'backup.db'

    assert db_paths.backup_database(dest, src=src) is True
    assert dest.exists()
    assert _count(dest) == 12
    # No leftover partial file.
    assert not dest.with_name(dest.name + '.partial').exists()


def test_backup_is_wal_safe(tmp_path):
    """Rows committed but still sitting in the -wal (uncheckpointed) must be
    captured. A naive file copy of the main db file would miss them."""
    src = tmp_path / 'salesbuddy.db'
    _make_db(src, customers=3)

    writer = sqlite3.connect(str(src))
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO customers (name) VALUES ('wal1')")
        writer.execute("INSERT INTO customers (name) VALUES ('wal2')")
        writer.commit()  # committed, but not checkpointed -> lives in -wal

        dest = tmp_path / 'backup.db'
        assert db_paths.backup_database(dest, src=src) is True
        assert _count(dest) == 5  # all rows, WAL folded into the snapshot
    finally:
        writer.close()


def test_backup_missing_source_returns_false(tmp_path):
    assert db_paths.backup_database(
        tmp_path / 'b.db', src=tmp_path / 'nope.db') is False


def test_backup_no_verify(tmp_path):
    src = tmp_path / 'salesbuddy.db'
    _make_db(src, customers=2)
    dest = tmp_path / 'b.db'
    assert db_paths.backup_database(dest, src=src, verify=False) is True
    assert _count(dest) == 2


def test_backup_verification_failure_discards(tmp_path, monkeypatch):
    src = tmp_path / 'salesbuddy.db'
    _make_db(src, customers=4)
    dest = tmp_path / 'backup.db'
    monkeypatch.setattr(db_paths, '_verify_copy', lambda *a, **k: False)

    assert db_paths.backup_database(dest, src=src) is False
    assert not dest.exists()  # bad copy discarded
    assert not dest.with_name(dest.name + '.partial').exists()
