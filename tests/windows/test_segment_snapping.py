"""Line capture uses connected segments while reporting actual samples."""

from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6 import QtCore

from qplot.windows import _plot1d_snap as snap
from qplot.windows._native_transforms import NativePlotDataItem
from tests.windows.test_plot_overlay_integration import loaded_plot as loaded_plot


@pytest.fixture
def trace_host(qapplication):
    import pyqtgraph as pg

    widget = pg.PlotWidget()
    widget.resize(1000, 700)
    plot = widget.getPlotItem()
    line = NativePlotDataItem([0, 10], [5, 5], connect="finite")
    plot.addItem(line)
    plot.setRange(xRange=[-1, 11], yRange=[-1, 11], padding=0)
    widget.show()
    qapplication.processEvents()
    host = SimpleNamespace(lines={"trace": line}, _viewbox_for_line=lambda _line: plot.vb)
    try:
        yield host, line, plot.vb, widget
    finally:
        widget.close()
        widget.deleteLater()


def nearest(host, point):
    return snap.Plot1DSnapMixin._nearest_trace_point(host, point)


@pytest.mark.parametrize("points", [
    ([0, 10], [5, 5]), ([5, 5], [0, 10]), ([0, 10], [0, 10]),
    ([10, 0], [5, 5]), ([0, 10, 0], [0, 10, 10]),
])
@pytest.mark.parametrize("appearance", ["line", "scatter", "both"])
def test_sparse_segments_capture_between_samples_in_screen_pixels(trace_host, points, appearance):
    host, line, vb, _widget = trace_host
    x, y = points
    line.setData(x, y, pen=None if appearance == "scatter" else "b",
                 symbol="o" if appearance != "line" else None)
    for scale in (1, 2):
        vb.setRange(xRange=[-scale, 10 + scale], yRange=[-scale, 10 + scale], padding=0)
        a, b = (vb.mapViewToScene(QtCore.QPointF(x[i], y[i])) for i in (0, 1))
        centre = a * .6 + b * .4
        tangent = b - a
        normal = QtCore.QPointF(-tangent.y(), tangent.x()) / np.hypot(tangent.x(), tangent.y())
        result = nearest(host, centre + 11 * normal)
        if appearance == "scatter":
            assert result is None
        else:
            assert result is not None
            assert (result.point_number, result.x_value, result.y_value) == (1, x[0], y[0])
        assert nearest(host, centre + 13 * normal) is None


@pytest.mark.parametrize("connection", ["finite", "auto", "pairs", "mask", "all"])
def test_capture_respects_rendered_connections_and_gaps(trace_host, connection):
    host, line, vb, _widget = trace_host
    if connection in ("finite", "auto", "all"):
        x, y = [0, 4, np.nan, 6, 10], [5, 5, np.nan, 5, 5]
    else:
        x, y = [0, 4, 6, 10], [5, 5, 5, 5]
    connect = np.array([True, False, True, False]) if connection == "mask" else connection
    line.setData(x, y, connect=connect)
    result = nearest(host, vb.mapViewToScene(QtCore.QPointF(5, 5)))
    assert (result is not None) == (connection == "all")
    assert nearest(host, vb.mapViewToScene(QtCore.QPointF(2, 5))) is not None


def test_index_keeps_long_segments_crossing_block_boundaries(trace_host):
    host, line, vb, _widget = trace_host
    line.setData(np.repeat([0., 10.], 256), np.full(512, 5.))
    result = nearest(host, vb.mapViewToScene(QtCore.QPointF(4, 5)))
    assert result is not None
    assert (result.point_number, result.x_value, result.y_value) == (256, 0, 5)


def test_closest_segment_wins_over_closer_sample_from_another_trace(trace_host):
    host, line, vb, _widget = trace_host
    other = NativePlotDataItem([5], [5.05], symbol="o", pen=None)
    vb.addItem(other)
    host.lines = {"point": other, "trace": line}
    result = nearest(host, vb.mapViewToScene(QtCore.QPointF(5, 5)))
    assert result is not None and result.label == "trace"


