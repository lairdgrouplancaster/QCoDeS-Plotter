"""Full-view plot actions validate processed bounds before native mutation."""

from copy import deepcopy

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore, QtTest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from qplot.windows._commands import command_spec
from qplot.windows._subplots.subplot1d import custom_viewbox
from tests._window_lifecycle import close_main_window
from tests.windows.test_axis_auto_padding_overflow import (
    axis_messages as _axis_messages,
)
from tests.windows.test_axis_auto_padding_overflow import (
    callback_errors as _callback_errors,
)
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_native_fft_coordinates import fft_plots as _fft_plots
from tests.windows.test_plot_integration import (
    configure_temp_qplot,
    database_artifact_state,
    wait_for,
)

axis_messages = _axis_messages
callback_errors = _callback_errors
fft_plots = _fft_plots


def _plot_axis(fft_plots, axis):
    window, (plot, source), _x, _y = fft_plots
    if axis in ("x2", "y2"):
        merge(window, plot, source, x_axis="Top", y_axis="Right")
    plot.open_axis_scale_dialog(axis)
    plot._axis_scale_dialog.hide()
    return plot, plot._axis_scale_viewbox(axis), plot._axis_scale_axis_number(axis)


def _request(plot, route, qapplication):
    if route == "graphics":
        button = plot.plot.autoBtn
        button.show()
        position = plot.widget.mapFromScene(button.mapToScene(button.boundingRect().center()))
        QtTest.QTest.mouseClick(plot.widget.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=position)
    else:
        action = plot._context_menu_action(command_spec("plot.autoscale").text)
        assert action is not None
        invoked = []
        action.triggered.connect(lambda *_args: invoked.append(True))
        if route == "context":
            action.trigger()
        else:
            plot.activateWindow()
            qapplication.setActiveWindow(plot)
            plot.widget.setFocus()
            qapplication.processEvents()
            QtTest.QTest.keyClick(plot.widget, QtCore.Qt.Key.Key_0, QtCore.Qt.KeyboardModifier.ControlModifier)
        assert invoked == [True], "The configured Qt action must actually execute"
    qapplication.processEvents()


@pytest.mark.parametrize("axis,fft_plots", [
    (axis, {"x": np.array([-1e308, 1e308]), "y": np.array([1., 2.])})
    if axis in ("x", "x2") else
    (axis, {"x": np.array([1., 2.]), "y": np.array([-1e308, 1e308])})
    for axis in ("x", "y", "x2", "y2")
], indirect=["fft_plots"])
@pytest.mark.parametrize("route", ["graphics", "context", "shortcut"])
def test_global_auto_rejects_unsupported_axis_without_mutating_it(
    fft_plots, axis, route, axis_messages, qapplication,
):
    plot, viewbox, number = _plot_axis(fft_plots, axis)
    setter = viewbox.setXRange if number == 0 else viewbox.setYRange
    setter(.1, .9, padding=0)
    qapplication.processEvents()
    before = deepcopy(viewbox.viewRange()[number])
    controls = plot._axis_scale_controls[axis]
    assert controls.manualRadio.isChecked()
    original = {key: tuple(values.copy() for values in line.getOriginalDataset())
                for key, line in plot.lines.items()}
    axis_messages.clear()
    _request(plot, route, qapplication)
    assert viewbox.viewRange()[number] == before
    assert controls.manualRadio.isChecked()
    assert viewbox.autoRangeEnabled()[number] is False
    assert any(window is plot and "supported plot range" in message
               for window, message in axis_messages)
    for key, line in plot.lines.items():
        for current, recorded in zip(line.getOriginalDataset(), original[key], strict=True):
            np.testing.assert_array_equal(current, recorded)


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([1., 2.]), "y": np.array([0., float(2**60)])},
], indirect=True)
@pytest.mark.parametrize("axis", ["x", "y", "x2", "y2"])
@pytest.mark.parametrize("route", ["graphics", "context", "shortcut"])
def test_global_auto_includes_supported_primary_and_secondary_samples(
    fft_plots, axis, route, qapplication,
):
    plot, viewbox, number = _plot_axis(fft_plots, axis)
    setter = viewbox.setXRange if number == 0 else viewbox.setYRange
    setter(.1, .9, padding=0)
    qapplication.processEvents()
    _request(plot, route, qapplication)
    values = fft_plots[2 + number]
    lower, upper = viewbox.viewRange()[number]
    assert lower <= values.min() < values.max() <= upper
    assert plot._axis_scale_controls[axis].autoRadio.isChecked()


