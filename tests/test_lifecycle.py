"""Tests for structured lifecycle/crash logging (app/services/lifecycle.py)."""
import json
import logging
import threading

import pytest


def _read_events(tmp_path):
    """Return all JSONL lifecycle events written to the temp log dir."""
    path = tmp_path / "lifecycle.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def lc(tmp_path, monkeypatch):
    """Provide the lifecycle module wired to an isolated temp log dir."""
    import app.services.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_resolve_log_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle, "_lifecycle_logger", None)
    monkeypatch.setattr(lifecycle, "_marker_path_cache", None)
    monkeypatch.setattr(lifecycle, "_shutdown_recorded", threading.Event())

    # The named logger is process-global; clear handlers so each test starts
    # clean and does not accumulate duplicate file handlers.
    logging.getLogger("salesbuddy.lifecycle").handlers.clear()
    yield lifecycle
    logging.getLogger("salesbuddy.lifecycle").handlers.clear()


def test_emit_writes_jsonl_event(lc, tmp_path):
    lc._emit("boot", commit="abc123", schedulers=["copilot"])

    events = _read_events(tmp_path)
    assert len(events) == 1
    assert events[0]["event"] == "boot"
    assert events[0]["commit"] == "abc123"
    assert events[0]["schedulers"] == ["copilot"]
    assert "ts" in events[0]
    assert "pid" in events[0]


def test_marker_roundtrip(lc):
    lc._write_marker(commit="deadbeef")
    data = lc._read_marker()
    assert data is not None
    assert data["commit"] == "deadbeef"
    assert "pid" in data
    assert "boot_ts" in data

    lc._remove_marker()
    assert lc._read_marker() is None


def test_detect_dirty_start_with_stale_marker(lc, tmp_path):
    lc._write_marker(pid=9999, commit="oldcommit")

    result = lc._detect_dirty_start()

    assert result == "dirty"
    events = _read_events(tmp_path)
    dirty = [e for e in events if e["event"] == "dirty_start"]
    assert len(dirty) == 1
    assert dirty[0]["prev_pid"] == 9999
    assert dirty[0]["prev_commit"] == "oldcommit"


def test_detect_clean_start_without_marker(lc, tmp_path):
    assert lc._detect_dirty_start() == "clean"
    assert _read_events(tmp_path) == []


def test_record_clean_shutdown_removes_marker_and_emits(lc, tmp_path):
    lc._write_marker()

    lc.record_clean_shutdown(reason="test_reason")

    assert lc._read_marker() is None
    shutdowns = [e for e in _read_events(tmp_path) if e["event"] == "shutdown"]
    assert len(shutdowns) == 1
    assert shutdowns[0]["reason"] == "test_reason"


def test_record_clean_shutdown_is_idempotent(lc, tmp_path):
    lc.record_clean_shutdown(reason="first")
    lc.record_clean_shutdown(reason="second")

    shutdowns = [e for e in _read_events(tmp_path) if e["event"] == "shutdown"]
    assert len(shutdowns) == 1
    assert shutdowns[0]["reason"] == "first"


def test_init_lifecycle_logging_noop_under_testing(lc, tmp_path):
    class FakeApp:
        config = {"TESTING": True}

    lc.init_lifecycle_logging(FakeApp(), ["copilot"])

    # Nothing should be written and no marker created under TESTING.
    assert _read_events(tmp_path) == []
    assert lc._read_marker() is None
