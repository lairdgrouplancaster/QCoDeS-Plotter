"""Actual line rendering rejects unrepresentable Qt coordinate ranges."""

import csv
import sys
import weakref

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtGui
from pyqtgraph.exporters import CSVExporter

from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_merged_trace_metadata import merge
from tests.windows.test_merged_trace_metadata import (
    merged_plots as _merged_plots_fixture,
)
from tests.windows.test_native_averaging import click_average
from tests.windows.test_native_transform_labels import click_control
from tests.windows.test_numeric_sample_preservation import array_plot
from tests.windows.test_plot_integration import wait_for

merged_plots = _merged_plots_fixture


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
@pytest.mark.parametrize("scale", [1e308, 1e-309, 1e-320])
def test_line_range_rejection_retains_real_samples_and_export(
        tmp_path, monkeypatch, qapplication, axis, scale):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: errors.append(error))
    extremes = np.array([-1., 0., 1.]) * scale
    x, y = ((extremes, np.array([0., 1., 0.])) if axis == "x"
            else (np.array([0., 1., 2.]), extremes))
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_main, plot):
        for size in ((500, 400), (1500, 900)):
            plot.resize(*size)
            _paint(plot, qapplication)
            assert plot.line.getData() == (None, None)
            assert not plot.line.curve.isVisible()
            assert plot.plot_state_overlay.frame.isVisible()
            assert "coordinate range" in plot.plot_state_overlay.title_label.text()
            assert "coordinate range" in plot.plot_status.message
        destination = tmp_path / "raw.csv"
        assert plot._write_line_csv_stage(str(destination), CSVExporter(plot.plot))
        with destination.open(newline="") as handle:
            rows = np.array([[float(value) for value in row] for row in list(csv.reader(handle))[1:]])
        np.testing.assert_array_equal(rows[:, 0], x)
        np.testing.assert_array_equal(rows[:, 1], y)
        for actual, original in zip(plot.line.getOriginalDataset(), (x, y), strict=True):
            np.testing.assert_array_equal(actual, original)
        # A later representable publication clears only this owned warning.
        plot.line.setData(x=np.arange(3.), y=np.arange(3.))
        plot.plot.enableAutoRange()
        wait_for(lambda: plot.line._qplot_precision_error is None)
        _paint(plot, qapplication)
        assert plot.line.curve.isVisible()
        assert not plot.plot_state_overlay.frame.isVisible()
    assert errors == []


def test_range_watch_detaches_and_reparents_without_data_access(qapplication):
    widgets = [pg.PlotWidget(), pg.PlotWidget()]
    line = NativePlotDataItem(np.arange(3.), np.arange(3.))
    first, second = [widget.getViewBox() for widget in widgets]
    try:
        first.addItem(line)
        assert line._qplot_range_viewbox is first
        first.sigTransformChanged.emit(first)
        assert line._qplot_range_timer.isActive()
        first.removeItem(line)
        assert line._qplot_range_viewbox is None
        assert not line._qplot_range_timer.isActive()
        first.sigTransformChanged.emit(first)
        assert not line._qplot_range_timer.isActive()
        second.addItem(line)
        assert line._qplot_range_viewbox is second
        line._qplot_range_timer.stop()
        first.sigTransformChanged.emit(first)
        assert not line._qplot_range_timer.isActive()
        second.sigTransformChanged.emit(second)
        assert line._qplot_range_timer.isActive()
        second.removeItem(line)
        assert not line._qplot_range_timer.isActive()
    finally:
        for widget in widgets:
            widget.close()
            widget.deleteLater()


def test_attachment_to_already_unsafe_viewport_is_validated(qapplication):
    widget = pg.PlotWidget()
    try:
        widget.show()
        qapplication.processEvents()
        widget.getViewBox().setRange(xRange=(-1e-309, 1e-309), yRange=(0., 1.), padding=0)
        qapplication.processEvents()
        line = NativePlotDataItem([0., 1.], [0., 1.])
        widget.addItem(line)
        wait_for(lambda: line._qplot_precision_error is not None)
        assert not line.curve.isVisible()
        assert line.getData() == (None, None)
    finally:
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("replacement", ["clear", "precision_error"])
def test_clear_releases_cached_measured_arrays(qapplication, replacement):
    widget = pg.PlotWidget()
    try:
        line = NativePlotDataItem(np.arange(100000.), np.arange(100000.))
        widget.addItem(line)
        prior = weakref.ref(line._qplot_range_dataset.x)
        if replacement == "clear":
            line.clear()
        else:
            line.setData(np.arange(2.), np.array([2**63, 2**63 + 1], dtype=np.uint64))
            assert line._qplot_precision_error is not None
        assert line._qplot_range_dataset is None
        assert prior() is None
    finally:
        widget.close()
        widget.deleteLater()


