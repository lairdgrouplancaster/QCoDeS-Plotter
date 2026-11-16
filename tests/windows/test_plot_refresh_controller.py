"""Faults and nested callbacks must leave one explicit plot owner."""

from types import SimpleNamespace

import pytest

from qplot.windows._plot_refresh_controller import (
    PlotRefreshController,
    PlotRefreshRequest,
)


class Worker:
    def __init__(self):
        self.running = True
        self.cancelled = 0
        self.on_cancel = lambda: None

    def cancel(self):
        self.cancelled += 1
        self.on_cancel()


class Host:
    def __init__(self):
        self.hooks = {}
        self.workers = []
        self.registered = set()
        self.submitted = []
        self.publications = []
        self.failures = []
        self.settlements = []
        self.cancellations = 0
        self.callbacks = []
        self.refreshes = []
        self.messages = []
        self.errors = []
        self.allowed = True
        self.required = True

    def step(self, name):
        self.hooks.get(name, lambda: None)()

    def create_worker(self, request):
        self.step("create")
        worker = Worker()
        self.workers.append(worker)
        return worker

    def prepare_worker(self, worker):
        self.step("prepare")

    def connect_worker(self, session):
        self.registered.add(session.worker)
        self.step("connect")

    def show_loading(self, session, request):
        self.step("loading")

    def submit_worker(self, worker):
        self.step("submit")
        self.submitted.append(worker)

    def release_worker(self, worker):
        self.registered.discard(worker)
        self.step("release")

    def can_refresh(self):
        self.step("allowed")
        return self.allowed

    def refresh_required(self):
        self.step("required")
        return self.required

    def refresh(self, force):
        self.refreshes.append(force)

    def schedule(self, callback):
        self.callbacks.append(callback)

    def publish(self, worker, finished):
        self.publications.append((worker, finished))
        self.step("publish")

    def failed(self, error, *, startup):
        self.failures.append((error, startup))
        self.step("failed")

    def settled(self, session):
        self.settlements.append(session)
        self.step("settled")

    def cancelled(self):
        self.cancellations += 1

    def status(self, worker, message):
        self.messages.append((worker, message))

    def error(self, worker, error):
        self.errors.append((worker, error))


@pytest.fixture
def job():
    host = Host()
    controller = PlotRefreshController(host)
    return SimpleNamespace(host=host, controller=controller, start=lambda: controller.start(PlotRefreshRequest(())))


def fail():
    raise RuntimeError("injected fault")


@pytest.mark.parametrize("stage", ["create", "prepare", "connect", "loading", "submit"])
def test_failed_start_retires_registration_and_allows_retry(job, stage):
    job.host.hooks[stage] = fail
    assert not job.start()
    failed = job.controller.session
    assert not job.controller.busy
    assert not job.host.registered
    assert len(job.host.failures) == 1
    if failed.worker is not None:
        assert failed.worker.cancelled == 1
        assert not failed.worker.running
    job.host.hooks.clear()
    assert job.start()
    current = job.controller.session
    job.controller.finish(failed, True)
    job.controller.status(failed, "late status")
    job.controller.error(failed, RuntimeError("late error"))
    assert job.controller.session is current
    assert job.controller.busy
    assert job.host.publications == job.host.messages == job.host.errors == []
    assert job.host.registered == {current.worker}


@pytest.mark.parametrize("stage", ["create", "prepare", "connect", "loading"])
def test_cancellation_during_startup_never_submits(job, stage):
    job.host.hooks[stage] = job.controller.cancel
    assert not job.start()
    assert not job.host.submitted
    assert not job.host.registered
    assert not job.controller.busy
    assert job.host.workers[0].cancelled >= 1


@pytest.mark.parametrize("stage", ["create", "prepare", "connect", "loading", "submit"])
def test_reentrant_start_cannot_be_cleared_by_outer_start(job, stage):
    def replace():
        job.host.hooks.pop(stage)
        assert job.start()

    job.host.hooks[stage] = replace
    assert not job.start()
    current = job.controller.session
    assert job.controller.accepts(current)
    assert current.worker.cancelled == 0
    assert current.worker in job.host.registered
    assert not job.host.failures


def test_synchronous_completion_waits_until_submission_returns(job):
    def complete():
        job.controller.finish(job.controller.session, True)
        assert not job.host.publications

    job.host.hooks["submit"] = complete
    assert job.start()
    assert job.host.publications == [(job.controller.worker, True)]
    assert not job.controller.busy
    assert not job.host.registered


def test_submission_failure_overrides_early_completion(job):
    def complete_then_fail():
        job.controller.finish(job.controller.session, True)
        fail()

    job.host.hooks["submit"] = complete_then_fail
    assert not job.start()
    assert not job.host.publications
    assert not job.controller.busy
    assert len(job.host.failures) == 1


