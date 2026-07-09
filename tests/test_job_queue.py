"""Tests for the durable SQLite job queue (app/services/job_queue.py)."""
import json
from datetime import timedelta

import pytest

from app.models import Job, db
from app.services import job_queue as jq


@pytest.fixture(autouse=True)
def _clean_jobs_and_handlers(app):
    """Clear the jobs table and restore the handler registry around each test.

    The app fixture is session-scoped, so state must be reset per test.
    """
    with app.app_context():
        Job.query.delete()
        db.session.commit()
    saved_handlers = dict(jq._HANDLERS)
    yield
    jq._HANDLERS.clear()
    jq._HANDLERS.update(saved_handlers)
    with app.app_context():
        Job.query.delete()
        db.session.commit()


def test_enqueue_creates_pending_job(app):
    with app.app_context():
        job = jq.enqueue("t", {"x": 1})
        assert job.status == Job.STATUS_PENDING
        assert job.job_type == "t"
        assert job.attempts == 0
        assert json.loads(db.session.get(Job, job.id).payload) == {"x": 1}


def test_enqueue_dedupe_skips_active_duplicate(app):
    with app.app_context():
        a = jq.enqueue("t", dedupe_key="k")
        b = jq.enqueue("t", dedupe_key="k")
        assert a.id == b.id
        assert Job.query.count() == 1


def test_enqueue_dedupe_allows_new_after_done(app):
    with app.app_context():
        a = jq.enqueue("t", dedupe_key="k")
        jq.complete(a.id)
        b = jq.enqueue("t", dedupe_key="k")
        assert a.id != b.id
        assert Job.query.count() == 2


def test_claim_next_marks_running(app):
    with app.app_context():
        job = jq.enqueue("t")
        claimed = jq.claim_next("w1")
        assert claimed.id == job.id
        assert claimed.status == Job.STATUS_RUNNING
        assert claimed.claimed_by == "w1"
        assert claimed.attempts == 1
        assert claimed.started_at is not None
        assert claimed.heartbeat_at is not None


def test_claim_next_empty_returns_none(app):
    with app.app_context():
        assert jq.claim_next("w1") is None


def test_claim_next_respects_available_at(app):
    with app.app_context():
        jq.enqueue("t", available_at=jq._now() + timedelta(seconds=60))
        assert jq.claim_next("w1") is None


def test_claim_next_honors_priority(app):
    with app.app_context():
        jq.enqueue("t", priority=0)
        high = jq.enqueue("t", priority=10)
        assert jq.claim_next("w1").id == high.id


def test_heartbeat_refreshes_timestamp(app):
    with app.app_context():
        job = jq.enqueue("t")
        jq.claim_next("w1")
        j = db.session.get(Job, job.id)
        j.heartbeat_at = None
        db.session.commit()
        jq.heartbeat(job.id)
        assert db.session.get(Job, job.id).heartbeat_at is not None


def test_complete_marks_done_with_result(app):
    with app.app_context():
        job = jq.enqueue("t")
        jq.claim_next("w1")
        jq.complete(job.id, {"ok": True})
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_DONE
        assert json.loads(j.result) == {"ok": True}
        assert j.finished_at is not None


def test_fail_retry_requeues_with_backoff(app):
    with app.app_context():
        job = jq.enqueue("t", max_attempts=3)
        jq.claim_next("w1")  # attempts -> 1
        jq.fail(job.id, "boom", retry=True)
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_PENDING
        assert j.available_at is not None
        assert j.available_at > jq._now()
        assert j.last_error == "boom"


def test_fail_exhausts_to_failed(app):
    with app.app_context():
        job = jq.enqueue("t", max_attempts=1)
        jq.claim_next("w1")  # attempts -> 1 == max
        jq.fail(job.id, "boom", retry=True)
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_FAILED
        assert j.finished_at is not None


def test_reclaim_stale_requeues_dead_job(app):
    with app.app_context():
        job = jq.enqueue("t", max_attempts=5)
        jq.claim_next("w1")
        j = db.session.get(Job, job.id)
        j.heartbeat_at = jq._now() - timedelta(
            seconds=Job.HEARTBEAT_STALE_SECONDS + 10
        )
        db.session.commit()

        assert jq.reclaim_stale() == 1
        assert db.session.get(Job, job.id).status == Job.STATUS_PENDING


def test_process_one_runs_handler_to_done(app):
    seen = {}

    @jq.job_handler("greet")
    def _greet(payload):
        seen["payload"] = payload
        return {"greeting": "hi " + payload["name"]}

    with app.app_context():
        job = jq.enqueue("greet", {"name": "bob"})
        done = jq.process_one("w1")
        assert done.id == job.id
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_DONE
        assert json.loads(j.result) == {"greeting": "hi bob"}
    assert seen["payload"] == {"name": "bob"}


def test_process_one_handler_error_retries(app):
    @jq.job_handler("boom")
    def _boom(payload):
        raise ValueError("nope")

    with app.app_context():
        job = jq.enqueue("boom", max_attempts=2)
        jq.process_one("w1")
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_PENDING
        assert "ValueError" in j.last_error


def test_process_one_unregistered_type_fails_without_retry(app):
    with app.app_context():
        job = jq.enqueue("no_such_handler", max_attempts=5)
        jq.process_one("w1")
        j = db.session.get(Job, job.id)
        assert j.status == Job.STATUS_FAILED
        assert "no handler" in j.last_error


def test_process_one_empty_queue_returns_none(app):
    with app.app_context():
        assert jq.process_one("w1") is None
