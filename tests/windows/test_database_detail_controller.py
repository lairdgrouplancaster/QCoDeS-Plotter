"""Failure and callback ownership tests without a main window or database."""

import pytest

from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.windows._database_detail_controller import DatabaseDetailController


class Worker:
    def __init__(self, host, expensive, generation):
        self.host = host
        self.expensive = expensive
        self.generation = generation
        self.cancelled = False
        self.on_cancel = lambda: None

    def cancel(self):
        self.cancelled = True
        self.on_cancel()

    def prioritize_run_ids(self, run_ids):
        self.host.call("prioritize", self)


class Host:
    def __init__(self):
        self.hooks = {}
        self.workers = []
        self.submitted = []
        self.loading = set()
        self.statuses = []
        self.published = []
        self.reloads = []
        self.source_checks = []
        self.selected_path = "selected.db"
        self.current_source = True
        self.controller = DatabaseDetailController(self)

    def call(self, name, *args):
        callback = self.hooks.get(name)
        if callback:
            callback(*args)

    def create_worker(self, expensive, generation, path, run_ids, worker_kwargs):
        self.call("create")
        worker = Worker(self, expensive, generation)
        self.workers.append(worker)
        return worker

    def connect_worker(self, worker, expensive):
        self.call("connect", worker)

    def submit_worker(self, worker, expensive):
        self.call("submit", worker)
        self.submitted.append(worker)

    def priority_run_ids(self, run_ids=None):
        self.call("priority")
        return [1]

    def set_loading(self, columns, *, loading, **kwargs):
        if loading:
            self.loading.update(columns)
        else:
            self.loading.difference_update(columns)
        self.call("loading" if loading else "clear")

    def can_publish(self, path):
        return path == self.selected_path

    def source_current(self, path, instance, *, after_publication=False):
        self.source_checks.append(after_publication)
        self.call("source", after_publication)
        return self.current_source

    def publish(self, runs):
        self.published.append(runs)
        self.call("publish")

    def show_status(self, message, timeout):
        self.statuses.append(message)

    def reload(self, path):
        self.reloads.append(path)

    def start(self, expensive=False):
        return self.controller.start(self.selected_path, [1], None, {}, expensive=expensive)

    def finish(self, expensive=False, error=None):
        self.controller.finish(
            self.controller.channel(expensive).generation, self.selected_path, error, expensive=expensive,
        )


def fail(*_args):
    raise RuntimeError("Injected failure")


@pytest.mark.parametrize("expensive", [False, True])
@pytest.mark.parametrize("stage", ["create", "priority", "prioritize", "connect", "clear", "loading", "submit"])
def test_startup_failure_retires_only_failed_stage_and_allows_retry(expensive, stage):
    host = Host()
    assert host.start(not expensive)
    other = host.controller.channel(not expensive).worker

    def fail_once(*args):
        del host.hooks[stage]
        fail()

    host.hooks[stage] = fail_once
    assert not host.start(expensive)
    channel = host.controller.channel(expensive)
    generation = channel.generation
    assert not channel.active and channel.worker is None and channel.instance is None
    assert not set(host.controller.columns(expensive)) & host.loading
    assert set(host.controller.columns(not expensive)) <= host.loading
    assert not other.cancelled
    assert all(worker.cancelled for worker in host.workers if worker.expensive == expensive)
    assert "Injected failure" in host.statuses[-1]

    host.finish(not expensive)
    assert "Injected failure" in host.statuses[-1]  # Success cannot hide the failed stage.
    assert host.start(expensive)
    before = set(host.loading)
    host.controller.finish(generation, host.selected_path, None, expensive=expensive)
    host.controller.status(generation, "stale status", expensive=expensive)
    assert host.loading == before and host.controller.channel(expensive).active
    assert "stale status" not in host.statuses
    host.finish(expensive)
    assert not host.loading and host.statuses[-1] == "Run details loaded."


def test_cancel_fences_both_channels_before_reentrant_worker_callbacks():
    host = Host()
    host.start()
    host.start(True)
    basic, expensive = host.workers

    def callback():
        assert not host.controller.channel().active
        assert not host.controller.channel(True).active
        host.controller.status(expensive.generation, "late", expensive=True)
        host.controller.publish(basic.generation, host.selected_path, {1: {}}, expensive=False)
        fail()  # A broken cancellation must not prevent cancelling the other stage.

    basic.on_cancel = callback
    host.controller.cancel()
    assert basic.cancelled and expensive.cancelled
    assert not host.loading and not host.published and not host.statuses


@pytest.mark.parametrize("stage", ["create", "priority", "prioritize", "connect", "clear", "loading", "submit"])
@pytest.mark.parametrize("raises", [False, True])
def test_reentrant_replacement_survives_old_startup_cleanup(stage, raises):
    host = Host()

    def replace(*_args):
        del host.hooks[stage]
        assert host.start()
        if raises:
            fail()

    host.hooks[stage] = replace
    host.start()
    channel = host.controller.channel()
    assert channel.active and channel.generation == 2
    assert not channel.worker.cancelled
    assert host.loading == set(host.controller.columns(False))
    assert not host.statuses
    assert all(worker.cancelled for worker in host.workers if worker.generation == 1)


def test_reentrant_cancellation_does_not_clear_replacement_loading():
    host = Host()
    host.start()
    host.workers[0].on_cancel = host.start
    host.controller.cancel()
    assert host.controller.channel().active
    assert host.loading == set(host.controller.columns(False))


