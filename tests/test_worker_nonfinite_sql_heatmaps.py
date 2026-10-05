"""Scalar SQL heatmaps aggregate finite observations, preserving valid bins."""

import hashlib
from contextlib import contextmanager

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.worker import loader


@contextmanager
def numeric_heatmap(tmp_path, records, shape=None):
    path = tmp_path / "nonfinite_heatmap.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("nonfinite_heatmap", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow", paramtype="numeric")
    measurement.register_custom_parameter("fast", paramtype="numeric")
    measurement.register_custom_parameter(
        "signal", setpoints=("slow", "fast"), paramtype="numeric",
    )
    if shape is not None:
        measurement.set_shapes({"signal": shape})
    with measurement.run() as datasaver:
        for slow, fast, signal in records:
            # QCoDeS expands numeric batches into individual scalar SQL rows.
            datasaver.add_result(("slow", slow), ("fast", fast), ("signal", signal))
        run_id = datasaver.dataset.run_id
    datasaver.dataset.conn.close()
    experiment.conn.close()
    before = hashlib.sha256(path.read_bytes()).digest()
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert hashlib.sha256(path.read_bytes()).digest() == before
        assert not path.with_name(path.name + "-journal").exists()
        assert not path.with_name(path.name + "-wal").exists()


def make_worker(dataset, **kwargs):
    params = {param.name: param for param in dataset.get_parameters()}
    return loader(
        dataset.cache, params["signal"], params, {"x": "fast", "y": "slow"},
        **kwargs,
    )


def run_worker(worker):
    finished, errors = [], []
    worker.emitter.finished.connect(finished.append)
    worker.emitter.errorOccurred.connect(errors.append)
    worker.run()
    assert errors == []
    assert finished == [True]
    assert worker._sql_connection is None
    assert worker.loaded_from_sql_heatmap
    assert not hasattr(worker, "cache_data")


def forbid_cache_read(*args, **kwargs):
    pytest.fail("Bounded heatmap loading must not read the full QCoDeS cache")


def test_default_bounded_load_with_final_infinite_coordinate(tmp_path, monkeypatch):
    """Reproduce the 2,002,000-point scan without overriding any load limits."""
    shape = (2000, 1001)

    def records():
        for slow in range(shape[0]):
            fast = np.arange(shape[1], dtype=float)
            if slow == shape[0] - 1:
                fast[-1] = np.inf
            yield slow, fast, np.ones(shape[1])

    with numeric_heatmap(tmp_path, records(), shape) as dataset:
        monkeypatch.setattr(worker_module, "load_param_data_from_db", forbid_cache_read)
        workers = [
            make_worker(dataset),
            make_worker(dataset, heatmap_axis_ranges={
                "x": (0, 1000), "y": (0, 1999),
            }),
        ]
        for worker in workers:
            run_worker(worker)
        for worker in workers:
            assert worker.dataGrid.shape == (500, 500)
            assert worker.aggregated_heatmap_source
            assert worker.total_point_count_estimate == 2_002_000
            info = worker.heatmap_downsample_info
            assert info["source_row_count"] == 2_002_000
            assert info["estimated_range_rows"] == 2_001_999
            assert info["aggregated_source_row_count"] == 2_001_999
            assert info["unique_x_count"] == 1001
            assert info["unique_y_count"] == 2000
            assert worker.dataGrid.size <= worker.max_heatmap_grid_cells
            assert max(worker.dataGrid.shape) <= worker.max_heatmap_grid_side
            assert worker.loaded_point_count == worker.dataGrid.size > 0
            np.testing.assert_array_equal(worker.dataGrid, np.ones(worker.dataGrid.shape))
        for axis in ("x", "y"):
            np.testing.assert_array_equal(workers[0].axis_data[axis], workers[1].axis_data[axis])
        np.testing.assert_array_equal(workers[0].dataGrid, workers[1].dataGrid)
        assert workers[0].heatmap_source_axis_ranges == {"x": (0, 1000), "y": (0, 1999)}


def test_both_infinity_signs_do_not_erase_valid_signal_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    records = [(0, 0, 2), (0, 0, np.inf), (0, 0, -np.inf), (0, 0, 4),
               (1, 1, 6), (0, 1, np.inf), (0, 1, -np.inf)]
    with numeric_heatmap(tmp_path, records) as dataset:
        for ranges in (None, {"x": (0, 1), "y": (0, 1)}):
            worker = make_worker(dataset, max_full_heatmap_points=2, heatmap_axis_ranges=ranges)
            run_worker(worker)
            np.testing.assert_array_equal(worker.dataGrid, [[3, np.nan], [np.nan, 6]])
            assert worker._heatmap_aggregated_source_rows == 3
            assert worker._heatmap_source_info["estimated_range_rows"] == 3
            assert worker.loaded_point_count == 2