@pytest.mark.parametrize("x,y", [
    ([], []), ([1., 1.], [2., 2.]),
    ([-1e308, 0., 1., 1e308], [np.nan, 0., 1., np.nan]),
    ([-1e308, 1e308], [np.nan, np.nan]),
])
def test_missing_or_constant_pairs_do_not_reject_range(qapplication, x, y):
    widget = pg.PlotWidget()
    line = NativePlotDataItem(np.asarray(x), np.asarray(y))
    try:
        widget.addItem(line)
        line.getData()
        qapplication.processEvents()
        assert line._qplot_precision_error is None
    finally:
        widget.close()
        widget.deleteLater()


def test_resize_and_unsafe_zoom_update_average_and_recover(tmp_path, monkeypatch, qapplication):
    x = np.arange(3.)
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": x}]) as (_main, plot):
        click_average(plot)
        assert len(plot.plot.avgCurves) == 1
        cached_spans = plot.line._qplot_range_spans
        for width in (500, 1500):
            plot.resize(width, 500)
            _paint(plot, qapplication)
            assert len(plot.plot.avgCurves) == 1
            assert plot.line._qplot_range_spans is cached_spans
        plot.plot.setXRange(-1e-309, 1e-309, padding=0)
        _paint(plot, qapplication)
        wait_for(lambda: plot.line._qplot_precision_error is not None)
        wait_for(lambda: not plot.plot.avgCurves)
        assert not plot.line.curve.isVisible()
        assert plot.plot_state_overlay.frame.isVisible()
        plot.plot.setXRange(0., 2., padding=0)
        _paint(plot, qapplication)
        wait_for(lambda: plot.line._qplot_precision_error is None)
        wait_for(lambda: len(plot.plot.avgCurves) == 1)
        assert plot.line.curve.isVisible()


def test_native_log_processing_recovers_tiny_line(tmp_path, monkeypatch, qapplication):
    x, y = np.arange(3.), np.array([1., 2., 3.]) * 1e-309
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": y}]) as (_main, plot):
        _paint(plot, qapplication)
        assert plot.line._qplot_precision_error is not None
        click_control(plot, "logYCheck")
        _paint(plot, qapplication)
        wait_for(lambda: plot.line._qplot_precision_error is None)
        assert plot.line.curve.isVisible()
        np.testing.assert_allclose(plot.line.getData()[1], np.log10(y))
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[1], y)


def test_resize_crosses_supported_line_range(tmp_path, monkeypatch, qapplication):
    x = np.array([-1., 0., 1.]) * 3e-306
    with array_plot(tmp_path, monkeypatch, [{"x": x, "signal": np.arange(3.)}]) as (_main, plot):
        plot.resize(500, 400)
        _paint(plot, qapplication)
        assert plot.line._qplot_precision_error is None
        assert plot.line.curve.isVisible()
        plot.resize(2000, 600)
        _paint(plot, qapplication)
        wait_for(lambda: plot.line._qplot_precision_error is not None)
        assert not plot.line.curve.isVisible()
        assert plot.plot_state_overlay.frame.isVisible()
        np.testing.assert_array_equal(plot.line.getOriginalDataset()[0], x)


def test_secondary_line_viewport_rejection_and_removal(merged_plots, qapplication):
    window, (host, source, _third), _x, _y = merged_plots
    key, line = merge(window, host, source, x_axis="Top", y_axis="Right")
    _paint(host, qapplication)
    viewbox = line.getViewBox()
    assert viewbox is not host.plot.getViewBox()
    assert line._qplot_range_viewbox is viewbox
    host.top_vb.setXRange(-1e-309, 1e-309, padding=0)
    _paint(host, qapplication)
    wait_for(lambda: line._qplot_precision_error is not None)
    assert not line.curve.isVisible()
    assert host.plot_state_overlay.frame.isVisible()
    assert host.line._qplot_precision_error is None
    host.remove_line(source.label, trace_key=key)
    assert line._qplot_range_viewbox is None
    assert not line._qplot_range_timer.isActive()
