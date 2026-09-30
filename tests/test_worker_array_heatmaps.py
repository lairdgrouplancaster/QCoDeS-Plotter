"""Bounded loading of real QCoDeS array-valued heatmaps."""

import hashlib
from contextlib import closing, contextmanager

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
def heatmap_dataset(tmp_path, *, arrays=True, shaped=True, records=None, scalar_fast=False):
    path = tmp_path / "array_heatmap.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("array_heatmap", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter(
        "fast", paramtype="array" if arrays and not scalar_fast else "numeric",
    )
    measurement.register_custom_parameter(
        "signal", setpoints=("slow", "fast"),
        paramtype="array" if arrays else "numeric",
    )
    if shaped:
        measurement.set_shapes({"signal": (2, 3)})
    if records is None:
        # Reverse the second sweep so independently sorting an array is wrong.
        records = [(0, [0, 1, 2], [0, 1, 2]), (1, [2, 1, 0], [12, 11, 10])]
    with measurement.run() as datasaver:
        for slow, fast, signal in records:
            if arrays:
                datasaver.add_result(
                    ("slow", slow), ("fast", np.asarray(fast, dtype=float)),
                    ("signal", np.asarray(signal)),
                )
            else:
                for x, z in zip(fast, signal, strict=True):
                    datasaver.add_result(("slow", slow), ("fast", x), ("signal", z))
        run_id = datasaver.dataset.run_id
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


@pytest.mark.parametrize("arrays", [False, True], ids=["scalar", "array"])
@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "shaped"])
@pytest.mark.parametrize("limit", [100, 4])
@pytest.mark.parametrize("swapped", [False, True])
def test_heatmap_above_and_below_full_resolution_threshold(tmp_path, arrays, shaped, limit, swapped):
    with heatmap_dataset(tmp_path, arrays=arrays, shaped=shaped) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=limit)
        if swapped:
            worker.axes_dict = {"x": "slow", "y": "fast"}
        run_worker(worker)
        assert worker.loaded_from_sql_heatmap == (limit == 4)
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1] if swapped else [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1, 2] if swapped else [0, 1])
        expected = np.array([[0, 1, 2], [10, 11, 12]])
        np.testing.assert_array_equal(worker.dataGrid, expected.T if swapped else expected)
        if limit == 4:
            assert not hasattr(worker, "cache_data")
            assert worker.total_point_count_estimate == 6


def test_array_heatmap_aggregates_every_paired_sample_in_bounded_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    records = [
        (0, [0, 1, 2], [2., 4., 6.]),
        (0, [0, 1, 2, np.nan, np.inf], [4., np.nan, 10., 99., 99.]),
        (1, [2, 1, 0], [12., 11., 10.]),
    ]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=4)
        sizes = []
        original = worker._arrays_from_values

        def record_chunks(x, y, z):
            sizes.append(np.asarray(z).size)
            return original(x, y, z)

        monkeypatch.setattr(worker, "_arrays_from_values", record_chunks)
        monkeypatch.setattr(worker_module, "load_param_data_from_db",
                            lambda *args, **kwargs: pytest.fail("Unbounded cache read"))
        run_worker(worker)
        assert max(sizes) <= 2
        assert len(sizes) == 14  # Seven chunks on each of two passes.
        assert worker.aggregated_heatmap_source
        assert worker.loaded_point_count == 6
        assert worker.total_point_count_estimate == 11
        assert worker.heatmap_downsample_info["aggregated_source_row_count"] == 8
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(worker.dataGrid, [[3, 4, 8], [10, 11, 12]])


