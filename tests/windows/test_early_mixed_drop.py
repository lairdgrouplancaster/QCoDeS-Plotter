"""Dropping a trace before the first heatmap render preserves axes and styles."""

from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtGui, QtTest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.windows import main as main_window
from qplot.windows.plot2d import plot2d
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _configure_temp_qplot,
    _database_artifact_state,
    _wait_for,
)
from tests.windows.test_mixed_plot_drops import _added, _drop


@pytest.fixture(params=[(False, True), (True, True), (True, False)],
                ids=["rendered", "render-pending", "unopened-trace"])
def early_mixed_plot(request, tmp_path, monkeypatch, qapplication):
    hold_render, preload_trace = request.param
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "early-mixed.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("early-mixed", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("frequency", unit="Hz")
    measurement.register_custom_parameter("S11", unit="", setpoints=("frequency",))
    with measurement.run() as saver:
        for x, y in zip((4e9, 6e9, 8e9), (.2, 1.5, .8), strict=True):
            saver.add_result(("frequency", x), ("S11", y))
        line_guid = saver.dataset.guid
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("gate", unit="V")
    measurement.register_custom_parameter("bias", unit="V")
    measurement.register_custom_parameter("charge_sensor", unit="A", setpoints=("bias", "gate"))
    with measurement.run() as saver:
        for y in (-.001, 0, .001):
            for x in (-.5, 0, .5):
                saver.add_result(("gate", x), ("bias", y), ("charge_sensor", x + y))
        heatmap_guid = saver.dataset.guid
    experiment.conn.close()
    artifacts = _database_artifact_state(path)
    original = plot2d.refreshPlot
    held = []
    release = not hold_render

    def gate_render(self, finished=True, worker=None):
        if finished and not release:
            held.append((self, finished, worker))
            return
        return original(self, finished, worker=worker)

    monkeypatch.setattr(plot2d, "refreshPlot", gate_render)
    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    try:
        window.close_database(status=False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        if preload_trace:
            window.open_run_preview_plot(line_guid, "S11")
            _wait_for(lambda: len(window.windows) == 1 and not window.windows[0].worker.running)
        existing = len(window.windows)
        window.open_run_preview_plot(heatmap_guid, "charge_sensor")
        _wait_for(lambda: len(window.windows) == existing + 1)
        target = window.windows[-1]
        if hold_render:
            _wait_for(lambda: bool(held))
            assert not hasattr(target, "bar")
        else:
            _wait_for(lambda: not target.worker.running and hasattr(target, "bar"))
        run = SimpleNamespace(guid=line_guid, name="S11", axes=("frequency",))
        _drop(target, run)
        line = _added(target, run)
        key = line.from_win._trace_key
        def finish_render():
            nonlocal release
            release = True
            while held:
                owner, finished, worker = held.pop(0)
                original(owner, finished, worker=worker)
            _wait_for(lambda: not target.worker.running and hasattr(target, "bar"))
            target.widget.grab()
            QtTest.QTest.qWait(50)

        yield target, key, line, finish_render
    finally:
        release = True
        while held:
            owner, finished, worker = held.pop(0)
            original(owner, finished, worker=worker)
        close_main_window(window)
        _assert_reader_artifact_invariant(path, artifacts)


def test_heatmap_axes_remain_independent_of_early_trace(early_mixed_plot):
    target, key, line, finish_render = early_mixed_plot
    finish_render()
    style = target._trace_styles[key]
    assert (style.x_axis, style.y_axis) == ("Top", "Right")
    np.testing.assert_allclose(target.dataGrid, [[-.501, -.001, .499], [-.5, 0, .5], [-.499, .001, .501]])
    lower, upper = target.vb.viewRange()[1]
    assert lower <= -.0015 and upper >= .0015
    assert upper - lower < .004, target.vb.viewRange()
    np.testing.assert_allclose(line.getData(), [[4e9, 6e9, 8e9], [.2, 1.5, .8]])


@pytest.mark.parametrize("custom_style", [False, True], ids=["default-style", "custom-style"])
def test_trace_colour_matches_appearance_after_first_heatmap_render(early_mixed_plot, custom_style):
    target, key, line, finish_render = early_mixed_plot
    if custom_style:
        style = target._trace_styles[key]
        style.line_color = "#123456"
        style.line_width = 4.5
        style.line_style = "Dash"
        target._apply_trace_style(key, line)
    finish_render()
    expected = QtGui.QColor(target._trace_styles[key].line_color).name()
    target.open_trace_appearance_dialog(key)
    dialog = target._plot_appearance_dialog
    assert dialog.line_color.currentData() == expected
    assert line.opts["pen"].color().name() == expected
    assert line.opts["pen"].widthF() == target._trace_styles[key].line_width
    dialog.reject()


def test_manual_heatmap_range_survives_pending_render_and_refresh(early_mixed_plot):
    target, _key, _line, finish_render = early_mixed_plot
    expected = (-.00075, .00075)
    target.vb.setYRange(*expected, padding=0)
    finish_render()
    np.testing.assert_allclose(target.vb.viewRange()[1], expected)
    target.refreshWindow(force=True)
    _wait_for(lambda: not target.worker.running)
    target.widget.grab()
    np.testing.assert_allclose(target.vb.viewRange()[1], expected)
