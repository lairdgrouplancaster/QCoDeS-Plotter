"""Spatial aggregation must not depend on representable coordinate units."""

from types import SimpleNamespace

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from qplot.datahandling.trusted_heatmap import NumericHeatmapPlan, numeric_heatmap
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


class _SqlExecutor:
    def __init__(self, connection):
        self.connection = connection

    def check_cancelled(self):
        pass

    def report_progress(self, *_args, **_kwargs):
        pass

    def query(self, sql, bindings=()):
        return SimpleNamespace(rows=tuple(self.connection.execute(sql, bindings).fetchall()))

    def query_batch(self, queries):
        return tuple(self.query(query.sql, query.bindings) for query in queries)


@pytest.mark.parametrize("route", ["trusted_sql", "snapshot_sql"])
@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("scale", [1., 1e-309, 1e-320, 1e308])
def test_spatial_means_preserve_cells_across_coordinate_scales(
        tmp_path, monkeypatch, route, axis, scale):
    # Three source cells become two bins. In particular, zero lies exactly
    # on their shared boundary and belongs to the second bin at every scale.
    coordinates = np.array([-1., 0., 1.])
    fast = coordinates * (scale if axis == "x" else 1.)
    slow = coordinates * (scale if axis == "y" else 1.)
    records = [(coordinate, fast, np.array([1., 3., 5.]) + 100 * index)
               for index, coordinate in enumerate(slow)]
    with heatmap_dataset(tmp_path, arrays=False, shaped=False, records=records) as dataset:
        if route == "trusted_sql":
            plan = NumericHeatmapPlan("fast", "slow", "signal", 1, 4, 2,
                                      (None, None), False)
            work = numeric_heatmap(_SqlExecutor(dataset.conn), dataset, plan, 9, True)
            with pytest.raises(StopIteration) as finished:
                while True:
                    next(work)
            heatmap = finished.value.value
            grid, axes = heatmap.grid, heatmap.axes
            assert heatmap.matching_count == 9
        else:
            monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
            worker = make_worker(dataset, force_sql_heatmap=True,
                                 max_full_heatmap_points=1,
                                 max_heatmap_grid_cells=4, max_heatmap_grid_side=2)
            run_worker(worker)
            grid, axes = worker.dataGrid, (worker.axis_data["x"], worker.axis_data["y"])
        np.testing.assert_array_equal(grid, [[1., 4.], [151., 154.]])
        for values in axes:
            assert np.all(np.isfinite(values))
            assert np.all(np.diff(values) > 0)
        # The fixture separately checks the source database hash and sidecars.
