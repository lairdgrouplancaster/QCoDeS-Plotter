"""Ownership and retry at preview startup, publication and cancellation boundaries."""

from types import SimpleNamespace

import pytest

from qplot.windows._widgets._preview_controller import PreviewController

pytestmark = pytest.mark.pure


class Host:
    def __init__(self):
        self.controller = PreviewController(self, max_workers=2)
        self.hooks = {}
        self.submitted = []
        self.published = []
        self.failures = []
        self.activities = []
        self.timers = []

    def call(self, name, job):
        if name in self.hooks:
            self.hooks[name](job)

    def create_worker(self, job):
        self.call("create", job)
        worker = SimpleNamespace(cancelled=False)

        def cancel():
            worker.cancelled = True
            self.call("cancel", job)

        worker.cancel = cancel
        worker.is_cancelled = lambda: worker.cancelled
        return worker

    def connect_worker(self, job):
        self.call("connect", job)

    def submit_worker(self, job):
        self.submitted.append(job)
        self.call("submit", job)

    def activity(self, job, active):
        self.activities.append((job.generation, job.guid, active))
        self.call("loading" if active else "idle", job)

    def publish(self, job, previews, error):
        self.call("publish", job)
        self.published.append((job.guid, previews, error))

    def failed(self, job, error):
        self.failures.append((job.guid, str(error)))
        self.call("failure", job)

    def schedule(self, callback):
        self.timers.append(callback)

    def settled(self):
        self.controller.pump()

    def finish(self, job, previews=None):
        self.controller.finish(job.generation, job.guid, job.worker, previews, None)


def fail(_job):
    raise RuntimeError("injected fault")


@pytest.mark.parametrize("stage", ["create", "connect", "loading", "submit"])
def test_startup_fault_releases_slot_allows_other_work_and_retry(stage):
    host = Host()
    controller = host.controller
    host.hooks[stage] = lambda job: fail(job) if job.guid == "first" else None
    controller.enqueue("first", 10)
    controller.enqueue("second", 5)
    controller.pump()
    assert controller.active == {(0, "second")}
    assert host.failures == [("first", "injected fault")]
    assert not controller.queue
    host.hooks.clear()
    controller.enqueue("first", 10)
    controller.pump()
    retried = host.submitted[-1]
    host.finish(retried, ["fresh"])
    assert host.published == [("first", ["fresh"], None)]


@pytest.mark.parametrize("stage", ["create", "connect", "loading", "idle"])
def test_reentrant_source_change_prevents_old_submission_or_publication(stage):
    host = Host()
    controller = host.controller

    def replace(job):
        host.hooks.clear()
        controller.reset()
        controller.enqueue("new", 10)
        controller.pump()

    host.hooks[stage] = replace
    controller.enqueue("old", 10)
    controller.pump()
    if stage == "idle":
        host.finish(host.submitted[0], ["stale"])
    assert host.submitted[-1].guid == "new"
    assert not host.published
    assert controller.active == {(1, "new")}
    if stage != "idle":
        assert all(job.guid != "old" for job in host.submitted)


def test_synchronous_completion_waits_for_submission_and_duplicate_is_ignored():
    host = Host()

    def complete(job):
        host.finish(job, ["first"])
        host.finish(job, ["duplicate"])
        assert host.published == []

    host.hooks["submit"] = complete
    host.controller.enqueue("run", 0)
    host.controller.pump()
    host.finish(host.submitted[0], ["late duplicate"])
    assert host.published == [("run", ["first"], None)]
    assert not host.controller.active
    assert not host.controller.workers


def test_pending_completion_is_discarded_if_submission_then_raises():
    host = Host()

    def submit(job):
        host.finish(job, ["unaccepted"])
        fail(job)

    host.hooks["submit"] = submit
    host.controller.enqueue("run", 0)
    host.controller.pump()
    assert not host.published
    assert not host.controller.workers
    assert host.submitted[0].worker.cancelled


def test_publication_fault_releases_slot_without_losing_queued_work():
    host = Host()
    host.controller.enqueue("a", 3)
    host.controller.enqueue("b", 2)
    host.controller.enqueue("c", 1)
    host.controller.pump()
    host.hooks["publish"] = fail
    host.finish(host.submitted[0])
    assert host.controller.active == {(0, "b"), (0, "c")}
    assert host.failures == [("a", "injected fault")]


def test_old_completion_cannot_retire_same_guid_in_new_generation():
    host = Host()
    host.controller.enqueue("same", 0)
    host.controller.pump()
    old = host.submitted[-1]
    host.controller.reset()
    host.controller.enqueue("same", 0)
    host.controller.pump()
    new = host.submitted[-1]
    host.finish(old, ["old"])
    assert host.controller.workers == {new.key: new.worker}
    assert host.activities[-1] == (1, "same", True)
    host.finish(new, ["new"])
    assert host.published == [("same", ["new"], None)]


def test_cancel_failure_does_not_prevent_sibling_cancellation_or_close():
    host = Host()
    for guid in ("a", "b", "queued"):
        host.controller.enqueue(guid, 0)
    host.controller.pump()
    host.hooks["cancel"] = fail
    host.controller.reset(close=True)
    assert all(job.worker.cancelled for job in host.submitted)
    assert not host.controller.active
    assert not host.controller.queue
    assert len(host.controller.workers) == 2
    for job in host.submitted:
        host.finish(job)
    assert not host.controller.workers
    assert not host.published


def test_old_timer_cannot_consume_new_generation_request():
    host = Host()
    host.controller.schedule()
    old = host.timers[-1]
    host.controller.reset()
    host.controller.enqueue("new", 0)
    host.controller.schedule()
    current = host.timers[-1]
    old()
    assert not host.submitted
    assert host.controller.scheduled
    current()
    assert host.submitted[-1].guid == "new"
    assert not host.controller.scheduled
