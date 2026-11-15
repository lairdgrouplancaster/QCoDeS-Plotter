"""Cell budgets preserve narrow heatmaps without an arbitrary axis cap."""

from types import SimpleNamespace

import numpy as np
import pytest

from qplot.tools.heatmap_geometry import bounded_grid_shape
from tests._plot_worker import make_plot_worker


@pytest.mark.parametrize("shape,expected", [
    ((1000, 1000), (500, 500)),
    ((2000, 100), (2000, 100)),
    ((2500, 100), (2500, 100)),
    ((25000, 10), (25000, 10)),
    ((250000, 1), (250000, 1)),
    ((2000, 1000), (707, 353)),
    ((1000000, 1000), (15811, 15)),
    ((1000000, 1), (250000, 1)),
])
def test_cell_budget_preserves_shape_and_uses_singleton_budget(shape, expected):
    assert bounded_grid_shape(*shape, max_cells=250000) == expected
    assert bounded_grid_shape(*shape[::-1], max_cells=250000) == expected[::-1]
    # The application's derived axis bound must not alter the allocation.
    assert bounded_grid_shape(*shape, max_cells=250000, max_side=250000) == expected


@pytest.mark.parametrize("budget", [1, 2, 3, 10, 250000])
def test_grid_never_exceeds_budget_or_source_dimensions(budget):
    for x in (1, 2, 7, 800, 2000, 10**12):
        for y in (1, 3, 11, 1000, 10**12):
            bx, by = bounded_grid_shape(x, y, max_cells=budget)
            assert 1 <= bx <= x and 1 <= by <= y
            assert bx * by <= budget
            assert (by, bx) == bounded_grid_shape(y, x, max_cells=budget)


def narrow_worker(**kwargs):
    return make_plot_worker(
        axes={"x": "x", "y": "y"},
        param=SimpleNamespace(name="signal", depends_on_=("y", "x")),
        param_dict={name: SimpleNamespace(name=name) for name in ("x", "y")},
        **kwargs,
    )


def test_oversized_legacy_budget_cannot_allocate_an_unbounded_grid():
    from qplot.tools.operation_registry import OperationExecutionError

    worker = narrow_worker(max_heatmap_grid_cells=2_000_000_000)
    assert worker._bounded_grid_shape(2000, 100) == (2000, 100)
    with pytest.raises(OperationExecutionError, match="aggregation memory budget"):
        worker._bounded_grid_shape(100000, 100000)


@pytest.mark.parametrize("streaming", [False, True])
def test_narrow_heatmap_keeps_coordinates_and_values(streaming, monkeypatch):
    from qplot.tools import worker as worker_module

    worker = narrow_worker()
    x, y = np.meshgrid(np.arange(2000, dtype=float), np.arange(2, dtype=float))
    values = x + 10 * y
    arrays = (x.ravel(), y.ravel(), values.ravel())
    if streaming:
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
        monkeypatch.setattr(worker, "_array_heatmap_chunks",
                            lambda conn: (chunk for chunk in [(values.size, arrays)]))
        arrays = worker._read_array_heatmap_arrays(None)
    xa, ya, grid = worker._heatmap_grid_from_arrays(*arrays)
    np.testing.assert_array_equal(xa, x[0])
    np.testing.assert_array_equal(ya, y[:, 0])
    np.testing.assert_array_equal(grid, values)


def test_streaming_coordinate_tracking_has_its_own_bound(monkeypatch):
    from qplot.tools import worker as worker_module

    worker = narrow_worker(max_heatmap_grid_cells=1000000)
    monkeypatch.setattr(worker_module, "MAX_ARRAY_HEATMAP_AXIS_VALUES", 4)
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
    arrays = (np.arange(2000, dtype=float), np.zeros(2000), np.full(2000, 7.))
    monkeypatch.setattr(worker, "_array_heatmap_chunks",
                        lambda conn: (chunk for chunk in [(2000, arrays)]))
    worker._read_array_heatmap_arrays(None)
    assert worker._array_heatmap_cardinality_exact == (False, True)
    assert worker._spatial_heatmap_axes[0].size == 5


@pytest.mark.parametrize("transpose", [False, True])
def test_long_image_can_render(qapplication, transpose):
    import pyqtgraph as pg
    from PyQt6 import QtCore, QtGui

    widget = pg.GraphicsLayoutWidget()
    widget.resize(640, 320)
    view = widget.addViewBox()
    values = np.linspace(0., 1., 250000).reshape(1, -1)
    image = pg.ImageItem(axisOrder="row-major")
    view.addItem(image)
    image.setImage(values.T if transpose else values, levels=(0, 1))
    image.setRect(QtCore.QRectF(0, 0, 1, 1))
    view.setRange(QtCore.QRectF(0, 0, 1, 1), padding=0)
    try:
        widget.show()
        qapplication.processEvents()
        target = QtGui.QImage(640, 320, QtGui.QImage.Format.Format_ARGB32)
        target.fill(QtGui.QColor("magenta"))
        painter = QtGui.QPainter(target)
        try:
            widget.render(painter)
        finally:
            painter.end()
        assert image.autoDownsample
        assert image.qimage is not None and not image.qimage.isNull()
        assert max(image.qimage.width(), image.qimage.height()) < 32768
        # Both ends must be painted, not clipped at the old raster axis limit.
        samples = ((320, 40), (320, 280)) if transpose else ((40, 160), (600, 160))
        levels = [target.pixelColor(*point).red() for point in samples]
        assert abs(levels[0] - levels[1]) > 150
    finally:
        widget.close()
