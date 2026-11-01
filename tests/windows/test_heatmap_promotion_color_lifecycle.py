"""Ready overlays keep a shared, renderable scale while a new host loads."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore, sip
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

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


@pytest.fixture(params=[False, True], ids=["uniform", "nonuniform"])
def promotion_plot(request, tmp_path, monkeypatch):
    _configure_temp_qplot(monkeypatch, tmp_path)
    path = tmp_path / "promotion.db"
    initialise_or_create_database_at(str(path))
    experiment = load_or_create_experiment("promotion", sample_name="shared_scale")
    x_values = np.array([-2., 0., 5. if request.param else 2.])
    y_values = np.array([3., 9.])
    expected = {
        "primary": 100 + y_values[:, None] + x_values,
        "overlay": -20 + 2 * y_values[:, None] + x_values,
    }
    runs = {}
    connections = {id(experiment.conn): experiment.conn}
    for name in ("line", "primary", "overlay"):
        x = ManualParameter("x", unit="V")
        y = ManualParameter("y", unit="V")
        z = ManualParameter("signal_" + name, unit="A")
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        if name != "line":
            measurement.register_parameter(y)
        axes = (x,) if name == "line" else (y, x)
        measurement.register_parameter(z, setpoints=axes)
        with measurement.run() as saver:
            if name == "line":
                for value in x_values:
                    saver.add_result((x, value), (z, value))
            else:
                for row, y_value in enumerate(y_values):
                    for column, x_value in enumerate(x_values):
                        saver.add_result((y, y_value), (x, x_value), (z, expected[name][row, column]))
            runs[name] = SimpleNamespace(guid=saver.dataset.guid, name=z.name,
                                         axes=tuple(axis.name for axis in axes))
            connections[id(saver.dataset.conn)] = saver.dataset.conn
    for connection in connections.values():
        connection.close()
    artifacts = _database_artifact_state(path)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(qtw.QMessageBox, "critical", lambda *args: errors.append(args))
    main = main_window.MainWindow()
    main.startupDatabaseTimer.stop()
    main.monitor.stop()
    preferences = main.config.config["user_preference"]
    for option in ("confirm_close", "confirm_close_all", "confirm_incompatible_axes"):
        preferences[option] = False
    try:
        main.close_database(status=False)
        assert main.load_file(str(path))
        _wait_for(lambda: not main._database_load_active and main._selected_run_guid)
        main.openPlot(guid=runs["line"].guid)
        _wait_for(lambda: bool(main.windows))
        target = main.windows[-1]
        _wait_for(lambda: not target.worker.running and len(target.axis_data["x"]) == 3)
        for name in ("primary", "overlay"):
            _drop(target, runs[name])
            _added(target, runs[name])
        yield main, target, expected
    finally:
        close_main_window(main)
        assert sip.isdeleted(main)
        _assert_reader_artifact_invariant(path, artifacts)
        assert errors == []


def _promote_with_pending_primary(main, target, monkeypatch):
    """Hold only the new primary's publication, after real data loading."""
    sources = [layer.from_win for layer in target.heatmaps.values()]
    original = plot2d.refreshPlot
    publications = []

    def hold_new_primary(self, finished=True, worker=None):
        if self not in sources:
            publications.append((self, finished, worker))
            return None
        return original(self, finished, worker=worker)

    monkeypatch.setattr(plot2d, "refreshPlot", hold_new_primary)
    target.appearance_action.trigger()
    dialog = target._plot_appearance_dialog
    dialog.select_trace(next(key for key, line in target.lines.items() if line is target.line))
    previous = set(main.windows)
    dialog.remove_item_button.click()
    promoted = next(window for window in main.windows if window not in previous)
    assert promoted.operation_kind == "plot2d"
    assert promoted.param.name == "signal_primary"
    assert promoted.worker.running
    overlay = next(layer for layer in promoted.heatmaps.values() if layer is not promoted)
    return promoted, overlay, publications, original


