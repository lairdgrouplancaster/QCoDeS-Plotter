"""Planned heatmap shapes must not erase differing acquired coordinates."""

from types import MethodType, SimpleNamespace

import numpy as np
import pytest

from qplot.tools.worker import loader
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


@pytest.mark.parametrize("axis", ["x", "y"])
@pytest.mark.parametrize("direction", [-1., 1.])
@pytest.mark.parametrize("scale", [1., 1e-309, 1e-320, 1e308])
def test_shaped_heatmap_retains_nonrectilinear_record_coordinates(
        tmp_path, axis, direction, scale):
    multiplier = direction * scale
    records = [
        (0., np.array([-1., 0., 1.]) * multiplier, np.array([1., 2., 3.])),
        (1., np.array([-1., 0., .9]) * multiplier, np.array([4., 5., 6.])),
    ]
    with heatmap_dataset(tmp_path, arrays=True, shaped=True, records=records) as dataset:
        worker = make_worker(dataset)
        if axis == "y":
            worker.axes_dict = {"x": "slow", "y": "fast", "z": "signal"}
        run_worker(worker)
        coordinates = np.array([-1., 0., .9, 1.]) * multiplier
        order = np.argsort(coordinates)
        expected = np.array([[1., 2., np.nan, 3.], [4., 5., 6., np.nan]])[:, order]
        np.testing.assert_array_equal(worker.axis_data[axis], coordinates[order])
        np.testing.assert_array_equal(worker.axis_data["y" if axis == "x" else "x"], [0., 1.])
        np.testing.assert_array_equal(worker.dataGrid, expected if axis == "x" else expected.T)
        assert np.count_nonzero(np.isfinite(worker.dataGrid)) == 6
        # The QCoDeS fixture also checks unchanged protected source bytes.


@pytest.mark.parametrize("base, delta, accepted", [
    (0., 5e-13, True),
    (0., 2e-12, False),
    (1e12, .125, False),
])
def test_constant_representatives_keep_absolute_coordinate_tolerance(base, delta, accepted):
    host = SimpleNamespace(_check_cancelled=lambda: None)
    host._shaped_axis_values = MethodType(loader._shaped_axis_values, host)
    coordinates = np.array([[base, base], [base, base + delta]])
    assert loader._shaped_axis_is_rectilinear(host, coordinates, 1) is accepted
