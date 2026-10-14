"""Auto-plot owns requests across refresh and selection, but not sessions."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore

from qplot.windows import _auto_plot
from qplot.windows._auto_plot import (
    cancel_auto_plot,
    enqueue_auto_plot,
    update_auto_plot,
)
from qplot.windows._plot_actions import PlotActionsMixin


class Request:
    done = False
    cancelled = False
    error = None

    def cancel(self):
        self.cancelled = True

    def wait(self, timeout):
        assert timeout == 0 and self.done
        if self.error:
            raise self.error
        return self.dataset


class Service:
    closing = closed = False

    def __init__(self):
        self.requests = []

    def submit_plot_dataset(self, guid):
        request = Request()
        request.dataset = SimpleNamespace(guid=guid)
        self.requests.append(request)
        return request


class Window(QtCore.QObject):
    _database_access_mode = "trusted_live"
    _resume_trusted_plot_action = PlotActionsMixin._resume_trusted_plot_action

    def __init__(self):
        super().__init__()
        self._trusted_read_service = Service()
        self.checked = True
        self.current = True
        self.metadata = {}
        self.RunList = SimpleNamespace(all_run_metadata=lambda: self.metadata)
        self.autoPlotBox = SimpleNamespace(isChecked=lambda: self.checked)
        self.opened = []
        self.statuses = []

    def _current_dataset_key(self, guid):
        return guid

    def _dataset_key_is_current(self, key):
        return self.current

    def _run_metadata_for_id(self, run_id):
        return self.metadata.get(run_id, {})

    def openPlot(self, key):
        assert self._trusted_plot_ready[key].guid == key
        self.opened.append(key)

    def show_status(self, *args):
        self.statuses.append(args)


@pytest.fixture
def window():
    window = Window()
    yield window
    cancel_auto_plot(window)


def finish_one(queue, *, error=None):
    queue.poll()
    request = queue.active[1]
    request.error = error
    request.done = True
    queue.poll()


def test_batch_exceeds_manual_request_cap_and_survives_refresh_selection(window):
    guids = [f"guid-{index}" for index in range(40)]
    enqueue_auto_plot(window, guids)
    queue = window._auto_plot_queue
    for index, _guid in enumerate(guids):
        queue.poll()
        assert len(window._trusted_read_service.requests) == index + 1
        window.selected_run_id = index  # Unrelated selection and refresh epochs.
        window._database_refresh_generation = index
        enqueue_auto_plot(window, guids)
        queue.active[1].done = True
        queue.poll()
    assert window.opened == guids
    enqueue_auto_plot(window, guids)  # Replay, even after a plot was closed.
    queue.poll()
    assert queue.active is None and not queue.timer.isActive()
    assert window._trusted_plot_ready == {}


@pytest.mark.parametrize("invalidate", ["disable", "shutdown", "service", "identity", "close"])
def test_cancellation_discards_pending_and_completed_unpublished_work(window, invalidate):
    enqueue_auto_plot(window, ["one", "two"])
    queue = window._auto_plot_queue
    queue.poll()
    request = queue.active[1]
    request.done = True  # Completion racing with cancellation cannot publish.
    if invalidate == "disable":
        window.checked = False
    elif invalidate == "shutdown":
        window._shutdown_started = True
    elif invalidate == "service":
        window._trusted_read_service = Service()
    elif invalidate == "identity":
        window.current = False
    else:
        cancel_auto_plot(window)
    queue.poll()
    assert request.cancelled
    assert not queue.runs and not queue.timer.isActive()
    assert not window.opened


def test_description_retries_are_bounded_and_rearm_only_on_metadata_change(window, monkeypatch):
    now = [0]
    monkeypatch.setattr(_auto_plot, "monotonic", lambda: now[0])
    window.metadata = {1: {"guid": "late", "is_completed": False}}
    enqueue_auto_plot(window, ["late", "ready"])
    queue = window._auto_plot_queue
    finish_one(queue, error=ValueError("No description yet"))
    finish_one(queue)  # The waiting run does not block others.
    assert window.opened == ["ready"]
    for _ in _auto_plot.RETRY_DELAYS:
        now[0] += 31
        finish_one(queue, error=ValueError("No description yet"))
    queue.poll()
    assert not queue.timer.isActive()
    assert len(window.statuses) == 1
    count = len(window._trusted_read_service.requests)
    for _ in range(20):
        now[0] += 100
        enqueue_auto_plot(window, ["late"])
        update_auto_plot(window, window.metadata)
        queue.poll()
    assert len(window._trusted_read_service.requests) == count
    window.metadata[1]["measure_parameters"] = ["signal"]
    update_auto_plot(window, window.metadata)
    finish_one(queue)
    assert window.opened == ["ready", "late"]


def test_metadata_change_during_failed_request_is_not_lost(window, monkeypatch):
    monkeypatch.setattr(_auto_plot, "RETRY_DELAYS", ())
    window.metadata = {1: {"guid": "late"}}
    enqueue_auto_plot(window, ["late"])
    queue = window._auto_plot_queue
    queue.poll()
    window.metadata[1]["is_completed"] = True
    update_auto_plot(window, window.metadata)
    queue.active[1].error = ValueError("older metadata")
    queue.active[1].done = True
    queue.poll()
    finish_one(queue)
    assert window.opened == ["late"]


def test_staged_database_switch_waits_for_commit_or_rollback(window):
    enqueue_auto_plot(window, ["one"])
    queue = window._auto_plot_queue
    queue.poll()
    request = queue.active[1]
    request.done = True
    old_service = window._trusted_read_service
    window._database_load_publication_active = True
    window._trusted_read_service = Service()
    queue.poll()
    assert not request.cancelled and not window.opened
    window._trusted_read_service = old_service
    window._database_load_publication_active = False
    queue.poll()
    assert window.opened == ["one"]


def test_stale_callback_does_not_cancel_a_new_auto_plot_session(window):
    enqueue_auto_plot(window, ["one"])
    old_queue = window._auto_plot_queue
    cancel_auto_plot(window)
    enqueue_auto_plot(window, ["two"])
    new_queue = window._auto_plot_queue
    old_queue.poll()
    assert window._auto_plot_queue is new_queue
    finish_one(new_queue)
    assert window.opened == ["two"]
