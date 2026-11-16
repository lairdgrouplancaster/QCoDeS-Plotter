"""Refresh lifetime tests without Qt event loops or background workers."""

from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.windows._database_refresh_controller import (
    DatabaseRefreshController,
    RefreshPublicationAborted,
)


class Worker:
    def __init__(self):
        self.cancelled = 0
        self.acknowledgements = 0
        self.rejections = []

    def cancel(self):
        self.cancelled += 1

    def acknowledge_new_runs_published(self):
        self.acknowledgements += 1

    def reject_new_runs_publication(self, error):
        self.rejections.append(error)


class Host:
    def __init__(self):
        self.hooks = {}
        self.workers = []
        self.submitted = []
        self.errors = []
        self.results = []
        self.refreshes = 0
        self.allowed = True

    def hook(self, stage):
        if callback := self.hooks.get(stage):
            callback()

    def create_worker(self, session):
        self.hook("create")
        worker = Worker()
        self.workers.append(worker)
        return worker

    def connect_worker(self, worker):
        self.hook("connect")

    def submit_worker(self, worker):
        self.hook("submit")
        self.submitted.append(worker)

    def show_status(self, message, timeout):
        self.hook("status")

    def failed(self, error):
        self.errors.append(error)

    def can_refresh(self, path):
        return self.allowed

    def refresh(self):
        self.refreshes += 1

    def reload(self, path):
        raise AssertionError(f"Unexpected reload: {path}")

    def begin_publication(self):
        self.hook("begin")
        return "selected"

    def end_publication(self, selected_guid):
        assert selected_guid == "selected"
        self.hook("end")

    def publish_page(self, session, runs, is_current):
        self.hook("page")
        if not is_current():
            raise RefreshPublicationAborted("cancelled during publication")

    def finish(self, session, runs, statuses, error, is_current):
        self.hook("finish")
        if is_current():
            self.results.append((runs, statuses, error))


@pytest.fixture
def refresh():
    host = Host()
    scheduled = []
    controller = DatabaseRefreshController(host, schedule=scheduled.append)
    instance = database_instance("refresh.db")
    return SimpleNamespace(
        host=host, controller=controller, scheduled=scheduled, instance=instance,
        start=lambda: controller.start("refresh.db", instance),
        finish=lambda **kwargs: controller.finish(
            controller.generation, "refresh.db", {}, {}, None, **kwargs,
        ),
    )


def fail():
    raise RuntimeError("injected failure")


@pytest.mark.parametrize("stage", ["status", "create", "connect", "submit"])
def test_startup_failure_fences_callbacks_and_allows_retry(refresh, stage):
    refresh.host.hooks[stage] = fail
    assert not refresh.start()
    assert not refresh.controller.active
    assert refresh.controller.worker is None
    assert len(refresh.host.errors) == 1
    if refresh.host.workers:
        assert refresh.host.workers[-1].cancelled
    refresh.controller.finish(1, "refresh.db", {1: {"guid": "stale"}}, {}, None)
    assert refresh.host.results == []
    refresh.host.hooks.clear()
    assert refresh.start()
    assert refresh.controller.active


@pytest.mark.parametrize("stage", ["status", "create", "connect"])
def test_cancellation_during_startup_prevents_submission(refresh, stage):
    refresh.host.hooks[stage] = refresh.controller.invalidate
    assert not refresh.start()
    assert not refresh.controller.active
    assert refresh.host.submitted == []
    assert refresh.host.errors == []
    assert all(worker.cancelled for worker in refresh.host.workers)


def test_repeated_requests_schedule_only_one_refresh(refresh):
    assert refresh.start()
    for _ in range(10):
        assert not refresh.start()
    generation = refresh.controller.generation
    refresh.finish()
    refresh.controller.finish(generation, "refresh.db", {}, {}, None)
    assert len(refresh.scheduled) == 1
    refresh.scheduled.pop()()
    assert refresh.host.refreshes == 1


@pytest.mark.parametrize("change", ["cancel", "replace", "shutdown"])
def test_queued_refresh_cannot_outlive_its_session(refresh, change):
    refresh.start()
    refresh.start()
    refresh.finish()
    if change == "cancel":
        refresh.controller.invalidate()
    elif change == "replace":
        refresh.start()
    else:
        refresh.host.allowed = False
    refresh.scheduled.pop()()
    assert refresh.host.refreshes == 0


def test_old_completion_cannot_clear_a_new_refresh(refresh):
    refresh.start()
    old_worker = refresh.controller.worker

    def replace():
        refresh.controller.invalidate()
        assert refresh.start()

    refresh.host.hooks["finish"] = replace
    refresh.finish()
    assert refresh.controller.active
    assert refresh.controller.worker is not old_worker
    assert not refresh.controller.worker.cancelled
    assert refresh.host.results == []


