"""Finite acquired axes must not install infinite Qt heatmap transforms."""

import csv
import sys

import numpy as np
import pytest
from PyQt6 import QtGui, QtTest, QtWidgets
from pyqtgraph.exporters import CSVExporter

import qplot.windows._plot_refresh as refresh_module
from qplot.windows._plot2d_layers import HeatmapLayer
from tests.windows.test_mixed_plot_drops import _added, _drop
from tests.windows.test_mixed_plot_drops import mixed_plot as mixed_plot
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for


def _paint(plot, application):
    application.processEvents()
    picture = QtGui.QPicture()
    painter = QtGui.QPainter(picture)
    try:
        plot.widget.scene().render(painter)
    finally:
        painter.end()
    application.processEvents()


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("nonuniform", [False, True])
@pytest.mark.parametrize("scale", [1e308, 1e-309, 1e-320])
def test_heatmap_display_range_rejection_retains_samples_and_recovers(
        tmp_path, monkeypatch, qapplication, axis, nonuniform, scale):
    callback_errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: callback_errors.append(error))
    if not nonuniform:
        original_loader = refresh_module.loader

        def bounded_loader(*args, **kwargs):
            kwargs.update(max_full_heatmap_points=1, max_heatmap_grid_cells=4,
                          max_heatmap_grid_side=2)
            return original_loader(*args, **kwargs)

        monkeypatch.setattr(refresh_module, "loader", bounded_loader)
    large = np.array([-1., 0., .8]) * scale if nonuniform else np.array([-1., 0., 1.]) * scale
    x = large if axis == "x" else np.array([0., 1.])
    y = np.array([0., 1.]) if axis == "x" else large
    records = [{"x": x, "slow": np.full(len(x), coordinate),
                "signal": np.arange(len(x), dtype=float) + 100 * index}
               for index, coordinate in enumerate(y)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_main, plot):
        _paint(plot, qapplication)
        initial_worker = plot.worker
        initial_grid = plot.dataGrid.copy()
        # The rejected image has never established a meaningful viewport.
        # Its untouched default range must not trigger the 450ms SQL reload.
        QtTest.QTest.qWait(650)
        assert plot.worker is initial_worker
        np.testing.assert_array_equal(plot.dataGrid, initial_grid)
        assert plot._heatmap_color_range_rejected
        assert not plot.image.isVisible() and not plot.heatmap_mesh.isVisible()
        assert plot.plot_state_overlay.frame.isVisible()
        assert "coordinate range" in plot.plot_state_overlay.title_label.text()
        assert "coordinate range" in plot.plot_status.message
        assert plot._qplot_display_synchronized
        original_grid = plot.dataGrid.copy()
        original_axes = {name: values.copy() for name, values in plot.axis_data.items()}
        destination = tmp_path / "retained.csv"
        assert plot._write_heatmap_csv_stage(str(destination), CSVExporter(plot.plot))
        with destination.open(newline="") as handle:
            rows = list(csv.reader(handle))
        exported = np.array([[float(cell) for cell in row] for row in rows[1:]])
        np.testing.assert_array_equal(exported[:, 2], original_grid.ravel())
        np.testing.assert_array_equal(exported[:, 0], np.tile(original_axes["x"], len(original_axes["y"])))
        np.testing.assert_array_equal(exported[:, 1], np.repeat(original_axes["y"], len(original_axes["x"])))

        layer = HeatmapLayer(plot, plot)
        plot.heatmaps["range-test-overlay"] = layer
        try:
            assert layer.geometry is None
            assert "coordinate range" in layer.compatibility_error
            assert not layer.image.isVisible() and not layer.heatmap_mesh.isVisible()
            np.testing.assert_array_equal(layer.data_grid, original_grid)
            for image in (plot.image, layer.image):
                assert np.all(np.isfinite([image.transform().m11(), image.transform().m22()]))
            # A bounded viewport read of the same source must recover. It
            # selects a real recorded centre; no source data are modified.
            centre = float(large[1])
            span = scale / 10
            ranges = {axis: (centre, centre + span),
                      "y" if axis == "x" else "x": (0., 2.)}
            plot.config.config["runtime_settings"]["max_full_heatmap_points"] = 1
            if nonuniform:
                original_loader = refresh_module.loader

                def recovery_loader(*args, **kwargs):
                    kwargs.update(max_full_heatmap_points=1, max_heatmap_grid_cells=4,
                                  max_heatmap_grid_side=2)
                    return original_loader(*args, **kwargs)

                monkeypatch.setattr(refresh_module, "loader", recovery_loader)
            assert plot.load_data(force_sql_heatmap=True, heatmap_axis_ranges=ranges)
            worker = plot.worker
            wait_for(lambda: not worker.running and not plot._heatmap_color_range_rejected)
            plot.monitor.stop()
            assert not plot._heatmap_color_range_rejected, (
                plot.axis_data, plot.plot_state_overlay.title_label.text(),
                plot.plot_state_overlay.detail_label.text(), plot.plot_status.message,
            )
            assert plot.image.isVisible()
            assert layer.geometry is not None and layer.compatibility_error is None
            assert layer.image.isVisible()
            np.testing.assert_array_equal(plot.axis_data[axis], [0.])
            expected = [[1.], [101.]] if axis == "x" else [[100., 101.]]
            if nonuniform:
                # The cached full-resolution route applies the one-cell
                # publication budget after selecting the bounded viewport.
                expected = [[51.]] if axis == "x" else [[100.5]]
            np.testing.assert_array_equal(plot.dataGrid, expected)
            np.testing.assert_array_equal(layer.data_grid, expected)
            _paint(plot, qapplication)
        finally:
            plot.heatmaps.pop("range-test-overlay")
            layer.disconnect_source_updates()
            layer.remove_renderers()
    assert callback_errors == []