@pytest.mark.parametrize("column", ["slow", "fast", "signal"])
def test_sql_finite_predicate_includes_float64_extrema(tmp_path, monkeypatch, column):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
    extrema = float(np.finfo(float).max)
    records = [[0., 0., 2.], [1., 1., 4.]]
    index = {"slow": 0, "fast": 1, "signal": 2}[column]
    records[0][index] = -extrema
    records[1][index] = extrema
    with numeric_heatmap(tmp_path, records) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=1)
        # Exercise the SQL reader independently of display edge arithmetic,
        # which cannot represent edges beyond the float64 coordinate extrema.
        worker._load_large_heatmap_from_sql()
        assert worker._sql_connection is None
        expected = [[-extrema, np.nan], [np.nan, extrema]] if column == "signal" else [[2, np.nan], [np.nan, 4]]
        np.testing.assert_array_equal(worker.dataGrid, expected)
        assert worker._heatmap_aggregated_source_rows == 2
        for axis, name in worker.axes_dict.items():
            np.testing.assert_array_equal(worker.axis_data[axis], [-extrema, extrema] if name == column else [0, 1])


@pytest.mark.parametrize("column", ["slow", "fast", "signal"])
@pytest.mark.parametrize("nonfinite", [np.inf, -np.inf, np.nan], ids=["inf", "negative-inf", "nan"])
@pytest.mark.parametrize("mode", ["raw", "aggregated-exact", "aggregated-binned"])
def test_nonfinite_samples_are_excluded_before_sql_means_and_bounds(
    tmp_path, monkeypatch, column, nonfinite, mode,
):
    records = []
    invalid_index = {"slow": 0, "fast": 1, "signal": 2}[column]
    for slow in range(4):
        for fast in range(4):
            row = [float(slow), float(fast), float(10 * slow + fast)]
            if (slow == 0 and fast == 1) or (slow >= 2 and fast >= 2):
                row[invalid_index] = nonfinite
            records.append(row)
    # Duplicates contribute individually to bin means. An invalid observation
    # with an outlying finite coordinate must not affect either axis summary.
    records.append([0., 0., 8.])
    outlier = [-999., -999., 999.]
    outlier[invalid_index] = nonfinite
    records.append(outlier)
    binned = mode == "aggregated-binned"
    if mode != "raw":
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 4)
    monkeypatch.setattr(worker_module, "load_param_data_from_db", forbid_cache_read)

    with numeric_heatmap(tmp_path, records) as dataset:
        workers = [
            make_worker(
                dataset, max_full_heatmap_points=4,
                max_heatmap_grid_cells=4 if binned else 16,
                heatmap_axis_ranges=ranges,
            )
            for ranges in (None, {"x": (0, 3), "y": (0, 3)})
        ]
        expected = np.array([[0, 1, 2, 3], [10, 11, 12, 13],
                             [20, 21, np.nan, np.nan], [30, 31, np.nan, np.nan]], dtype=float)
        expected[0, 0] = 4  # Mean of the two valid observations at (0, 0).
        expected[0, 1] = np.nan
        if binned:
            expected = np.array([[7.25, 7.5], [25.5, np.nan]])
        for worker in workers:
            run_worker(worker)
            assert worker.aggregated_heatmap_source == (mode != "raw")
            assert worker.total_point_count_estimate == 18
            assert worker._heatmap_source_info["row_count"] == 18
            assert worker._heatmap_source_info["estimated_range_rows"] == 12
            np.testing.assert_array_equal(worker.dataGrid, expected)
            for axis in ("x", "y"):
                np.testing.assert_array_equal(worker.axis_data[axis], [0.5, 2.5] if binned else np.arange(4))
            assert worker.loaded_point_count == (3 if binned else 11 if mode != "raw" else 12)
            if mode != "raw":
                assert worker._heatmap_aggregated_source_rows == 12
                assert worker._spatial_heatmap_source_unique_counts == (4, 4)
                if binned:
                    assert worker.heatmap_downsample_info["empty_bins_filled"] is False
        np.testing.assert_array_equal(workers[0].dataGrid, workers[1].dataGrid)