def test_completion_exception_restores_idle_and_allows_retry(refresh):
    refresh.start()
    refresh.host.hooks["finish"] = fail
    refresh.finish()
    assert not refresh.controller.active
    assert len(refresh.host.errors) == 1
    refresh.host.hooks.clear()
    assert refresh.start()


@pytest.mark.parametrize("mismatch", ["generation", "path", "worker"])
def test_unrelated_callbacks_cannot_consume_current_refresh(refresh, mismatch):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker
    generation = controller.generation - (mismatch == "generation")
    path = "other.db" if mismatch == "path" else "refresh.db"
    source = Worker() if mismatch == "worker" else worker
    controller.publish_page(generation, path, {1: {"guid": "wrong"}}, source)
    controller.finish(generation, path, {}, {}, None, source)
    assert controller.active
    assert controller.worker is worker
    assert dict(controller.staged_runs) == {}
    assert source.acknowledgements == 0
    assert len(source.rejections) == 1
    assert refresh.host.results == []


def test_page_is_staged_before_acknowledgement_and_merged_on_completion(refresh):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker
    page = {1: {"guid": "first"}}
    controller.publish_page(controller.generation, "refresh.db", page, worker)
    assert dict(controller.staged_runs) == page
    assert worker.acknowledgements == 1
    controller.finish(controller.generation, "refresh.db", {2: {"guid": "second"}}, {}, None)
    assert refresh.host.results == [({**page, 2: {"guid": "second"}}, {}, None)]


def test_failed_page_preserves_previously_published_staging(refresh):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker
    first = {1: {"guid": "first"}}
    controller.publish_page(controller.generation, "refresh.db", first, worker)
    refresh.host.hooks["page"] = fail
    controller.publish_page(controller.generation, "refresh.db", {2: {"guid": "bad"}}, worker)
    assert dict(controller.staged_runs) == first
    assert worker.acknowledgements == 1
    assert len(worker.rejections) == 1
    assert not controller.publication_active


def test_nested_completion_waits_until_page_publication_unwinds(refresh):
    refresh.start()
    controller = refresh.controller

    def nested_finish():
        refresh.finish()
        refresh.finish()
        assert refresh.scheduled == []
        assert refresh.host.results == []
        assert controller.active

    refresh.host.hooks["page"] = nested_finish
    page = {1: {"guid": "first"}}
    controller.publish_page(controller.generation, "refresh.db", page, controller.worker)
    assert len(refresh.scheduled) == 1
    refresh.scheduled.pop()()
    assert refresh.host.results == [(page, {}, None)]
    assert not controller.active


def test_selection_repair_error_does_not_lose_page_acknowledgement(refresh):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker
    refresh.host.hooks["end"] = fail
    controller.publish_page(controller.generation, "refresh.db", {}, worker)
    assert worker.acknowledgements == 1
    assert worker.rejections == []
    assert not controller.publication_active


def test_deferred_completion_retains_page_publication_failure(refresh):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker

    def failed_page():
        refresh.finish()
        fail()

    refresh.host.hooks["page"] = failed_page
    controller.publish_page(controller.generation, "refresh.db", {1: {"guid": "bad"}}, worker)
    refresh.scheduled.pop()()
    assert refresh.host.results[0][0] == {}
    assert refresh.host.results[0][2] is worker.rejections[0]
    assert not controller.active


def test_cancellation_failure_still_fences_ownership(refresh):
    refresh.start()
    generation = refresh.controller.generation
    refresh.controller.worker.cancel = fail
    refresh.controller.invalidate()
    assert not refresh.controller.active
    refresh.controller.finish(generation, "refresh.db", {}, {}, None)
    assert refresh.host.results == []


def test_nested_duplicate_completion_is_ignored(refresh):
    refresh.start()
    refresh.host.hooks["finish"] = refresh.finish
    refresh.finish()
    assert refresh.host.results == [({}, {}, None)]


def test_cancellation_during_publication_rejects_page_without_restoring_session(refresh):
    refresh.start()
    controller = refresh.controller
    worker = controller.worker
    refresh.host.hooks["page"] = controller.invalidate
    controller.publish_page(controller.generation, "refresh.db", {1: {"guid": "new"}}, worker)
    assert not controller.active
    assert not controller.publication_active
    assert dict(controller.staged_runs) == {}
    assert worker.acknowledgements == 0
    assert len(worker.rejections) == 1
