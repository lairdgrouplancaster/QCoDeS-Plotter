"""Native averages follow their sources' actual clipped display samples."""

import numpy as np
import pyqtgraph as pg
import pytest
from PyQt6 import QtCore

from qplot.windows._native_averaging import NativePlotItem
from qplot.windows._native_transforms import NativePlotDataItem


def _settle(app):
    for _ in range(12):
        app.processEvents()


@pytest.mark.parametrize("secondary", [False, True])
def test_average_rebuilds_after_zoom_without_view_feedback(qapplication, monkeypatch, secondary):
    plot = NativePlotItem()
    host = pg.PlotWidget(plotItem=plot)
    host.resize(600, 400)
    host.show()
    x = np.arange(100_000, dtype=float)
    curves = [NativePlotDataItem(x, np.sin(x) + offset) for offset in (0., 2.)]
    viewbox = plot.vb
    try:
        for curve in curves:
            plot.addItem(curve)
        if secondary:
            viewbox = pg.ViewBox()
            plot.scene().addItem(viewbox)
            viewbox.setGeometry(plot.vb.sceneBoundingRect())
            for curve in curves:
                plot.vb.removeItem(curve)
                viewbox.addItem(curve)
        plot.setDownsampling(auto=True, mode="mean")
        plot.setClipToView(True)
        viewbox.setXRange(0, 99_999, padding=0)
        _settle(qapplication)
        plot.ctrl.averageGroup.setChecked(True)
        initial_size = len(next(iter(plot.avgCurves.values()))[1].getData()[0])
        calls = []
        original = plot.recomputeAverages

        def record():
            calls.append(True)
            original()

        monkeypatch.setattr(plot, "recomputeAverages", record)
        for start in (200, 500, 1000):
            viewbox.setXRange(start, start + 50, padding=0)
        _settle(qapplication)
        count, average = next(iter(plot.avgCurves.values()))
        assert count == 2
        displayed = [curve.getData() for curve in curves]
        assert len(displayed[0][0]) < initial_size
        np.testing.assert_array_equal(average.getData()[0], displayed[0][0])
        np.testing.assert_allclose(average.getData()[1], (displayed[0][1] + displayed[1][1]) / 2)
        assert 1 <= len(calls) <= 3
        completed_calls = len(calls)
        _settle(qapplication)
        assert len(calls) == completed_calls
        assert not plot._qplot_average_refresh_timer.isActive()
    finally:
        monkeypatch.undo()
        plot.clear()
        if secondary:
            plot.scene().removeItem(viewbox)
        host.close()


def test_queued_average_update_ignores_removed_sources_and_deleted_plot(qapplication):
    plot = NativePlotItem()
    host = pg.PlotWidget(plotItem=plot)
    curve = NativePlotDataItem(np.arange(100), np.arange(100))
    plot.addItem(curve)
    plot.ctrl.averageGroup.setChecked(True)
    curve.sigDisplayChanged.emit(curve)
    assert plot._qplot_average_refresh_timer.isActive()
    plot.removeItem(curve)
    _settle(qapplication)
    assert not plot.avgCurves
    assert not plot._qplot_average_display_sources
    curve.sigDisplayChanged.emit(curve)
    assert not plot._qplot_average_refresh_timer.isActive()
    plot.addItem(curve)
    curve.sigDisplayChanged.emit(curve)
    host.close()
    host.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    _settle(qapplication)
