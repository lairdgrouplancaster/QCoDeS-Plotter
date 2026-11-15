"""Mean repeated real QCoDeS cells without losing finite measurements."""


import numpy as np
import pytest

from qplot.datahandling.parameter_data import parameter_data_for_export
from qplot.tools.general import data2matrix
from tests.support.workers.heatmap_float_means import (
    repeated_dataset as repeated_dataset,
)
from tests.support.workers.worker_array_heatmaps import make_worker, run_worker


@pytest.mark.parametrize(
    "values",
    [
        np.full(4, 60000, dtype=np.float16),
        np.full(4, 3e38, dtype=np.float32),
        np.full(4, 1e308),
        np.full(4, np.finfo(float).max),
        np.array([1e308, 1e308, -1e308, -1e308]),
    ],
)
@pytest.mark.parametrize("shaped", [False, True])
def test_real_repeated_heatmap_mean(tmp_path, values, shaped):
    with repeated_dataset(tmp_path, values, shaped=shaped) as dataset:
        worker = make_worker(dataset)
        run_worker(worker)
        from fractions import Fraction

        expected = float(sum(Fraction(float(value)) for value in values) / len(values))
        np.testing.assert_array_equal(worker.axis_data["x"], [0.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [0.0, 1.0])
        np.testing.assert_allclose(
            worker.dataGrid, np.full((2, 1), expected), rtol=2e-15
        )
        exported = parameter_data_for_export(dataset, "signal")["signal"]
        assert exported.dtype == values.dtype
        np.testing.assert_array_equal(exported, np.tile(values, (2, 1)))


def test_extreme_cell_recovery_preserves_small_cells_and_holes():
    rows = np.array([0.0, 0.0, 0.0, 0.0, 1.0])
    columns = np.array([0.0, 0.0, 0.0, 0.0, 1.0])
    values = np.array([1e308, 1e308, -1e308, -1e308, 1e-308])
    grid = data2matrix(rows, columns, values).to_numpy()
    np.testing.assert_array_equal(grid, [[0.0, np.nan], [np.nan, 1e-308]])
    np.testing.assert_array_equal(values, [1e308, 1e308, -1e308, -1e308, 1e-308])


def test_extreme_mean_recovery_is_cancellable():
    calls = 0

    def check_cancelled():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        data2matrix(
            np.zeros(4), np.zeros(4), np.full(4, 1e308), check_cancelled=check_cancelled
        )