def _assert_ready_overlay(host, overlay, expected, levels):
    np.testing.assert_array_equal(overlay.data_grid, expected)
    # Both renderers share the host scale, even when only one is visible.
    np.testing.assert_array_equal(overlay.image.getLevels(), levels)
    np.testing.assert_array_equal(overlay.heatmap_mesh.getLevels(), levels)
    np.testing.assert_array_equal(host.bar.levels(), levels)
    assert host.bar.isVisible()
    if overlay.geometry.is_uniform:
        assert overlay.image.isVisible()
        overlay.image.render()
        assert overlay.image.qimage is not None
    else:
        assert overlay.heatmap_mesh.isVisible()
    assert not host.grab().isNull()
    qtw.QApplication.processEvents()


@pytest.mark.parametrize("scale", ["auto_on", "auto_off", "manual_before", "manual_pending"])
def test_promotion_renders_before_first_primary_publication(promotion_plot, monkeypatch, scale):
    main, target, expected = promotion_plot
    manual = (-30., 130.)
    assert target.setColorbarColorMap("plasma")
    if scale == "manual_before":
        assert target.setColorbarManualRange(*manual)
    promoted, overlay, publications, original = _promote_with_pending_primary(main, target, monkeypatch)
    promoted.relevel_refresh.setChecked(scale == "auto_on")
    if scale == "manual_pending":
        promoted.colorbar_min_text.setText(str(manual[0]))
        promoted.colorbar_max_text.setText(str(manual[1]))
        promoted._apply_colorbar_manual_fields()
    levels = manual if scale.startswith("manual") else (expected["overlay"].min(), expected["overlay"].max())
    _assert_ready_overlay(promoted, overlay, expected["overlay"], levels)
    assert promoted.bar.colorMap().name == "plasma"
    _wait_for(lambda: bool(publications))
    monkeypatch.setattr(plot2d, "refreshPlot", original)
    for host, finished, worker in publications:
        original(host, finished, worker=worker)
    _wait_for(lambda: not promoted.worker.running)
    np.testing.assert_array_equal(promoted.dataGrid, expected["primary"])
    final_levels = manual if scale.startswith("manual") else (expected["overlay"].min(), expected["primary"].max())
    _assert_ready_overlay(promoted, overlay, expected["overlay"], final_levels)
    np.testing.assert_array_equal(promoted.image.getLevels(), final_levels)
    np.testing.assert_array_equal(promoted.heatmap_mesh.getLevels(), final_levels)
    assert promoted.bar.colorMap().name == "plasma"
    assert promoted.relevel_refresh.isChecked() == (scale == "auto_on")


@pytest.mark.parametrize("first_result", ["cancelled", "failed", "empty", "nonfinite"])
def test_ready_overlay_survives_missing_primary_and_retry(promotion_plot, monkeypatch, first_result):
    main, target, expected = promotion_plot
    promoted, overlay, publications, original = _promote_with_pending_primary(main, target, monkeypatch)
    promoted.relevel_refresh.setChecked(False)
    levels = (expected["overlay"].min(), expected["overlay"].max())
    _assert_ready_overlay(promoted, overlay, expected["overlay"], levels)
    _wait_for(lambda: bool(publications))
    monkeypatch.setattr(plot2d, "refreshPlot", original)
    host, finished, worker = publications[-1]
    if first_result == "cancelled":
        worker.cancel()
    elif first_result == "empty":
        worker.axis_data = {"x": np.array([]), "y": np.array([])}
        worker.dataGrid = np.empty((0, 0))
    elif first_result == "nonfinite":
        worker.dataGrid = np.full_like(worker.dataGrid, np.nan)
    # This controlled publication models a failed/cancelled/empty first load;
    # retry below uses the original real QCoDeS data without an injected result.
    promoted._refresh_pending = False
    promoted._refresh_pending_force = False
    original(host, False if first_result == "failed" else finished, worker=worker)
    assert not promoted.worker.running
    _assert_ready_overlay(promoted, overlay, expected["overlay"], levels)
    promoted.refreshWindow(force=True)
    _wait_for(lambda: not promoted.worker.running and np.array_equal(promoted.dataGrid, expected["primary"]))
    _assert_ready_overlay(promoted, overlay, expected["overlay"],
                          (expected["overlay"].min(), expected["primary"].max()))
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