def test_standalone_full_view_and_owned_subset_keep_signal_and_padding_semantics(qapplication):
    widget = pg.GraphicsLayoutWidget()
    viewbox = custom_viewbox()
    widget.addItem(viewbox)
    near = pg.PlotDataItem([0., 1.], [0., 1.])
    far = pg.PlotDataItem([100., 101.], [100., 101.])
    viewbox.addItem(near)
    viewbox.addItem(far)
    signals = []
    callbacks = []
    viewbox.autoRange_triggered.connect(lambda: signals.append(True))
    try:
        viewbox.autoRange()
        assert len(signals) == 1
        assert viewbox.viewRange()[0][1] >= 101.
        viewbox.set_auto_range_handler(lambda: callbacks.append(True))
        before = deepcopy(viewbox.viewRange())
        viewbox.autoRange()
        assert callbacks == [True] and len(signals) == 2
        assert viewbox.viewRange() == before
        viewbox.autoRange(items=[near], padding=.1)
        assert callbacks == [True] and len(signals) == 3
        assert viewbox.viewRange()[0][0] < 0. < 1. < viewbox.viewRange()[0][1] < 10.
        viewbox.set_auto_range_handler(None)
        viewbox.autoRange()
        assert callbacks == [True] and len(signals) == 4
        assert viewbox.viewRange()[0][1] >= 101.
    finally:
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([1., 2.]), "y": np.array([3., 4.])},
    {"x": np.array([1., 2.]), "y": np.array([3., 4.]), "heatmap": True},
], indirect=True)
def test_line_and_heatmap_cut_context_applies_guard_once_and_notifies_once(
    fft_plots, monkeypatch, qapplication,
):
    _window, (plot, _source), _x, _y = fft_plots
    plot.vb.setXRange(.1, .9, padding=0)
    qapplication.processEvents()
    applications = []
    notifications = []
    original = plot.force_all_axes_autoscale

    def record():
        applications.append(True)
        original()

    monkeypatch.setattr(plot, "force_all_axes_autoscale", record)
    plot.vb.autoRange_triggered.connect(lambda: notifications.append(True))
    _request(plot, "context", qapplication)
    assert applications == [True]
    assert notifications == [True]
    assert plot.vb.viewRange()[0][0] <= 1. < 2. <= plot.vb.viewRange()[0][1]


@pytest.mark.parametrize("fft_plots", [
    {"x": np.array([1., 2.]), "y": np.array([3., 4.])},
], indirect=True)
def test_graphics_auto_disable_retains_all_ranges_and_synchronizes_modes(fft_plots, qapplication):
    plot, _viewbox, _number = _plot_axis(fft_plots, "y2")
    for axis in ("x", "y", "x2", "y2"):
        plot.open_axis_scale_dialog(axis)
    plot._axis_scale_dialog.hide()
    _request(plot, "graphics", qapplication)
    before = {axis: deepcopy(plot._axis_scale_viewbox(axis).viewRange())
              for axis in ("x", "y", "x2", "y2")}
    plot.plot.autoBtn.mode = "manual"
    _request(plot, "graphics", qapplication)
    for axis, previous in before.items():
        assert plot._axis_scale_controls[axis].manualRadio.isChecked()
        assert plot._axis_scale_viewbox(axis).viewRange() == previous