def test_duplicate_completion_and_late_messages_do_not_republish(job):
    assert job.start()
    session = job.controller.session
    job.host.hooks["publish"] = lambda: job.controller.finish(session, True)
    job.controller.finish(session, True)
    job.controller.finish(session, True)
    job.controller.status(session, "late")
    job.controller.error(session, RuntimeError("late"))
    assert job.host.publications == [(session.worker, True)]
    assert len(job.host.settlements) == 1
    assert job.host.messages == job.host.errors == []


def test_queued_refresh_waits_through_concrete_rendering(job):
    assert job.start()
    session = job.controller.session
    job.controller.queue(force=True)

    def publish():
        session.worker.running = False
        job.controller.run_pending()
        assert job.controller.busy
        assert not job.host.refreshes

    job.host.hooks["publish"] = publish
    job.controller.finish(session, True)
    assert len(job.host.callbacks) == 1
    job.host.callbacks.pop()()
    assert job.host.refreshes == [True]


@pytest.mark.parametrize("stage", ["publish", "settled"])
def test_old_finalisation_does_not_clear_replacement(job, stage):
    assert job.start()
    old = job.controller.session
    job.host.hooks[stage] = job.start
    job.controller.finish(old, True)
    assert job.controller.session is not old
    assert job.controller.busy
    assert job.controller.worker.running
    assert job.controller.worker.cancelled == 0
    assert job.host.registered == {job.controller.worker}


def test_publication_failure_releases_worker_and_preserves_retry(job):
    assert job.start()
    job.controller.queue(force=True)
    job.host.hooks["publish"] = fail
    job.controller.finish(job.controller.session, True)
    assert job.host.failures[0][1] is False
    assert not job.controller.busy
    assert not job.host.registered
    job.host.callbacks.pop()()
    assert job.host.refreshes == [True]


def test_cancel_fences_callbacks_before_worker_cancellation(job):
    assert job.start()
    session = job.controller.session
    session.worker.on_cancel = lambda: job.controller.finish(session, True)
    job.controller.queue(force=True)
    job.controller.schedule_pending()
    callback = job.host.callbacks.pop()
    job.controller.cancel()
    assert not job.controller.busy
    assert job.controller.pending is None
    assert job.host.publications == []
    callback()
    assert job.host.refreshes == []


def test_old_queued_callback_cannot_consume_new_request(job):
    job.controller.queue(force=True)
    job.controller.schedule_pending()
    old = job.host.callbacks.pop()
    job.controller.cancel()
    job.controller.queue(force=True)
    job.controller.schedule_pending()
    new = job.host.callbacks.pop()
    old()
    assert job.host.refreshes == []
    new()
    assert job.host.refreshes == [True]


@pytest.mark.parametrize("force", [False, True])
def test_coalescing_preserves_force_but_drops_completed_automatic_work(job, force):
    job.controller.queue(force=force)
    job.controller.queue()
    job.host.required = False
    job.controller.run_pending()
    assert job.host.refreshes == ([True] if force else [])


@pytest.mark.parametrize("stage", ["allowed", "required"])
def test_nested_cancellation_cannot_revive_queued_refresh(job, stage):
    job.controller.queue()
    job.host.hooks[stage] = job.controller.cancel
    job.controller.run_pending()
    assert not job.host.refreshes


def test_cancel_failure_still_invalidates_and_allows_retry(job):
    assert job.start()
    job.controller.worker.on_cancel = fail
    job.controller.cancel()
    assert not job.controller.busy
    assert job.start()


def test_cleanup_failure_does_not_leave_active_job(job):
    assert job.start()
    job.host.hooks["release"] = fail
    job.controller.finish(job.controller.session, True)
    assert not job.controller.busy
    assert len(job.host.settlements) == 1


def test_changed_source_rejects_completion_without_publishing(job):
    assert job.start()
    job.host.allowed = False
    job.controller.finish(job.controller.session, True)
    assert not job.host.publications
    assert not job.host.registered
    assert not job.controller.busy


def test_forgetting_cancelled_worker_does_not_discard_reentrant_replacement(job):
    assert job.start()
    previous = job.controller.session
    previous.worker.on_cancel = job.start
    job.controller.forget_worker()
    assert job.controller.session is not previous
    assert job.controller.busy


def test_queued_request_yields_without_reposting_while_worker_is_busy(job):
    assert job.start()
    job.controller.queue(force=True)
    job.controller.schedule_pending()
    job.host.callbacks.pop()()
    assert job.host.refreshes == job.host.callbacks == []
    assert job.controller.pending is True
    job.controller.finish(job.controller.session, True)
    job.host.callbacks.pop()()
    assert job.host.refreshes == [True]


def test_cancellation_is_idempotent_for_worker_and_waiters(job):
    assert job.start()
    worker = job.controller.worker
    job.controller.cancel()
    job.controller.cancel()
    assert worker.cancelled == 1
    assert job.host.cancellations == 1


@pytest.mark.parametrize("stage", ["failed", "settled"])
def test_feedback_failure_does_not_escape_startup_or_prevent_retry(job, stage):
    job.host.hooks.update(create=fail, **{stage: fail})
    assert not job.start()
    assert not job.controller.busy
    job.host.hooks.clear()
    assert job.start()
