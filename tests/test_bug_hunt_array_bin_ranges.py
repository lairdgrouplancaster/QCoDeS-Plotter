"""Bounded array heatmaps retain bins across finite coordinate ranges."""

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed"])
@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("scale", [1., 1e-309, 1e-320, 1e308])
def test_array_bin_means_and_centres_preserve_coordinate_scale(
        tmp_path, monkeypatch, route, axis, scale):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS",
                        1 if route == "streamed" else 1000)
    coordinates = np.array([-1., 0., 1.])
    fast = coordinates * (scale if axis == "x" else 1.)
    slow = coordinates * (scale if axis == "y" else 1.)
    records = [(coordinate, fast, np.array([1., 3., 5.]) + 100 * index)
               for index, coordinate in enumerate(slow)]
    with heatmap_dataset(tmp_path, arrays=True, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=1,
                             max_heatmap_grid_cells=4, max_heatmap_grid_side=2)
        run_worker(worker)
        np.testing.assert_array_equal(worker.dataGrid, [[1., 4.], [151., 154.]])
        for name in ("x", "y"):
            expected = np.array([-.5, .5]) * (scale if name == axis else 1.)
            np.testing.assert_array_equal(worker.axis_data[name], expected)
        # The real-QCoDeS fixture separately verifies protected source hashes.
