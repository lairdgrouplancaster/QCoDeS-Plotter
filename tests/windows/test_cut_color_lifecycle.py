"""Cut colors remain usable before, during and after asynchronous publication."""

import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.windows._subplots.subplot2d import sweeper
from qplot.windows._widgets.dropbox import picker_1d
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)


@pytest.fixture
def heatmap(tmp_path, monkeypatch):
    configure_temp_qplot(monkeypatch, tmp_path)
    path = (tmp_path / "cut-colors.db").resolve()
    assert not path.exists()
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    with closing(connect(str(path))) as connection:
        assert Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve() == path
        experiment = load_or_create_experiment("cut colors", "lifecycle", conn=connection)
        measurement = Measurement(exp=experiment)
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast")
        measurement.register_custom_parameter("signal", setpoints=("slow", "fast"))
        with measurement.run(write_in_background=False) as saver:
            for slow in (10., 20., 30.):
                for fast in (1., 2., 4., 8.):
                    saver.add_result(("slow", slow), ("fast", fast), ("signal", slow + fast))
        guid = saver.dataset.guid
    protected = database_artifact_state(path)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        wait_for(lambda: len(window.windows) == 1)
        plot = window.windows[-1]
        wait_for(lambda: "dataGrid" in plot.__dict__ and not plot.worker.running)
        plot.monitor.stop()
        yield window, plot, errors
    finally:
        close_main_window(window)
        assert database_artifact_state(path) == protected
        assert not errors, [(kind.__name__, str(error)) for kind, error, _tb in errors]


def open_cut(window, plot):
    plot.z_index = [1, 1]
    plot.openSweep("h")
    return window.windows[-1]


def choose_color(cut, index):
    picker = cut.axes_dock.findChild(picker_1d)
    assert picker is not None and picker.color_box.isEnabled()
    picker.color_box.activated.emit(index)
    return picker.color_box.itemData(index).name()


def ready(cut):
    wait_for(lambda: "dataGrid" in cut.__dict__ and not cut.worker.running
             and cut._axis_change_transaction is None)
    cut.monitor.stop()


def assert_color_and_data(plot, cut, color):
    assert cut.line.opts["pen"].color().name() == color
    marker = plot.sweep_lines[cut.sweep_id]
    assert marker.pen.color().name() == color
    assert marker.isVisible()
    assert marker.value() == cut.fixed_value
    assert marker.angle == (0 if cut.fixed_indep == "slow" else 90)
    x, y = cut.line.getOriginalDataset()
    np.testing.assert_array_equal(y, x + cut.fixed_value)


@pytest.mark.parametrize("state", ["pending", "failed", "empty"])
def test_cut_color_before_first_data_and_after_initial_failure_or_empty_result(
    heatmap, monkeypatch, state,
):
    window, plot, errors = heatmap
    original_start = sweeper._refresh_worker_will_start
    failures = []

    def first_result(cut, worker):
        original_start(cut, worker)
        if state == "failed":
            def unavailable():
                raise RuntimeError("Temporary cut reader failure")
            monkeypatch.setattr(worker, "_prepare_trusted_prefix", unavailable)
        elif state == "empty":
            # Deliver an empty asynchronous result through the real publication
            # path; the following retry reads the unchanged QCoDeS recording.
            original_finish = worker._emit_finished

            def empty_result(finished):
                if finished:
                    worker.axis_data = {"x": np.asarray([]), "y": np.asarray([])}
                    worker.dataGrid = np.empty((0, 0))
                    worker.loaded_point_count = 0
                original_finish(finished)

            monkeypatch.setattr(worker, "_emit_finished", empty_result)

    monkeypatch.setattr(sweeper, "_refresh_worker_will_start", first_result)
    monkeypatch.setattr(sweeper, "show_error", lambda self, *args: failures.append(args))
    cut = open_cut(window, plot)
    assert "dataGrid" not in cut.__dict__
    if state != "pending":
        wait_for(lambda: not cut.worker.running)
        cut.monitor.stop()
        assert bool(failures) == (state == "failed")
        assert not cut.picker.slider.isEnabled()
    color = choose_color(cut, 1)
    assert not errors
    assert cut.sweep_id not in plot.sweep_lines
    monkeypatch.setattr(sweeper, "_refresh_worker_will_start", original_start)
    if state != "pending":
        cut.refreshWindow(force=True)
    ready(cut)
    assert_color_and_data(plot, cut, color)


