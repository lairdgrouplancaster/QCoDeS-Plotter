"""Real dataset drops must choose axes, retain samples, and honour cancellation."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

from qplot.configuration.config import config
from qplot.windows import main as main_window
from qplot.windows._plot_drop_axes import DropAxisPlan, plan_drop_axes
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._window_controls import CONFIRM_INCOMPATIBLE_AXES_KEY
from tests._window_lifecycle import close_main_window
from tests.windows.test_heatmap_layer_integration import (
    _assert_reader_artifact_invariant,
    _configure_temp_qplot,
    _database_artifact_state,
    _drop_heatmap,
    _wait_for,
)


@pytest.fixture(params=[1, 2], ids=["line", "heatmap"])
def drop_plots(request, tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "drop-axes.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("drop-axes", sample_name="sample")
    dimensions = request.param
    specifications = [
        ("a", ("x", "y"), ("V", "s"), "A"),
        ("b", ("u", "v"), ("K", "T"), "V" if dimensions == 1 else "A"),
        ("c", ("x", "v"), ("V", "T"), "V" if dimensions == 1 else "A"),
        ("d", ("x", "y"), ("V", "s"), "Hz" if dimensions == 1 else "V"),
        ("e", ("u", "w"), ("K", "m"), "Hz" if dimensions == 1 else "A"),
        ("f", ("z", "y"), ("m", "s"), "V" if dimensions == 1 else "A"),
        # The same parameter name with different units needs its own axis.
        ("g", ("x", "y"), ("mV", "ms"), "A"),
    ]
    runs = []
    connections = {id(experiment.conn): experiment.conn}
    for name, names, units, value_unit in specifications:
        coordinates = [ManualParameter(axis, unit=unit) for axis, unit in
                       zip(names[:dimensions], units[:dimensions], strict=True)]
        signal = ManualParameter(name, unit=value_unit)
        measurement = Measurement(exp=experiment)
        for coordinate in coordinates:
            measurement.register_parameter(coordinate)
        measurement.register_parameter(signal, setpoints=tuple(coordinates))
        with measurement.run() as saver:
            for row in range(2 if dimensions == 2 else 1):
                for column in range(3):
                    values = (column, row)[:dimensions]
                    saver.add_result(*zip(coordinates, values, strict=True),
                                     (signal, column + 10 * row))
            runs.append((saver.dataset.guid, name))
        connections[id(saver.dataset.conn)] = saver.dataset.conn
    for connection in connections.values():
        connection.close()
    artifacts = _database_artifact_state(path)

    window = main_window.MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    exceptions = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: exceptions.append(args))
    try:
        window.close_database(status=False)
        assert window.load_file(str(path))
        _wait_for(lambda: not window._database_load_active and window._selected_run_guid)
        class PlotSources(dict):
            def __missing__(self, name):
                guid = next(guid for guid, parameter in runs if parameter == name)
                count = len(window.windows)
                window.openPlot(guid=guid)
                _wait_for(lambda: len(window.windows) > count)
                plot = window.windows[-1]
                _wait_for(lambda: not plot.worker.running and (
                    hasattr(plot, "dataGrid") if dimensions == 2 else len(plot.axis_data["x"]) == 3
                ))
                plot.monitor.stop()
                self[name] = plot
                return plot

        plots = PlotSources()
        yield window, plots, dimensions
        assert exceptions == []
    finally:
        close_main_window(window)
        _assert_reader_artifact_invariant(path, artifacts)


def _layers(target, dimensions):
    return target.lines if dimensions == 1 else target.heatmaps


def _sides(target, layer, dimensions):
    if dimensions == 2:
        return target._heatmap_axis_sides(layer)
    style = target._trace_styles[target._window_trace_key(layer.from_win)]
    return style.x_axis, style.y_axis


def _drop(target, source):
    # Both line and heatmap drops use the same MIME/event entry point.
    _drop_heatmap(target, source._dataset_key.guid, source.param)


def _add(target, source, dimensions):
    _drop(target, source)
    _wait_for(lambda: source._trace_key in _layers(target, dimensions))
    layer = _layers(target, dimensions)[source._trace_key]
    _wait_for(lambda: layer.geometry is not None if dimensions == 2 else
              layer.getData()[0] is not None and len(layer.getData()[0]) > 0)
    return layer


def _mock_warning(monkeypatch, response):
    """Inspect the real checkbox-enabled warning while selecting its result."""
    def respond(box):
        assert box.checkBox().text() == "Do not show again"
        return response(box.parentWidget(), box.windowTitle(), box.text(),
                        box.standardButtons(), box.standardButton(box.defaultButton()))

    monkeypatch.setattr(qtw.QMessageBox, "exec", respond)


def test_drop_reuses_matching_and_unused_axes(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    target = plots["a"]

    def unexpected_dialog(*args):
        pytest.fail(f"Compatible and unused axes should not prompt: {args}")

    _mock_warning(monkeypatch, unexpected_dialog)
    spare = _add(target, plots["b"], dimensions)
    assert _sides(target, spare, dimensions) == ("Top", "Right")
    shared = _add(target, plots["c"], dimensions)
    assert _sides(target, shared, dimensions) == (
        ("Bottom", "Right") if dimensions == 1 else ("Top", "Left")
    )
    if dimensions == 1:
        assert plots["b"] not in target.mergable
        assert plots["c"] not in target.mergable
    if dimensions == 1:
        np.testing.assert_array_equal(spare.getData(), [[0, 1, 2], [0, 1, 2]])
    else:
        np.testing.assert_array_equal(spare.data_grid, [[0, 10], [1, 11], [2, 12]])
        assert target.plot.getAxis("top").labelText == "v"
        assert target.plot.getAxis("right").labelText == "u"
    # Dropping the same dataset again leaves the registry untouched.
    _drop(target, plots["b"])
    assert len(_layers(target, dimensions)) == 3


def test_drop_matches_units_as_well_as_coordinate_names(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    _mock_warning(monkeypatch, lambda *args: pytest.fail(str(args)))
    target = plots["a"]
    layer = _add(target, plots["g"], dimensions)
    assert _sides(target, layer, dimensions) == (
        ("Top", "Left") if dimensions == 1 else ("Top", "Right")
    )


def test_incompatible_drop_can_be_cancelled_then_approved(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    target = plots["a"]
    _add(target, plots["b"], dimensions)
    dialogs = []

    def warning(*args):
        dialogs.append(args)
        return qtw.QMessageBox.StandardButton.Cancel

    _mock_warning(monkeypatch, warning)
    before = dict(_layers(target, dimensions))
    _drop(target, plots["d"])
    assert _layers(target, dimensions) == before
    assert getattr(plots["d"], "_merged_trace_users", 0) == 0
    assert len(dialogs) == 1
    assert "The axes are incompatible" in dialogs[0][2]
    assert dialogs[0][-1] == qtw.QMessageBox.StandardButton.Cancel
    if dimensions == 2:
        assert "colorscale units do not match" in dialogs[0][2]
    _mock_warning(monkeypatch, lambda *args: qtw.QMessageBox.StandardButton.Ok)
    added = _add(target, plots["d"], dimensions)
    assert _sides(target, added, dimensions) == ("Bottom", "Left")
    added.refresh(source_ready=True)
    if dimensions == 2:
        assert added.image.isVisible()
        assert added.geometry is not None
    else:
        np.testing.assert_array_equal(added.getData()[1], [0, 1, 2])


def test_approved_drop_keeps_compatible_axes_before_defaulting(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    target = plots["a"]
    _add(target, plots["b"], dimensions)
    dialogs = []
    _mock_warning(monkeypatch, lambda *args:
                        dialogs.append(args) or qtw.QMessageBox.StandardButton.Ok)
    partly_matching = _add(target, plots["e"], dimensions)
    assert _sides(target, partly_matching, dimensions) == (
        ("Top", "Left") if dimensions == 1 else ("Bottom", "Right")
    )
    second = _add(target, plots["f"], dimensions)
    assert _sides(target, second, dimensions) == (
        ("Bottom", "Right") if dimensions == 1 else ("Bottom", "Left")
    )
    assert len(dialogs) == 2


def test_suppressed_warning_still_chooses_compatible_axes(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    target = plots["a"]
    _add(target, plots["b"], dimensions)

    def approve_and_disable(box):
        box.checkBox().setChecked(True)
        return qtw.QMessageBox.StandardButton.Ok

    monkeypatch.setattr(qtw.QMessageBox, "exec", approve_and_disable)
    _add(target, plots["d"], dimensions)
    assert not config().get(CONFIRM_INCOMPATIBLE_AXES_KEY)
    _mock_warning(monkeypatch, lambda *args: pytest.fail("Disabled warning was shown"))
    layer = _add(target, plots["e"], dimensions)
    assert _sides(target, layer, dimensions) == (
        ("Top", "Left") if dimensions == 1 else ("Bottom", "Right")
    )


def test_dropped_axes_and_samples_follow_host_and_source_swaps(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    _mock_warning(monkeypatch, lambda *args: pytest.fail(str(args)))
    target, source = plots["a"], plots["b"]
    layer = _add(target, source, dimensions)
    original = layer.data_grid.copy() if dimensions == 2 else np.asarray(layer.getData()).copy()
    source.set_plot_axes_swapped(True)
    _wait_for(lambda: not source.worker.running)
    layer.refresh(source_ready=True)
    np.testing.assert_array_equal(layer.data_grid if dimensions == 2 else layer.getData(), original)
    target.set_plot_axes_swapped(True)
    _wait_for(lambda: not target.worker.running)
    layer.refresh(source_ready=True)
    assert _sides(target, layer, dimensions) == ("Top", "Right")
    np.testing.assert_array_equal(layer.data_grid if dimensions == 2 else layer.getData(),
                                  original.T if dimensions == 2 else original[::-1])
    # A subsequent drop still sees the coordinates actually assigned to each side.
    plan = plan_drop_axes(target, source)
    assert not plan.incompatible
    assert (plan.x_side, plan.y_side) == ("Top", "Right")


def test_unused_primary_axes_are_preferred_when_primary_dataset_moves(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    target = plots["a"]
    if dimensions == 2:
        target._set_layer_axes(target, "Top", "Right")
    else:
        style = target._trace_styles[target.label]
        style.x_axis, style.y_axis = "Top", "Right"
        target._apply_trace_style(target.label, target.line)
    _mock_warning(monkeypatch, lambda *args: pytest.fail(str(args)))
    layer = _add(target, plots["b"], dimensions)
    assert _sides(target, layer, dimensions) == ("Bottom", "Left")


def test_colorscale_mismatch_warns_even_with_unused_coordinate_axes(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    if dimensions != 2:
        pytest.skip("Only heatmaps have a colorscale")
    target, source = plots["a"], plots["b"]
    monkeypatch.setattr(source, "display_param", SimpleNamespace(unit="V"))
    dialogs = []
    _mock_warning(monkeypatch, lambda *args:
                        dialogs.append(args) or qtw.QMessageBox.StandardButton.Ok)
    layer = _add(target, source, dimensions)
    assert len(dialogs) == 1
    assert "colorscale units do not match" in dialogs[0][2]
    assert _sides(target, layer, dimensions) == ("Top", "Right")
    assert layer.image.isVisible()


def test_native_derivative_drop_matches_the_axes_it_will_render(drop_plots, monkeypatch):
    _window, plots, dimensions = drop_plots
    if dimensions != 1:
        pytest.skip("Native derivative controls are for line plots")
    target, source = plots["a"], plots["d"]
    # This source has the same raw units; the host will render both as A/V.
    monkeypatch.setattr(source, "axis_param", {
        "x": source.axis_param["x"],
        "y": SimpleNamespace(name="d", label="d", unit="A"),
    })
    target.plot.ctrl.derivativeCheck.setChecked(True)
    _mock_warning(monkeypatch, lambda *args: pytest.fail(str(args)))
    layer = _add(target, source, dimensions)
    assert _sides(target, layer, dimensions) == ("Bottom", "Left")
    assert target._trace_axis_parameter(layer, "y").unit == "A/V"


@pytest.fixture
def warning_target(tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    parent = qtw.QWidget()
    parent.config = config()
    try:
        yield parent
    finally:
        parent.deleteLater()


@pytest.mark.parametrize("dismiss", ["close", "escape"])
@pytest.mark.parametrize("checked", [False, True])
def test_warning_can_be_dismissed(warning_target, dismiss, checked):
    source = SimpleNamespace(label="test measurement")
    plan = DropAxisPlan(("x", "y"), "Bottom", "Left", ("The axis units differ.",))

    def dismiss_dialog():
        dialog = qtw.QApplication.activeModalWidget()
        assert isinstance(dialog, qtw.QMessageBox)
        dialog.checkBox().setChecked(checked)
        if dismiss == "close":
            dialog.close()
        else:
            from PyQt6.QtTest import QTest
            QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)

    QtCore.QTimer.singleShot(0, dismiss_dialog)
    assert not plan.approve(warning_target, source)
    assert warning_target.config.get(CONFIRM_INCOMPATIBLE_AXES_KEY)
    assert config().get(CONFIRM_INCOMPATIBLE_AXES_KEY)


@pytest.mark.parametrize("checked", [False, True])
def test_warning_preference_persists_and_can_be_reenabled(warning_target, monkeypatch, checked):
    plan = DropAxisPlan(("x", "y"), "Bottom", "Left", ("The axis units differ.",))
    source = SimpleNamespace(label="test measurement")

    def approve(box):
        assert box.checkBox().text() == "Do not show again"
        box.checkBox().setChecked(checked)
        return qtw.QMessageBox.StandardButton.Ok

    monkeypatch.setattr(qtw.QMessageBox, "exec", approve)
    assert plan.approve(warning_target, source)
    warning_target.config = config()
    assert warning_target.config.get(CONFIRM_INCOMPATIBLE_AXES_KEY) == (not checked)
    if checked:
        _mock_warning(monkeypatch, lambda *args: pytest.fail("Disabled warning was shown"))
        assert plan.approve(warning_target, source)
        # Users can restore the warning through Preferences without resetting other settings.
        dialog = PreferencesDialog(warning_target.config)
        try:
            assert not dialog.confirmIncompatibleAxesCheck.isChecked()
            dialog.confirmIncompatibleAxesCheck.setChecked(True)
            assert dialog.apply_preferences()
        finally:
            dialog.deleteLater()
        assert config().get(CONFIRM_INCOMPATIBLE_AXES_KEY)
    _mock_warning(monkeypatch, lambda *args: qtw.QMessageBox.StandardButton.Cancel)
    assert not plan.approve(warning_target, source)


def test_failed_warning_preference_save_keeps_prompt_enabled(warning_target, monkeypatch):
    plan = DropAxisPlan(("x", "y"), "Bottom", "Left", ("The axis units differ.",))
    errors = []
    warning_target.show_error = lambda *args: errors.append(args)

    def approve(box):
        box.checkBox().setChecked(True)
        return qtw.QMessageBox.StandardButton.Ok

    def fail_save(*args):
        raise OSError("settings write failed")

    monkeypatch.setattr(qtw.QMessageBox, "exec", approve)
    monkeypatch.setattr(warning_target.config, "save_config", fail_save)
    assert plan.approve(warning_target, SimpleNamespace(label="test measurement"))
    assert warning_target.config.get(CONFIRM_INCOMPATIBLE_AXES_KEY)
    assert config().get(CONFIRM_INCOMPATIBLE_AXES_KEY)
    assert len(errors) == 1
    assert errors[0][0] == "Settings Not Saved"
