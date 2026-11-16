"""Controller ownership contracts, without widgets or background threads."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE
from qplot.windows._database_load_controller import DatabaseLoadController
from qplot.windows._database_load_state import DatabaseLoadSession


class Worker:
    def __init__(self, service):
        self.trusted_service = service
        self.access_mode = TRUSTED_LIVE_MODE
        self.fallback_reason = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Host:
    def __init__(self, instance):
        self.instance = instance
        self.service = SimpleNamespace(accepted=True, database_instance=instance)
        self.worker = Worker(self.service)
        self.loading = False
        self.submitted = []
        self.retired = []
        self.errors = []
        self.results = []
        self.retries = []
        self.progress = []
        self.statuses = []
        self.cancelled = False
        self.closing = False

    def create_worker(self, generation, session):
        return self.worker

    def connect_worker(self, worker):
        pass

    def submit_worker(self, worker):
        self.submitted.append(worker)

    def is_committed_service(self, service):
        return False

    def retire_service(self, service, *, force=False):
        self.retired.append((service, force))

    def show_loading(self, message):
        self.loading = True

    def hide_loading(self):
        self.loading = False

    def show_progress(self, message):
        self.progress.append(message)

    def show_status(self, message, timeout):
        self.statuses.append((message, timeout))

    def load_failed(self, *failure):
        self.errors.append(failure)

    def load_cancelled(self):
        self.cancelled = True

    def publish(self, result):
        with self.controller.publication(result, committed=lambda: True) as current:
            assert current
            self.results.append(result)

    def can_retry(self):
        return not self.closing

    def reload(self, session):
        self.retries.append(session)
        return True


@pytest.fixture
def load():
    instance = DatabaseInstance("source.db", "source.db", (1, 2))
    host = Host(instance)
    queued = []
    controller = DatabaseLoadController(
        host, observe_instance=lambda _path: host.instance, schedule=queued.append,
    )
    host.controller = controller
    session = DatabaseLoadSession(
        "source.db", instance, load_started_at=12.0, generation_recovery=True,
    )
    return controller, host, session, queued


@pytest.mark.parametrize("boundary", ["show_loading", "create_worker", "connect_worker", "submit_worker"])
def test_startup_failure_releases_pending_ownership_and_allows_another_load(load, monkeypatch, boundary):
    controller, host, session, _queued = load

    def fail(*_args):
        raise RuntimeError("startup failed")

    with monkeypatch.context() as failure:
        failure.setattr(host, boundary, fail)
        assert not controller.start(session, "Loading")
    assert not controller.active and controller.worker is None
    assert not controller.pending_services and not host.loading
    assert len(host.errors) == 1
    if boundary in {"connect_worker", "submit_worker"}:
        assert host.worker.cancelled
        assert host.retired == [(host.service, True)]
    assert controller.start(replace(session, worker=None), "Retry")


def test_cancellation_failure_does_not_lose_pending_reader(load, monkeypatch):
    controller, host, session, _queued = load

    def fail(*_args):
        raise RuntimeError("failed")

    monkeypatch.setattr(host, "submit_worker", fail)
    monkeypatch.setattr(host.worker, "cancel", fail)
    assert not controller.start(session, "Loading")
    assert not controller.active and not controller.pending_services
    assert host.retired == [(host.service, True)]


@pytest.mark.parametrize("boundary", ["show_loading", "create_worker", "connect_worker"])
def test_reentrant_cancellation_during_startup_never_submits_worker(load, monkeypatch, boundary):
    controller, host, session, _queued = load
    original = getattr(host, boundary)

    def cancel(*args):
        result = original(*args)
        controller.cancel()
        return result

    monkeypatch.setattr(host, boundary, cancel)
    assert not controller.start(session, "Loading")
    assert not host.submitted and not host.errors
    assert not controller.active and not controller.pending_services
    if boundary != "show_loading":
        assert host.worker.cancelled
        assert len(host.retired) == 1


@pytest.mark.parametrize("mismatch", ["path", "worker"])
def test_unrelated_callback_cannot_take_current_pending_reader(load, mismatch):
    controller, host, session, _queued = load
    assert controller.start(session, "Loading")
    generation = controller.generation
    controller.finish(
        generation, "other.db" if mismatch == "path" else session.abspath, {}, None,
        Worker(host.service) if mismatch == "worker" else host.worker,
    )
    assert controller.worker is host.worker
    assert controller.pending_services == {generation: host.service}
    assert not host.retired and not host.results
    controller.finish(generation, session.abspath, {1: {"guid": "accepted"}}, None, host.worker)
    assert host.results[0].runs == {1: {"guid": "accepted"}}
    assert not controller.active and not controller.pending_services


def test_cancelled_callback_and_progress_cannot_affect_new_load(load):
    controller, host, session, _queued = load
    assert controller.start(session, "First")
    old_generation = controller.generation
    old_worker = host.worker
    controller.cancel()
    host.worker = Worker(host.service)
    assert controller.start(replace(session, worker=None), "Second")
    controller.progress(old_generation, "Stale")
    controller.finish(old_generation, session.abspath, {}, None, old_worker)
    assert controller.worker is host.worker
    assert not host.progress and not host.results
    assert controller.pending_services == {controller.generation: host.service}


@pytest.mark.parametrize("boundary", ["hide_loading", "publish"])
def test_duplicate_completion_cannot_retire_reader_being_published(load, monkeypatch, boundary):
    controller, host, session, _queued = load
    assert controller.start(session, "Loading")
    generation = controller.generation
    original = getattr(host, boundary)

    def duplicate(*args):
        controller.finish(generation, session.abspath, {}, None, host.worker)
        assert controller.pending_services == {generation: host.service}
        return original(*args)

    monkeypatch.setattr(host, boundary, duplicate)
    controller.finish(generation, session.abspath, {1: {"guid": "accepted"}}, None)
    assert not host.retired and not host.errors
    assert len(host.results) == 1


@pytest.mark.parametrize("supersede", ["cancel", "close", "new_load", "shutdown"])
def test_queued_replacement_retry_is_fenced_by_later_action(load, supersede):
    controller, host, session, queued = load
    assert controller.start(session, "Loading")
    controller.finish(controller.generation, session.abspath, {}, DatabaseInstanceChangedError("changed"))
    assert len(queued) == 1 and not controller.active
    assert host.retired == [(host.service, False)]
    if supersede == "cancel":
        controller.cancel()
    elif supersede == "close":
        controller.invalidate()
    elif supersede == "new_load":
        controller.start(replace(session, worker=None), "Another load")
    else:
        host.closing = True
    queued.pop()()
    assert not host.retries


def test_current_retry_retains_recovery_context_and_original_start_time(load):
    controller, host, session, queued = load
    assert controller.start(session, "Loading")
    controller.finish(controller.generation, session.abspath, {}, DatabaseInstanceChangedError("changed"))
    queued.pop()()
    assert host.retries == [session]
    assert host.retries[0].generation_recovery and host.retries[0].load_started_at == 12.0


def test_idle_cancellation_does_not_invalidate_committed_source_consumers(load):
    controller, host, session, _queued = load
    assert controller.start(session, "Loading")
    controller.finish(controller.generation, session.abspath, {}, None)
    generation = controller.generation
    controller.cancel()
    assert controller.generation == generation
    assert not host.retired and not host.cancelled


@pytest.mark.parametrize("boundary", ["hide_loading", "observe_instance", "publish"])
def test_completion_failure_retires_reader_without_leaking_active_state(load, monkeypatch, boundary):
    controller, host, session, _queued = load
    assert controller.start(session, "Loading")

    def fail(*_args):
        raise RuntimeError("completion failed")

    monkeypatch.setattr(controller if boundary == "observe_instance" else host, boundary, fail)
    controller.finish(controller.generation, session.abspath, {}, None)
    assert not controller.active and not controller.pending_services
    assert host.retired == [(host.service, True)]
    assert len(host.errors) == 1


@pytest.mark.parametrize("close_during_publication", [False, True])
def test_failed_publication_retains_reader_for_shutdown_and_always_releases_gate(
    load, monkeypatch, close_during_publication,
):
    controller, host, session, _queued = load
    assert controller.start(session, "Loading")

    def publish(result):
        with controller.publication(result, committed=lambda: False) as current:
            assert current and controller.publication_active
            assert tuple(controller.pending_services.values()) == (host.service,)
            assert not controller.start(replace(session), "Nested load")
            if close_during_publication:
                controller.invalidate()
                for service in controller.take_pending_services():
                    host.retire_service(service, force=True)
            raise RuntimeError("view failed")

    monkeypatch.setattr(host, "publish", publish)
    controller.finish(controller.generation, session.abspath, {}, None)
    assert not controller.publication_active and not controller.pending_services
    assert host.retired == [(host.service, True)]
    assert len(host.errors) == 1