@pytest.fixture
def varied_cut(tmp_path, monkeypatch):
    """Record both unsupported and supported cut rows in a new QCoDeS file."""
    configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "varied-cut.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("varied cut", sample_name="owned")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x", paramtype="array")
    measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter("signal", paramtype="array", setpoints=("slow", "x"))
    values = np.array([[-1e308, 1e308], [3., 4.]])
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", np.tile([1., 2.], (2, 1))),
                             ("slow", np.array([[0., 0.], [1., 1.]])),
                             ("signal", values))
        guid = saver.dataset.guid
    finally:
        experiment.conn.close()
    protected = database_artifact_state(path)
    window = main_window.MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.close_database(status=False)
        assert window.load_file(str(path))
        wait_for(lambda: not window._database_load_active)
        window.openPlot(guid=guid, show=True)
        heatmap = window.windows[-1]
        wait_for(lambda: hasattr(heatmap, "dataGrid") and not heatmap.worker.running)
        heatmap.monitor.stop()
        heatmap.z_index = [0, 0]
        heatmap.openSweep("h")
        cut = window.windows[-1]
        wait_for(lambda: hasattr(cut, "axis_data") and not cut.worker.running)
        cut.monitor.stop()
        np.testing.assert_array_equal(heatmap.dataGrid, values)
        yield heatmap, cut, values
    finally:
        close_main_window(window)
        assert database_artifact_state(path) == protected


@pytest.mark.parametrize("route", ["graphics", "context", "shortcut"])
def test_real_cut_global_auto_rejects_unsupported_and_accepts_supported_rows(
    varied_cut, route, axis_messages, qapplication,
):
    heatmap, cut, values = varied_cut
    cut.open_axis_scale_dialog("y")
    cut._axis_scale_dialog.hide()
    controls = cut._axis_scale_controls["y"]
    cut.vb.setYRange(.1, .9, padding=0)
    qapplication.processEvents()
    axis_messages.clear()
    _request(cut, route, qapplication)
    assert cut.vb.viewRange()[1] == [.1, .9]
    assert controls.manualRadio.isChecked()
    assert any(window is cut and "supported plot range" in message
               for window, message in axis_messages)
    np.testing.assert_array_equal(cut.line.getOriginalDataset()[1], values[0])
    cut.picker.slider.setValue(1)
    qapplication.processEvents()
    assert cut.vb.viewRange()[1] == [.1, .9]
    assert controls.manualRadio.isChecked()
    _request(cut, route, qapplication)
    lower, upper = cut.vb.viewRange()[1]
    assert lower <= 3. < 4. <= upper
    assert controls.autoRadio.isChecked()
    np.testing.assert_array_equal(cut.line.getOriginalDataset()[1], values[1])
    np.testing.assert_array_equal(heatmap.dataGrid, values)


def test_real_cut_initial_auto_recovers_on_slider_and_refresh(varied_cut, qapplication):
    heatmap, cut, values = varied_cut
    cut.open_axis_scale_dialog("y")
    cut._axis_scale_dialog.hide()
    controls = cut._axis_scale_controls["y"]
    assert controls.autoRadio.isChecked()
    assert cut.vb.autoRangeEnabled()[1] is False
    assert np.all(np.isfinite(cut.vb.viewRange()))
    cut.picker.slider.setValue(1)
    qapplication.processEvents()
    assert controls.autoRadio.isChecked()
    lower, upper = cut.vb.viewRange()[1]
    assert lower <= 3. < 4. <= upper
    cut.refreshWindow(force=True)
    wait_for(lambda: not cut.worker.running)
    cut.monitor.stop()
    assert controls.autoRadio.isChecked()
    np.testing.assert_array_equal(cut.line.getOriginalDataset()[1], values[1])
    previous = deepcopy(cut.vb.viewRange()[1])
    cut.picker.slider.setValue(0)
    qapplication.processEvents()
    assert controls.autoRadio.isChecked()
    assert cut.vb.viewRange()[1] == previous
    np.testing.assert_array_equal(cut.line.getOriginalDataset()[1], values[0])
    np.testing.assert_array_equal(heatmap.dataGrid, values)
