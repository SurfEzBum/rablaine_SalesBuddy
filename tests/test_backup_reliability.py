"""Tests for backup JSON write reliability (app/services/backup.py).

Covers the atomic-write-with-retry helper that rides out transient OneDrive
file locks, and flush_pending_backups which lands debounced backups on exit.
"""
import contextlib
import json
import os

import pytest

from app.services import backup


def test_atomic_write_json_succeeds_first_try(tmp_path):
    target = tmp_path / "ok.json"
    backup._atomic_write_json(str(target), {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert not (tmp_path / "ok.json.tmp").exists()


def test_atomic_write_json_retries_on_transient_lock(tmp_path, monkeypatch):
    target = tmp_path / "retry.json"
    real_replace = backup.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by OneDrive")
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", flaky_replace)
    monkeypatch.setattr(backup.time, "sleep", lambda *_a, **_k: None)

    backup._atomic_write_json(str(target), {"a": 2})

    assert calls["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


def test_atomic_write_json_gives_up_and_cleans_tmp(tmp_path, monkeypatch):
    target = tmp_path / "fail.json"

    def always_locked(src, dst):
        raise PermissionError("permanently locked")

    monkeypatch.setattr(backup.os, "replace", always_locked)
    monkeypatch.setattr(backup.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(PermissionError):
        backup._atomic_write_json(str(target), {"a": 3})

    assert not target.exists()
    assert not (tmp_path / "fail.json.tmp").exists()


def test_flush_pending_backups_runs_and_clears(monkeypatch):
    ran = []
    monkeypatch.setattr(
        backup, "backup_customer", lambda cid: ran.append(cid) or True
    )

    class FakeApp:
        def app_context(self):
            return contextlib.nullcontext()

    class FakeTimer:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    timer = FakeTimer()
    with backup._pending_backups_lock:
        backup._pending_backups.clear()
        backup._pending_backups[42] = (timer, FakeApp())

    flushed = backup.flush_pending_backups()

    assert flushed == 1
    assert ran == [42]
    assert timer.cancelled is True
    assert backup._pending_backups == {}


def test_flush_pending_backups_empty_is_noop():
    with backup._pending_backups_lock:
        backup._pending_backups.clear()
    assert backup.flush_pending_backups() == 0


def test_backups_subdir_isolates_dev_from_prod(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    dev_sub = backup._backups_subdir()
    assert dev_sub == os.path.join("Backups", "SalesBuddyDev")

    monkeypatch.setenv("FLASK_ENV", "production")
    prod_sub = backup._backups_subdir()
    assert prod_sub == os.path.join("Backups", "SalesBuddy")
    assert "SalesBuddyDev" not in prod_sub


def test_get_backup_root_uses_dev_subdir(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr(backup, "_get_onedrive_path_from_db", lambda: r"C:\od")

    root = backup._get_backup_root()

    assert root == os.path.join(r"C:\od", "Backups", "SalesBuddyDev")