@pytest.mark.parametrize("arrays", [False, True], ids=["scalar", "array"])
@pytest.mark.parametrize("visible", [False, True])
def test_bounded_heatmap_spatial_means_and_visible_range(tmp_path, monkeypatch, arrays, visible):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 4)
    records = [(y, np.arange(6), np.arange(6) + y * 10) for y in range(2)]
    with heatmap_dataset(tmp_path, arrays=arrays, shaped=False, records=records) as dataset:
        ranges = {"x": (1, 4), "y": (-0.1, 1.1)} if visible else None
        worker = make_worker(
            dataset, max_full_heatmap_points=4,
            max_heatmap_grid_cells=4, max_heatmap_grid_side=2,
            heatmap_axis_ranges=ranges,
        )
        run_worker(worker)
        assert worker.dataGrid.shape == (2, 2)
        np.testing.assert_allclose(worker.dataGrid,
                                   [[1.5, 3.5], [11.5, 13.5]] if visible
                                   else [[1, 4], [11, 14]])
        assert worker.heatmap_downsample_info["source_aggregated"]
        assert worker.heatmap_downsample_info["aggregated_source_row_count"] == (8 if visible else 12)
        if arrays:
            # Saturated coordinate sets must not be reported as exact counts.
            assert worker.heatmap_downsample_info["unique_x_count"] is None
            assert worker.heatmap_downsample_info["exact_cell_count"] is None


@pytest.mark.parametrize("fortran_column", ["fast", "signal"])
def test_array_heatmap_chunks_align_different_numpy_storage_orders(tmp_path, monkeypatch, fortran_column):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    fast = np.arange(6, dtype=float).reshape(2, 3)
    records = []
    for slow in range(2):
        signal = fast + slow * 10
        records.append((slow,
                        np.asfortranarray(fast) if fortran_column == "fast" else fast,
                        np.asfortranarray(signal) if fortran_column == "signal" else signal))
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=4)
        run_worker(worker)
        np.testing.assert_array_equal(worker.axis_data["x"], np.arange(6))
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(worker.dataGrid, [np.arange(6), np.arange(6) + 10])


@pytest.mark.parametrize("phase", ["count", "decode", "aggregate"])
def test_array_heatmap_cancellation_closes_blob_readers_without_publication(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    with heatmap_dataset(tmp_path, shaped=False) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=4)
        blobs = []
        init = worker_module._HeatmapArrayReader.__init__
        read = worker_module._HeatmapArrayReader.read
        chunks = worker._array_heatmap_chunks
        passes = []

        def record_blob(reader, blob):
            init(reader, blob)
            blobs.append(blob)
            if phase == "count":
                worker.cancel()

        def cancel_decode(reader, *args):
            result = read(reader, *args)
            if phase == "decode":
                worker.cancel()
            return result

        def cancel_aggregation(conn):
            passes.append(1)
            with closing(chunks(conn)) as source:
                for chunk in source:
                    if phase == "aggregate" and len(passes) == 2:
                        worker.cancel()
                    yield chunk

        monkeypatch.setattr(worker_module._HeatmapArrayReader, "__init__", record_blob)
        monkeypatch.setattr(worker_module._HeatmapArrayReader, "read", cancel_decode)
        monkeypatch.setattr(worker, "_array_heatmap_chunks", cancel_aggregation)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False]
        assert errors == []
        assert worker._sql_connection is None
        assert not hasattr(worker, "dataGrid")
        assert not hasattr(worker, "cache_data")
        assert blobs
        for blob in blobs:
            with pytest.raises(Exception, match="closed"):
                blob.read(1)


@pytest.mark.parametrize("limit", [100, 4])
def test_complex_array_heatmap_is_rejected_without_losing_imaginary_values(tmp_path, limit):
    records = [(y, [0, 1, 2], np.arange(3) + 10 * y + 1j) for y in range(2)]
    with heatmap_dataset(tmp_path, records=records) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=limit)
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1
        assert "Complex-valued heatmaps" in str(errors[0])
        assert not hasattr(worker, "dataGrid")