def test_loaded_cut_color_preserves_samples_and_notifies_consumers(heatmap):
    window, plot, errors = heatmap
    cut = open_cut(window, plot)
    ready(cut)
    before = tuple(values.copy() for values in cut.line.getOriginalDataset())
    worker = cut.worker
    updates = []
    cut.trace_updated.connect(lambda: updates.append(True))
    color = choose_color(cut, 2)
    assert not errors and updates == [True]
    assert cut.worker is worker and not worker.running
    for actual, expected in zip(cut.line.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    assert_color_and_data(plot, cut, color)


def test_cut_recolored_before_first_publication_can_close(heatmap):
    window, plot, errors = heatmap
    cut = open_cut(window, plot)
    assert "dataGrid" not in cut.__dict__
    color = choose_color(cut, 1)
    assert not errors and cut.line.opts["pen"].color().name() == color
    cut.close()
    wait_for(lambda: not cut.worker.running)
    assert cut not in window.windows
    assert cut.sweep_id not in plot.sweep_lines
    assert not cut.monitor.isActive()


@pytest.mark.parametrize("cancel", [False, True], ids=["commit", "cancel"])
def test_cut_color_during_axis_change_uses_committed_position(heatmap, cancel):
    window, plot, errors = heatmap
    cut = open_cut(window, plot)
    ready(cut)
    before = tuple(values.copy() for values in cut.line.getOriginalDataset())
    window.post_admin()
    picker = next(box for box in plot.option_boxes if box.option_box.isEnabled())
    index = next(index for index in range(picker.option_box.count())
                 if picker.option_box.itemData(index) == cut._trace_key)
    picker.option_box.setCurrentIndex(index)
    overlay = plot.lines[cut._trace_key]
    for actual, expected in zip(overlay.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    cut.axis_dropdown["x"].setCurrentText("slow")
    transaction = cut._axis_change_transaction
    assert transaction is not None
    assert cut.axis_options == {"x": "slow", "y": "fast"}
    assert (cut.sweep_indep, cut.fixed_indep) == ("fast", "slow")
    color = choose_color(cut, 3)
    assert not errors
    # The requested selectors already show the other axis pair, but the data
    # and marker still describe the last committed cut until publication.
    assert_color_and_data(plot, cut, color)
    for actual, expected in zip(cut.line.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    for actual, expected in zip(overlay.getOriginalDataset(), before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    if cancel:
        cut.worker.cancel()
    ready(cut)
    assert_color_and_data(plot, cut, color)
    assert (cut.sweep_indep, cut.fixed_indep) == (
        ("fast", "slow") if cancel else ("slow", "fast")
    )
    if cancel:
        for actual, expected in zip(overlay.getOriginalDataset(), before, strict=True):
            np.testing.assert_array_equal(actual, expected)
    else:
        assert overlay.getOriginalDataset() == (None, None)
        cut.axis_dropdown["x"].setCurrentText("fast")
        ready(cut)
        for actual, expected in zip(
            overlay.getOriginalDataset(), cut.line.getOriginalDataset(), strict=True,
        ):
            np.testing.assert_array_equal(actual, expected)
    cut.picker.slider.setValue(cut.picker.slider.maximum())
    assert_color_and_data(plot, cut, color)
    for actual, expected in zip(
        overlay.getOriginalDataset(), cut.line.getOriginalDataset(), strict=True,
    ):
        np.testing.assert_array_equal(actual, expected)
