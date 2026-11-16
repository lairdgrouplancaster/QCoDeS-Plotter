"""Real plot startup recovery retains the displayed data and protected source."""

import numpy as np
import pytest
from PyQt6 import QtCore, sip

from qplot.windows._plot_refresh_ui import PlotRefreshUi
from tests.support.windows.refresh_interval_precision import (
    live_view,
    wait_for_plot_load,
)


@pytest.mark.parametrize("stage", [
    "create_worker", "prepare_worker", "connect_worker", "show_loading", "submit_worker",
])
@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
def test_plot_startup_failure_preserves_display_and_allows_retry(tmp_path, monkeypatch, stage, heatmap):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        plot = open_plot("signal_a")
        controller = plot._refresh_controller()
        ui = controller.host
        previous_axes = {name: values.copy() for name, values in plot.axis_data.items()}
        previous_grid = plot.dataGrid.copy() if heatmap else None
        prior_workers = set(window._plot_workers)
        settled = []
        plot.end_wait.connect(lambda: settled.append(True))
        original = getattr(ui, stage)

        def fail(*args):
            if stage not in {"create_worker", "submit_worker"}:
                original(*args)
            raise RuntimeError(f"injected {stage} failure")

        with monkeypatch.context() as fault:
            fault.setattr(ui, stage, fail)
            launched = plot.load_data()

        assert not launched
        assert not controller.busy
        assert set(window._plot_workers) == prior_workers
        assert settled == [True]
        assert not plot.plot_state_overlay.progress_timer.isActive()
        assert plot.plot_state_overlay.title_label.text() == "Plot could not start"
        assert stage in plot.plot_state_overlay.detail_label.text()
        for name, values in previous_axes.items():
            np.testing.assert_array_equal(plot.axis_data[name], values)
        if heatmap:
            np.testing.assert_array_equal(plot.dataGrid, previous_grid)
        failed = controller.session
        if failed.worker is not None:
            assert failed.worker.is_cancelled()
            assert not failed.worker.running
        assert plot.load_data()
        replacement = controller.session
        # Even callbacks already queued before a failed submission are fenced.
        controller.finish(failed, True)
        controller.status(failed, "obsolete loading message")
        assert controller.session is replacement
        wait_for_plot_load(window, plot)
        assert not controller.busy
        assert settled == [True, True]
        assert not plot.plot_state_overlay.progress_timer.isActive()
        assert plot.plot_state_overlay.title_label.text() != "Plot could not start"


def test_real_worker_completion_runs_on_gui_thread_and_releases_after_render(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, 0.) as (window, open_plot):
        plot = open_plot("signal_a")
        controller = plot._refresh_controller()
        original = plot.refreshPlot
        callbacks = []

        def publish(finished=True, worker=None):
            callbacks.append(QtCore.QThread.currentThread())
            assert controller.busy
            assert worker in window._plot_workers
            original(finished, worker=worker)
            assert controller.busy
            assert worker in window._plot_workers

        monkeypatch.setattr(plot, "refreshPlot", publish)
        assert plot.load_data()
        wait_for_plot_load(window, plot)
        assert callbacks == [window.thread()]
        assert not controller.busy
        assert controller.worker not in window._plot_workers


def test_synchronous_start_failure_does_not_enter_wait_loop(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, 0.) as (_window, open_plot):
        plot = open_plot("signal_a")

        def fail(_request):
            raise RuntimeError("construction failed")

        monkeypatch.setattr(plot._refresh_controller().host, "create_worker", fail)
        assert not plot.load_data(wait_on_thread=True)
        assert not plot._refresh_controller().busy


@pytest.mark.parametrize("heatmap", [False, True], ids=["line", "heatmap"])
def test_initial_worker_failure_leaves_window_retryable(tmp_path, monkeypatch, heatmap):
    with live_view(tmp_path, monkeypatch, 0., heatmap=heatmap) as (window, open_plot):
        source = open_plot("signal_a")

        def fail(_ui, _request):
            raise RuntimeError("initial construction failed")

        with monkeypatch.context() as fault:
            fault.setattr(PlotRefreshUi, "create_worker", fail)
            window.openWin(type(source), source._dataset_key, source.param, show=False)
        plot = window.windows[-1]
        assert plot is not source
        assert plot._plot_setup_complete
        assert not plot._refresh_controller().busy
        assert getattr(plot, "worker", None) is None
        assert plot.plot_state_overlay.title_label.text() == "Plot could not start"
        assert "initial construction failed" in plot.plot_status.message
        plot.refreshWindow(force=True)
        wait_for_plot_load(window, plot)
        assert plot.axis_data["x"].size
        assert not plot._refresh_controller().busy


def test_pool_registration_is_released_after_window_destruction(tmp_path, monkeypatch):
    with live_view(tmp_path, monkeypatch, 0.) as (window, open_plot):
        plot = open_plot("signal_a")
        monkeypatch.setattr(plot._refresh_controller().host, "submit_worker", lambda worker: None)
        assert plot.load_data()
        worker = plot.worker
        assert worker in window._plot_workers
        closed = plot.close()
        assert closed
        assert worker.is_cancelled()
        sip.delete(plot)
        worker.emitter.finished.emit(False)
        QtCore.QCoreApplication.processEvents()
        assert worker not in window._plot_workers