def test_large_array_blob_reads_are_bounded_and_physically_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1024)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 257)
    values = np.arange(12_003, dtype=float)
    records = [(y, values, values + y * 10) for y in range(2)]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        connect = worker_module.sqlite_read_only_connection
        read_sizes = []

        class BoundedBlob:
            def __init__(self, blob):
                self.blob = blob

            def __getattr__(self, name):
                return getattr(self.blob, name)

            def __len__(self):
                return len(self.blob)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.blob.close()

            def read(self, size=-1):
                assert 0 <= size <= 257 * 8
                read_sizes.append(size)
                return self.blob.read(size)

        class Connection:
            def __init__(self, *args, **kwargs):
                self.conn = connect(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def blobopen(self, *args, **kwargs):
                assert kwargs["readonly"] is True
                return BoundedBlob(self.conn.blobopen(*args, **kwargs))

        monkeypatch.setattr(worker_module, "sqlite_read_only_connection", Connection)
        monkeypatch.setattr(worker_module, "load_param_data_from_db",
                            lambda *args, **kwargs: pytest.fail("Unbounded cache read"))
        worker = make_worker(dataset, max_full_heatmap_points=4,
                             max_heatmap_grid_cells=4, max_heatmap_grid_side=2)
        run_worker(worker)
        assert read_sizes
        assert worker.loaded_point_count == 4
        assert worker.total_point_count_estimate == 24_006
        np.testing.assert_array_equal(worker.dataGrid, [[3000, 9001.5], [3010, 9011.5]])


def test_array_signal_broadcasts_both_scalar_setpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    records = [(0, 0, [0., 1., 2.]), (1, 0, [10., 11., 12.])]
    with heatmap_dataset(tmp_path, shaped=False, records=records, scalar_fast=True) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=4)
        run_worker(worker)
        np.testing.assert_array_equal(worker.axis_data["x"], [0])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1])
        np.testing.assert_array_equal(worker.dataGrid, [[1], [11]])


@pytest.mark.parametrize(
    ("slow_order", "fast_order", "repeat_offset", "expected_x", "expected_grid"),
    [
        ([0, 1], [0, 2, 1], 0, [0, 1, 2], [[0, 1, 2], [10, 11, 12]]),
        ([1, 0], [2, 1, 0], 0, [0, 1, 2], [[0, 1, 2], [10, 11, 12]]),
        ([0, 1], [0, 2, 0], 2, [0, 2], [[1, 2], [11, 12]]),
    ],
    ids=["unordered-fast", "descending-both", "repeated-fast"],
)
def test_real_qcodes_shaped_and_unshaped_coordinate_order_equivalent(
    tmp_path, slow_order, fast_order, repeat_offset, expected_x, expected_grid,
):
    path = tmp_path / "coordinate_order.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("coordinate_order", sample_name="test")
    run_ids = []
    for shaped in (False, True):
        name = "shaped_scan" if shaped else "unshaped_scan"
        measurement = Measurement(exp=experiment, name=name)
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast")
        measurement.register_custom_parameter("signal", setpoints=("slow", "fast"))
        if shaped:
            measurement.set_shapes({"signal": (2, 3)})
        with measurement.run() as datasaver:
            for slow in slow_order:
                for index, fast in enumerate(fast_order):
                    datasaver.add_result(
                        ("slow", slow), ("fast", fast),
                        ("signal", 10 * slow + fast + (
                            repeat_offset if index == 2 else 0
                        )),
                    )
            run_ids.append(datasaver.dataset.run_id)

    before = hashlib.sha256(path.read_bytes()).digest()
    results = []
    for run_id in run_ids:
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            worker = make_worker(dataset)
            run_worker(worker)
            results.append((worker.axis_data, worker.dataGrid))
        finally:
            dataset.conn.close()

    for axes, grid in results:
        np.testing.assert_array_equal(axes["x"], expected_x)
        np.testing.assert_array_equal(axes["y"], [0, 1])
        np.testing.assert_array_equal(grid, expected_grid)
    np.testing.assert_array_equal(results[0][1], results[1][1])
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()
