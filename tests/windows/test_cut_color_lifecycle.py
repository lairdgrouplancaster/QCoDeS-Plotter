"""Cut colors remain usable before, during and after asynchronous publication."""

from dataclasses import replace

import numpy as np
import pytest
from PyQt6 import sip

from qplot.windows._subplots.subplot2d import sweeper
from qplot.windows._widgets.dropbox import picker_1d
from tests.support.windows.cut_color_lifecycle import (
    heatmap as heatmap,
)
from tests.support.windows.cut_color_lifecycle import (
    ready as ready,
)
from tests.support.windows.plot_integration import (
    wait_for,
)


def open_cut(window, plot):
    plot.z_index = [1, 1]
    plot.openSweep("h")
    return window.windows[-1]


def choose_color(cut, index):
    picker = cut.axes_dock.findChild(picker_1d)
    assert picker is not None and picker.color_box.isEnabled()
    picker.color_box.activated.emit(index)
    return picker.color_box.itemData(index).name()


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

            def empty_result(finished, result=None):
                if finished:
                    assert result is not None
                    result = replace(
                        result,
                        axis_data={"x": np.asarray([]), "y": np.asarray([])},
                        dataGrid=np.empty((0, 0)),
                        loaded_point_count=0,
                    )
                original_finish(finished, result)

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
    assert sip.isdeleted(cut.monitor) or not cut.monitor.isActive()


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