def test_geometry_cache_tracks_publication_processing_and_style(trace_host, monkeypatch):
    host, line, vb, widget = trace_host
    calls = []
    original = snap._line_snap_data

    def prepare(line):
        calls.append(line)
        return original(line)

    monkeypatch.setattr(snap, "_line_snap_data", prepare)
    line.setData([1., 10.], [1., 10.])
    point = vb.mapViewToScene(QtCore.QPointF(5, 5))
    assert nearest(host, point) is not None
    geometry = line._qplot_snap_cache.geometry
    widget.resize(900, 600)
    vb.setRange(xRange=[-2, 12], yRange=[-2, 12], padding=0)
    line.setDownsampling(ds=2, auto=False, method="mean")
    nearest(host, vb.mapViewToScene(QtCore.QPointF(5, 5)))
    assert calls == [line]
    assert line._qplot_snap_cache.geometry is geometry
    line.setPen(None)
    line.setSymbol("o")
    assert nearest(host, vb.mapViewToScene(QtCore.QPointF(5, 5))) is None
    assert calls == [line]
    line.setPen("b")
    line.setLogMode(True, True)
    result = nearest(host, vb.mapViewToScene(QtCore.QPointF(.4, .4)))
    assert result is not None
    assert (result.x_value, result.y_value, result.point_number) == (1., 1., 1)
    assert len(calls) == 2
    line.setLogMode(False, False)
    line.setSubtractMeanMode(True)
    result = nearest(host, vb.mapViewToScene(QtCore.QPointF(4.6, -.9)))
    assert result is not None and result.y_value == -4.5
    assert len(calls) == 3
    x, y = np.array([1., 10.]), np.array([1., 10.])
    line.setData(x, y)
    nearest(host, point)
    y += 10
    line.setData(x, y)
    nearest(host, point)
    assert len(calls) == 5
    line.clear()
    assert nearest(host, point) is None
    assert line._qplot_snap_cache is None


def test_sparse_line_hover_reports_measured_endpoint_and_highlights_axes(loaded_plot):
    window = loaded_plot
    window.line.setData([0., 60.], [40., 40.])
    window.plot.vb.setRange(xRange=[-5, 65], yRange=[0, 80], padding=0)
    point = window.plot.vb.mapViewToScene(QtCore.QPointF(25, 40))
    window.mouseMoved(point)
    assert window._hover_highlight.active_item is window.line
    assert window._hover_highlight.axis_sides == ("bottom", "left")
    assert window.pos_labels["index"].text().strip() == "[0]"
    assert float(window.pos_labels["x"].text()) == 0
    assert float(window.pos_labels["y"].text()) == 40
    window.mouseMoved(point + QtCore.QPointF(0, 13))
    assert window._hover_highlight.active_item is None
    assert window._hover_highlight.axis_sides == ()


@pytest.mark.parametrize("offset", [0., 5e9, 1e12])
@pytest.mark.parametrize("vertical", [False, True])
def test_segment_capture_preserves_large_offset_coordinates(trace_host, offset, vertical):
    host, line, vb, _widget = trace_host
    x, y = np.array([0., 1000.]) + offset, np.array([3., 3.]) + offset
    if vertical:
        x, y = y, x
    line.setData(x, y)
    vb.setRange(xRange=[float(min(x) - 10), float(max(x) + 10)],
                yRange=[float(min(y) - 10), float(max(y) + 10)], padding=0)
    a, b = (vb.mapViewToScene(QtCore.QPointF(x[i], y[i])) for i in (0, 1))
    point = a * .6 + b * .4
    displacement = QtCore.QPointF(11, 0) if vertical else QtCore.QPointF(0, 11)
    result = nearest(host, point + displacement)
    assert result is not None
    assert (result.point_number, result.x_value, result.y_value) == (1, x[0], y[0])
    assert nearest(host, point + displacement * (13 / 11)) is None


def test_block_search_matches_exhaustive_segment_oracle(trace_host):
    host, line, vb, _widget = trace_host
    rng = np.random.default_rng(713)
    values = rng.uniform(0, 10, (2, 800))
    line.setData(*values, connect="finite")
    vertices = [vb.mapViewToScene(QtCore.QPointF(x, y)) for x, y in zip(*values, strict=True)]
    for x, y in rng.uniform(0, 10, (20, 2)):
        pointer = vb.mapViewToScene(QtCore.QPointF(x, y))
        candidates = []
        for index, (a, b) in enumerate(zip(vertices[:-1], vertices[1:], strict=True)):
            dx, dy = b.x() - a.x(), b.y() - a.y()
            along = ((pointer.x() - a.x()) * dx + (pointer.y() - a.y()) * dy) / (dx*dx + dy*dy)
            along = max(0., min(1., along))
            distance = (a.x() + along * dx - pointer.x())**2 + (a.y() + along * dy - pointer.y())**2
            candidates.append((distance, index + (along > .5)))
        distance, index = min(candidates)
        result = nearest(host, pointer)
        if distance > snap.SNAP_RADIUS_PIXELS**2:
            assert result is None
        else:
            assert result is not None
            assert result.point_number == index + 1
            assert (result.x_value, result.y_value) == tuple(values[:, index])