@pytest.mark.parametrize("nonuniform", [False, True])
@pytest.mark.parametrize("scale", [1e150, 1e-150, 1e-300])
def test_finite_display_spans_remain_supported(tmp_path, monkeypatch, qapplication, nonuniform, scale):
    callback_errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: callback_errors.append(error))
    x = np.array([-1., -.25, 1.]) * scale if nonuniform else np.array([-1., 0., 1.]) * scale
    records = [{"x": x, "slow": np.full(3, row), "signal": np.arange(3.) + 10 * row}
               for row in (0., 1.)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_main, plot):
        layer = HeatmapLayer(plot, plot)
        plot.heatmaps["range-test-overlay"] = layer
        try:
            plot._sync_heatmap_colorbar_items(rescale=True)
            _paint(plot, qapplication)
            assert not plot._heatmap_color_range_rejected
            assert plot.image.isVisible() != nonuniform
            assert plot.heatmap_mesh.isVisible() == nonuniform
            assert layer.geometry is not None and layer.compatibility_error is None
            assert layer.image.isVisible() != nonuniform
            assert layer.heatmap_mesh.isVisible() == nonuniform
            assert np.all(np.isfinite(plot._required_heatmap_geometry().rect))
            for renderer in (plot.heatmap_mesh, layer.heatmap_mesh) if nonuniform else (plot.image, layer.image):
                transform = renderer.sceneTransform()
                assert np.all(np.isfinite([transform.m11(), transform.m22(), transform.dx(), transform.dy()]))
        finally:
            plot.heatmaps.pop("range-test-overlay")
            layer.disconnect_source_updates()
            layer.remove_renderers()
    assert callback_errors == []


def test_heatmap_resize_rejection_and_explicit_refresh_recovery(tmp_path, monkeypatch, qapplication):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    x = np.array([-1., 0., 1.]) * 3e-306
    records = [{"x": x, "slow": np.full(3, row), "signal": np.arange(3.) + 10 * row}
               for row in (0., 1.)]
    with array_plot(tmp_path, monkeypatch, records, heatmap=True) as (_main, plot):
        _paint(plot, qapplication)
        assert not plot._heatmap_color_range_rejected
        expected = plot.dataGrid.copy()
        plot.resize(3000, 600)
        wait_for(lambda: plot._heatmap_color_range_rejected)
        _paint(plot, qapplication)
        assert not plot.image.isVisible()
        assert "coordinate range" in plot.plot_state_overlay.title_label.text()
        np.testing.assert_array_equal(plot.dataGrid, expected)
        plot.resize(500, 600)
        qapplication.processEvents()
        plot.refreshWindow(force=True)
        wait_for(lambda: not plot.worker.running and not plot._heatmap_color_range_rejected)
        plot.monitor.stop()
        _paint(plot, qapplication)
        assert plot.image.isVisible()
        assert np.isfinite(plot.image.sceneTransform().m11())
        np.testing.assert_array_equal(plot.dataGrid, expected)
    assert errors == []


def test_overlay_unsafe_zoom_after_axis_assignment_recovers_and_removes(mixed_plot, qapplication, monkeypatch):
    _main, plot, runs = mixed_plot
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda box: QtWidgets.QMessageBox.StandardButton.Ok)
    _drop(plot, runs["heatmap"] if plot.operation_kind == "plot1d" else runs["other_heatmap"])
    run = runs["heatmap"] if plot.operation_kind == "plot1d" else runs["other_heatmap"]
    layer = _added(plot, run)
    plot._set_layer_axes(layer, "Top", "Right")
    _paint(plot, qapplication)
    expected = layer.data_grid.copy()
    plot.top_vb.setXRange(-1e-309, 1e-309, padding=0)
    wait_for(lambda: layer.geometry is None)
    assert "coordinate range" in layer.compatibility_error
    assert not layer.image.isVisible() and not layer.heatmap_mesh.isVisible()
    np.testing.assert_array_equal(layer.data_grid, expected)
    plot.top_vb.setXRange(-1., 3., padding=0)
    layer.from_win.refreshWindow(force=True)
    wait_for(lambda: layer.geometry is not None and not layer.from_win.worker.running)
    layer.from_win.monitor.stop()
    _paint(plot, qapplication)
    assert layer.compatibility_error is None
    assert layer.image.isVisible()
    np.testing.assert_array_equal(layer.data_grid, expected)
    assert plot.remove_heatmap(trace_key=layer.from_win._trace_key)
    plot.top_vb.setXRange(-1e-309, 1e-309, padding=0)
    _paint(plot, qapplication)
