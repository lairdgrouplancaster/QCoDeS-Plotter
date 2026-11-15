"""Distinct QCoDeS coordinates must never become implicit duplicate cells."""


import numpy as np
import pytest

import qplot.tools.worker as worker_module
from tests.support.workers.worker_coordinate_precision import (
    coordinate_dataset as coordinate_dataset,
)
from tests.support.workers.worker_coordinate_precision import (
    coordinate_worker as coordinate_worker,
)
from tests.support.workers.worker_coordinate_precision import (
    run_coordinate_worker as run_coordinate_worker,
)


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("force_sql,split", [(False, False), (True, False), (True, True)])
def test_collapsed_coordinates_fail_before_publishing_averaged_cells(
    tmp_path, shaped, swapped, force_sql, split,
):
    coordinates = np.array([2**53, 2**53 + 1, 2**53 + 4], dtype=np.int64)
    with coordinate_dataset(tmp_path, coordinates=coordinates, shaped=shaped, split=split) as dataset:
        worker = coordinate_worker(dataset, swapped=swapped, force_sql_heatmap=force_sql)
        finished, errors = run_coordinate_worker(worker)
        assert finished == [False]
        assert len(errors) == 1
        assert "coordinate precision" in str(errors[0])
        assert "distinct values" in str(errors[0])
        assert getattr(worker, "dataGrid", None) is None


@pytest.mark.parametrize("force_sql", [False, True])
@pytest.mark.parametrize("duplicate", [False, True])
def test_large_representable_and_truly_duplicate_coordinates_remain_supported(tmp_path, force_sql, duplicate):
    coordinates = np.array([2**53, 2**53 if duplicate else 2**53 + 4, 2**53 + 8])
    with coordinate_dataset(tmp_path, coordinates=coordinates) as dataset:
        worker = coordinate_worker(dataset, force_sql_heatmap=force_sql)
        assert run_coordinate_worker(worker) == ([True], [])
        expected = [[2, 10], [22, 30]] if duplicate else [[1, 3, 10], [21, 23, 30]]
        np.testing.assert_array_equal(worker.dataGrid, expected)


def test_coordinate_precision_check_remains_cancellable(tmp_path, monkeypatch):
    with coordinate_dataset(tmp_path, coordinates=np.array([0, 1, 2])) as dataset:
        worker = coordinate_worker(dataset)
        checks = []

        def cancel_after_unique():
            checks.append(True)
            if len(checks) == 2:
                worker.cancel()
            original_check()

        original_check = worker._check_cancelled
        monkeypatch.setattr(worker, "_check_cancelled", cancel_after_unique)
        with pytest.raises(worker_module.PlotWorkCancelled):
            worker._float_heatmap_coordinates(np.array([2**53, 2**53 + 4]), "fast")


@pytest.mark.parametrize("unsigned", [False, True])
def test_collisions_between_records_with_different_dtypes(tmp_path, unsigned):
    if unsigned:
        coordinates = np.array([2**63 - 1, 2**63, 2**63 + 4096], dtype=np.uint64)
        dtypes = (np.int64, np.uint64)
    else:
        coordinates = np.array([2**53 + 1, 2**53, 2**53 + 4], dtype=np.int64)
        dtypes = (np.int64, np.float64)
    with coordinate_dataset(tmp_path, coordinates=coordinates, split=True, record_dtypes=dtypes) as dataset:
        worker = coordinate_worker(dataset, force_sql_heatmap=True)
        finished, errors = run_coordinate_worker(worker)
        assert finished == [False]
        assert len(errors) == 1 and "coordinate precision" in str(errors[0])
        assert getattr(worker, "dataGrid", None) is None


@pytest.mark.parametrize("discarded_inexact", [False, True])
def test_streaming_precision_guard_after_unique_set_saturates(tmp_path, monkeypatch, discarded_inexact):

    # Each source value is a separate chunk. A one-coordinate limit drops the
    # set before the last sample arrives; no unbounded history may be retained.
    coordinates = np.array([2**53 + 1, 0, 2**53] if discarded_inexact else [0, 2**53, 2**53 + 1])
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 1)
    monkeypatch.setattr(worker_module, "MAX_ARRAY_HEATMAP_AXIS_VALUES", 1)
    with coordinate_dataset(tmp_path, coordinates=coordinates) as dataset:
        worker = coordinate_worker(dataset, force_sql_heatmap=True, max_heatmap_grid_side=1)
        finished, errors = run_coordinate_worker(worker)
        assert finished == [False]
        assert len(errors) == 1 and "coordinate precision" in str(errors[0])
        assert getattr(worker, "dataGrid", None) is None


def test_unacquired_coordinates_do_not_trigger_precision_error(tmp_path):
    with coordinate_dataset(tmp_path, coordinates=np.array([0, 1, 2])) as dataset:
        worker = coordinate_worker(dataset)
        signal = np.array([[1, 2, 3], [0, 0, 0]])
        data = {"fast": np.array([[0, 1, 2], [2**53, 2**53 + 1, 2**53 + 4]]),
                "slow": np.array([[0, 0, 0], [0, 0, 0]])}
        acquired = np.array([[True, True, True], [False, False, False]])
        axes, _, grid = worker.for_shaped_2d(data, signal, acquired=acquired)
        np.testing.assert_array_equal(axes["x"], [0, 1, 2])
        np.testing.assert_array_equal(grid, [[1, 2, 3]])


def test_numpy_object_scalar_comparison_cannot_hide_coordinate_collision(tmp_path):
    with coordinate_dataset(tmp_path, coordinates=np.array([0, 1, 2])) as dataset:
        worker = coordinate_worker(dataset)
        values = np.array([np.int64(2**53 + 1), np.float64(2**53)], dtype=object)
        with pytest.raises(ValueError, match="coordinate precision"):
            worker._float_heatmap_coordinates(values, "fast")
