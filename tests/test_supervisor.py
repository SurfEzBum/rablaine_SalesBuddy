"""Tests for the process supervisor (app/supervisor.py)."""
import time
from types import SimpleNamespace

import pytest

from app import supervisor as sup


@pytest.fixture(autouse=True)
def _quiet_lifecycle(monkeypatch):
    """Silence lifecycle event emission so tests don't write to the log."""
    monkeypatch.setattr(sup.lifecycle, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr(sup.lifecycle, "set_role", lambda *a, **k: None)


class FakeChild(sup.ManagedChild):
    """A ManagedChild that records lifecycle calls instead of spawning."""

    def __init__(self, name="c", health=None):
        super().__init__(name, ["noop"], health_check=lambda: self._health)
        self._poll_result = None
        self._health = health
        self.starts = 0
        self.terminates = 0

    def start(self, now=None):
        self.starts += 1
        # Start well outside the grace window so health checks apply immediately.
        self.started_at = (time.monotonic() - 10_000) if now is None else now
        self.health_failures = 0
        self.process = SimpleNamespace(returncode=None)

    def poll(self):
        return self._poll_result

    def terminate(self):
        self.terminates += 1


# --- decide_action truth table ---------------------------------------------

def test_decide_action_crash():
    assert sup.decide_action(exited=True, in_grace=False,
                             health=None, health_failures=0) == "restart_crash"


def test_decide_action_grace_ignores_bad_health():
    assert sup.decide_action(exited=False, in_grace=True,
                             health=False, health_failures=99) == "ok"


def test_decide_action_health_failures_below_threshold():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=False,
                             health_failures=sup.MAX_HEALTH_FAILURES - 1) == "ok"


def test_decide_action_health_failures_at_threshold():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=False,
                             health_failures=sup.MAX_HEALTH_FAILURES) == "restart_hang"


def test_decide_action_healthy_and_indeterminate_are_ok():
    assert sup.decide_action(exited=False, in_grace=False,
                             health=True, health_failures=0) == "ok"
    assert sup.decide_action(exited=False, in_grace=False,
                             health=None, health_failures=0) == "ok"


# --- startup grace ----------------------------------------------------------

def test_in_startup_grace():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    child.started_at = now
    assert child.in_startup_grace(now + 1) is True
    assert child.in_startup_grace(now + sup.STARTUP_GRACE_SECONDS + 1) is False


# --- crash-loop backoff -----------------------------------------------------

def test_backoff_zero_below_threshold():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    for _ in range(sup.CRASH_LOOP_MAX):
        child.record_restart(now)
    assert child.backoff_seconds(now) == 0.0


def test_backoff_grows_and_caps_above_threshold():
    child = sup.ManagedChild("c", ["noop"])
    now = 1000.0
    for _ in range(sup.CRASH_LOOP_MAX + 1):
        child.record_restart(now)
    assert child.backoff_seconds(now) == 2.0  # 2 ** 1 excess

    for _ in range(20):
        child.record_restart(now)
    assert child.backoff_seconds(now) == float(sup.MAX_BACKOFF_SECONDS)


def test_restarts_outside_window_are_pruned():
    child = sup.ManagedChild("c", ["noop"])
    child.record_restart(1000.0)
    later = 1000.0 + sup.CRASH_LOOP_WINDOW_SECONDS + 10
    child.record_restart(later)
    assert child.restarts_in_window(later) == 1


# --- supervisor _check behavior --------------------------------------------

def test_check_restarts_on_crash():
    child = FakeChild("web")
    child.start()
    child._poll_result = 1  # process exited
    child.process.returncode = 1
    s = sup.Supervisor([child], poll_interval=0)
    s._check(child, time.monotonic())
    assert child.terminates == 1
    assert child.starts == 2  # initial start + restart


def test_check_restarts_after_repeated_health_failures():
    child = FakeChild("worker", health=False)
    child.start()
    s = sup.Supervisor([child], poll_interval=0)
    now = time.monotonic()

    # First failures below threshold: no restart yet.
    for _ in range(sup.MAX_HEALTH_FAILURES - 1):
        s._check(child, now)
    assert child.starts == 1
    assert child.health_failures == sup.MAX_HEALTH_FAILURES - 1

    # The failure that reaches the threshold triggers a restart.
    s._check(child, now)
    assert child.terminates == 1
    assert child.starts == 2


def test_check_healthy_child_is_left_alone():
    child = FakeChild("web", health=True)
    child.start()
    s = sup.Supervisor([child], poll_interval=0)
    s._check(child, time.monotonic())
    assert child.starts == 1
    assert child.terminates == 0
    assert child.health_failures == 0