@pytest.mark.parametrize("after_publication", [False, True])
def test_source_identity_guard_prevents_stale_loading_updates(after_publication):
    host = Host()
    host.start()

    def changed(after):
        if after == after_publication:
            host.current_source = False

    host.hooks["source"] = changed
    host.controller.publish(1, host.selected_path, {1: {"guid": "run"}}, expensive=False)
    assert len(host.published) == int(after_publication)
    assert host.loading == set(host.controller.columns(False))
    assert host.source_checks == ([False, True] if after_publication else [False])


def test_replacement_during_publication_keeps_new_loading_indicators():
    host = Host()
    host.start(True)
    host.hooks["publish"] = lambda: host.start(True)
    host.controller.publish(1, host.selected_path, {1: {"guid": "run", "storage_bytes": 12}}, expensive=True)
    assert host.controller.channel(True).generation == 2
    assert host.loading == set(host.controller.columns(True))
    assert not host.controller.channel(True).publishing


def test_nested_completion_waits_for_publication_and_duplicate_is_ignored():
    host = Host()
    host.start()

    def finish_during_publication():
        host.finish()
        assert host.controller.channel().active
        assert not host.statuses

    host.hooks["publish"] = finish_during_publication
    host.controller.publish(1, host.selected_path, {1: {"guid": "run"}}, expensive=False)
    assert not host.controller.channel().active
    assert not host.loading
    assert host.statuses == ["Run details loaded."]
    host.finish()
    assert host.statuses == ["Run details loaded."]


def test_wrong_path_and_old_generation_cannot_publish_or_finish():
    host = Host()
    host.start()
    for generation, path in ((0, host.selected_path), (1, "other.db")):
        host.controller.publish(generation, path, {1: {}}, expensive=False)
        host.controller.finish(generation, path, None, expensive=False)
    assert host.controller.channel().active
    assert not host.published and not host.source_checks


def test_completion_retires_worker_after_view_changes_without_publishing_to_new_view():
    host = Host()
    host.start()
    path = host.selected_path
    host.selected_path = "another.db"
    before = set(host.loading)
    host.controller.finish(1, path, RuntimeError("obsolete error"), expensive=False)
    assert not host.controller.channel().active
    assert host.controller.channel().worker is None
    assert host.loading == before
    assert not host.statuses and not host.source_checks


def test_instance_changed_completion_cancels_both_channels_and_requests_reload():
    host = Host()
    host.start()
    host.start(True)
    host.finish(error=DatabaseInstanceChangedError("replaced"))
    assert all(not host.controller.channel(stage).active for stage in (False, True))
    assert host.workers[1].cancelled
    assert not host.loading and host.reloads == [host.selected_path]


@pytest.mark.parametrize("replacement", [False, True])
def test_pair_start_abandons_old_second_stage_after_reentrant_selection(replacement):
    host = Host()

    def change_selection():
        del host.hooks["create"]
        host.controller.cancel()
        if replacement:
            host.controller.start_all(host.selected_path, [2], None)

    host.hooks["create"] = change_selection
    assert not host.controller.start_all(host.selected_path, [1], None)
    assert len(host.submitted) == (2 if replacement else 0)
    assert all(worker.expensive is False for worker in host.workers if worker.generation == 1)
    assert all(host.controller.channel(stage).active == replacement for stage in (False, True))


def test_pair_start_continues_after_first_stage_failure():
    host = Host()

    def fail_once():
        del host.hooks["create"]
        fail()

    host.hooks["create"] = fail_once
    assert host.controller.start_all(host.selected_path, [1], None)
    assert not host.controller.channel().active
    assert host.controller.channel(True).active
    assert host.loading == set(host.controller.columns(True))


def test_indicator_cleanup_failure_still_retires_and_reports_startup_error():
    host = Host()
    host.hooks["clear"] = fail
    assert not host.start()
    assert not host.controller.channel().active
    assert host.workers[0].cancelled
    assert "Injected failure" in host.statuses[-1]


def test_cancellation_reaches_both_workers_even_when_indicator_cleanup_fails():
    host = Host()
    host.start()
    host.start(True)
    host.hooks["clear"] = fail
    host.controller.cancel()
    assert all(worker.cancelled for worker in host.workers)
    assert not any(host.controller.channel(stage).active for stage in (False, True))


def test_nested_batches_are_published_in_order_before_deferred_completion():
    host = Host()
    host.start()
    first, second = {1: {"guid": "first"}}, {2: {"guid": "second"}}

    def nested_callbacks():
        del host.hooks["publish"]
        host.controller.publish(1, host.selected_path, second, expensive=False)
        host.finish()
        assert host.published == [first]
        assert host.controller.channel().active

    host.hooks["publish"] = nested_callbacks
    host.controller.publish(1, host.selected_path, first, expensive=False)
    assert host.published == [first, second]
    assert host.source_checks == [False, True, False, True, False]
    assert host.statuses == ["Run details loaded."]
    assert not host.controller.channel().pending_batches


def test_publication_failure_overrides_nested_success_and_cancels_worker():
    host = Host()
    host.start()

    def fail_publication():
        host.finish()
        fail()

    host.hooks["publish"] = fail_publication
    host.controller.publish(1, host.selected_path, {1: {}}, expensive=False)
    assert host.workers[0].cancelled
    assert not host.controller.channel().active
    assert not host.loading
    assert len(host.statuses) == 1 and "Injected failure" in host.statuses[0]
