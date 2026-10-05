"""Bound Cartesian heatmap size without ever constructing an oversized grid."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import OperationExecutionError, PlotWorkCancelled, loader


@pytest.fixture
def worker():
    worker = loader.__new__(loader)
    worker.axes_dict = {"x": "x", "y": "y"}
    worker.param = SimpleNamespace(name="signal", depends_on_=("y", "x"))
    worker.param_dict = {name: SimpleNamespace(name=name) for name in ("x", "y")}
    worker.operations = []
    worker.max_full_heatmap_points = worker_module.MAX_FULL_HEATMAP_POINTS
    worker.max_heatmap_grid_cells = 16
    worker.max_heatmap_grid_side = 3
    return worker


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("operations", [False, True])
def test_diagonal_scan_never_allocates_cartesian_grid(worker, shaped, operations):
    # Only 2,000 source samples, but the pivot would contain 4,000,000 cells.
    coordinates = np.arange(2000, dtype=float)
    if shaped:
        coordinates = coordinates.reshape(40, 50)
    data = {"x": coordinates, "y": coordinates}
    worker.operations = [lambda data: data] if operations else []

    def load():
        if shaped:
            return worker.for_shaped_2d(data, coordinates)
        return worker.for_unshaped_2d(data, np.isfinite(coordinates), coordinates)

    with (
        patch.object(worker_module, "data2matrix") as pivot,
        patch.object(worker, "_unique_heatmap_grid") as exact_grid,
        patch.object(worker, "_shaped_data_grid") as shaped_grid,
    ):
        if operations:
            with pytest.raises(
                OperationExecutionError,
                match=r"4,000,000.*exceeds the full-resolution operation limit of 2,000,000",
            ):
                load()
        else:
            axes, params, grid = load()
            assert grid.shape == (3, 3)
            assert grid.size <= worker.max_heatmap_grid_cells
            assert params == worker.param_dict
            assert all(axis.size == 3 for axis in axes.values())
            info = worker.heatmap_downsample_info
            assert info["source_grid_cell_count"] == 4_000_000
            assert info["source_row_count"] == 2000
            assert info["grid_binned"]
        pivot.assert_not_called()
        exact_grid.assert_not_called()
        shaped_grid.assert_not_called()


@pytest.mark.parametrize("shaped", [False, True])
def test_cartesian_count_ignores_duplicates_and_missing_samples(worker, shaped):
    worker.max_full_heatmap_points = 4
    worker.operations = [lambda data: data]
    x = np.array([0, 1, 1, 0, 1, 2, 3, np.nan, 100, np.nan], dtype=float)
    y = np.array([0, 0, 0, 1, 1, 2, np.nan, 4, 100, np.nan], dtype=float)
    z = np.array([1, 2, 6, 3, 4, np.nan, 5, 6, 7, np.nan], dtype=float)
    # An unselected sample must not contribute either coordinate to the count.
    valid = np.ones(z.size, dtype=bool)
    valid[8] = False
    if shaped:
        z[8] = np.nan
        x, y, z = (values.reshape(2, 5) for values in (x, y, z))

    with patch.object(worker_module, "data2matrix", wraps=worker_module.data2matrix) as pivot:
        if shaped:
            axes, _params, grid = worker.for_shaped_2d({"x": x, "y": y}, z)
        else:
            axes, _params, grid = worker.for_unshaped_2d({"x": x, "y": y}, valid, z)
        pivot.assert_called_once()

    np.testing.assert_array_equal(axes["x"], [0, 1])
    np.testing.assert_array_equal(axes["y"], [0, 1])
    np.testing.assert_array_equal(grid, [[1, 4], [3, 4]])


@pytest.mark.parametrize("size", [0, 5])
def test_empty_or_missing_heatmap_has_no_cartesian_cells(worker, size):
    worker.max_full_heatmap_points = 1
    worker.operations = [lambda data: data]
    x = np.arange(size, dtype=float)
    z = np.full(size, np.nan)
    axes, _params, grid = worker.for_unshaped_2d(
        {"x": x, "y": x}, np.ones(size, dtype=bool), z,
    )
    assert grid.shape == (0, 0)
    assert axes["x"].size == axes["y"].size == 0


def test_bounded_grid_averages_duplicate_samples_and_skips_missing_values(worker):
    worker.max_full_heatmap_points = 8
    worker.max_heatmap_grid_cells = 4
    x = np.array([0, 0, 1, 2, 3, np.nan, 99], dtype=float)
    y = np.array([0, 0, 1, 2, 3, 99, np.nan], dtype=float)
    z = np.array([2, 4, 6, 8, 10, 100, 100], dtype=float)
    with patch.object(worker_module, "data2matrix") as pivot:
        _axes, _params, grid = worker.for_unshaped_2d(
            {"x": x, "y": y}, np.isfinite(z), z,
        )
        pivot.assert_not_called()
    np.testing.assert_array_equal(grid, [[4, np.nan], [np.nan, 9]])
    assert worker.heatmap_downsample_info["source_row_count"] == 5


@pytest.mark.parametrize("operations", [False, True])
def test_cached_rectilinear_shape_is_bounded_before_dense_copy(worker, operations):
    worker.max_full_heatmap_points = 6
    worker.operations = [lambda data: data] if operations else []
    x, y = np.meshgrid(np.arange(4, dtype=float), np.arange(3, dtype=float))
    with patch.object(worker, "_shaped_data_grid") as dense_copy:
        if operations:
            with pytest.raises(OperationExecutionError, match="12.*limit of 6"):
                worker.for_shaped_2d({"x": x, "y": y}, x + y)
        else:
            _axes, _params, grid = worker.for_shaped_2d({"x": x, "y": y}, x + y)
            assert grid.size <= 6
        dense_copy.assert_not_called()


@pytest.mark.parametrize("max_cells,max_side,requested_cells", [
    (6, 800, None), (100, 2, None), (6, 800, 100), (100, 2, 100),
])
def test_bounded_gridding_obeys_configured_cells_and_sides(
    worker, max_cells, max_side, requested_cells,
):
    worker.max_heatmap_grid_cells = max_cells
    worker.max_heatmap_grid_side = max_side
    x, y = np.meshgrid(np.arange(4, dtype=float), np.arange(3, dtype=float))
    with patch.object(worker, "_unique_heatmap_grid") as exact_grid:
        _x_axis, _y_axis, grid = worker._heatmap_grid_from_arrays(
            x.ravel(), y.ravel(), (x + y).ravel(), max_cells=requested_cells,
        )
        exact_grid.assert_not_called()
    assert grid.size <= max_cells
    assert max(grid.shape) <= max_side


def test_cancellation_during_cardinality_check_prevents_gridding(worker):
    coordinates = np.arange(2000, dtype=float)
    unique = np.unique

    def cancel_after_unique(values):
        result = unique(values)
        worker.cancel()
        return result

    with (
        patch.object(worker_module.np, "unique", side_effect=cancel_after_unique),
        patch.object(worker_module, "data2matrix") as pivot,
        patch.object(worker, "_heatmap_grid_from_arrays") as bounded_grid,
    ):
        with pytest.raises(PlotWorkCancelled):
            worker.for_unshaped_2d(
                {"x": coordinates, "y": coordinates},
                np.isfinite(coordinates), coordinates,
            )
        pivot.assert_not_called()
        bounded_grid.assert_not_called()


def test_diagonal_qcodes_heatmap_is_bounded_on_initial_and_cached_load(tmp_path):
    path = tmp_path / "diagonal.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("diagonal", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y")
    measurement.register_custom_parameter("signal", setpoints=("y", "x"))
    with measurement.run() as datasaver:
        for value in range(2000):
            datasaver.add_result(("x", value), ("y", value), ("signal", value))
        run_id = datasaver.dataset.run_id

    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        params = {param.name: param for param in dataset.get_parameters()}
        for read_data, operations in ((True, []), (False, []), (False, [lambda data: data])):
            worker = loader(
                dataset.cache, params["signal"], params, {"x": "x", "y": "y"},
                read_data=read_data, operations=operations,
                max_heatmap_grid_cells=16, max_heatmap_grid_side=3,
            )
            finished, errors = [], []
            worker.emitter.finished.connect(finished.append)
            worker.emitter.errorOccurred.connect(errors.append)
            with patch.object(worker_module, "data2matrix") as pivot:
                worker.run()
                pivot.assert_not_called()
            if operations:
                assert finished == [False]
                assert len(errors) == 1
                assert isinstance(errors[0], OperationExecutionError)
                assert "4,000,000" in str(errors[0])
            else:
                assert finished == [True]
                assert errors == []
                assert worker.dataGrid.shape == (3, 3)
            if read_data:
                assert update_cache_parameter_data(
                    dataset.cache, "signal", worker.updated_read_status,
                    worker.updated_write_status, worker.cache_data,
                )
    finally:
        dataset.conn.close()
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()
