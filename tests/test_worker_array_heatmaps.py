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
from qplot.datahandling.qcodes_cache import update_cache_parameter_data
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.operation_registry import OperationCall
from qplot.tools.plot_tools import differentiate, fill_heatmap
from qplot.tools.worker import loader


@contextmanager
def heatmap_dataset(
    tmp_path,
    *,
    arrays=True,
    shaped=True,
    records=None,
    scalar_fast=False,
    planned_shape=None,
):
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
        measurement.set_shapes({"signal": planned_shape or (2, 3)})
    if records is None:
        # Reverse the second sweep so independently sorting an array is wrong.
        records = [(0, [0, 1, 2], [0, 1, 2]), (1, [2, 1, 0], [12, 11, 10])]
    with measurement.run() as datasaver:
        for slow, fast, signal in records:
            if arrays:
                datasaver.add_result(
                    ("slow", slow), ("fast", np.asarray(fast)),
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


@contextmanager
def multidimensional_heatmap_dataset(tmp_path, *, widths=(2, 3), shaped=False, missing=False):
    path = tmp_path / "multidimensional_heatmap.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("multidimensional", sample_name="test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow", paramtype="array")
    measurement.register_custom_parameter("fast", paramtype="array")
    measurement.register_custom_parameter(
        "signal", paramtype="array", setpoints=("slow", "fast"),
    )
    if shaped:
        measurement.set_shapes({"signal": (4, max(widths))})
    try:
        with measurement.run() as datasaver:
            for offset, width in zip((0, 2), widths, strict=True):
                fast = np.tile(np.arange(width, dtype=float), (2, 1))
                slow = np.repeat(
                    np.arange(offset, offset + 2, dtype=float)[:, None], width, axis=1,
                )
                signal = 10 * slow + fast
                if missing:
                    if offset == 0:
                        signal[0, 1] = np.nan
                    else:
                        fast[0, 1] = np.nan
                # Different memory layouts must still use the same logical order.
                datasaver.add_result(
                    ("slow", slow), ("fast", np.asfortranarray(fast)),
                    ("signal", signal),
                )
            run_id = datasaver.dataset.run_id
    finally:
        datasaver.dataset.conn.close()
        experiment.conn.close()

    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == before_mtime
        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-journal").exists()


@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("widths", [(2, 3), (3, 3)], ids=["varying", "equal"])
def test_normal_multidimensional_array_heatmap(tmp_path, swapped, shaped, widths):
    with multidimensional_heatmap_dataset(tmp_path, widths=widths, shaped=shaped) as dataset:
        worker = make_worker(dataset)
        worker.axes_dict = {
            "x": "slow" if swapped else "fast",
            "y": "fast" if swapped else "slow", "z": "signal",
        }
        run_worker(worker)
        expected = np.array([[0, 1, 2], [10, 11, 12], [20, 21, 22], [30, 31, 32]], dtype=float)
        if widths[0] == 2:
            expected[:2, 2] = np.nan
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2, 3] if swapped else [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1, 2] if swapped else [0, 1, 2, 3])
        np.testing.assert_array_equal(worker.dataGrid, expected.T if swapped else expected)
        assert np.count_nonzero(np.isfinite(worker.dataGrid)) == 2 * sum(widths)
        assert worker.loaded_from_sql_heatmap == (widths[0] != widths[1])


@pytest.mark.parametrize("swapped", [False, True])
def test_normal_multidimensional_heatmap_preserves_missing_pairs(tmp_path, swapped):
    with multidimensional_heatmap_dataset(tmp_path, missing=True) as dataset:
        worker = make_worker(dataset)
        if swapped:
            worker.axes_dict = {"x": "slow", "y": "fast"}
        run_worker(worker)
        expected = np.array([
            [0, np.nan, np.nan], [10, 11, np.nan],
            [20, np.nan, 22], [30, 31, 32],
        ])
        np.testing.assert_array_equal(worker.axis_data["x"], [0, 1, 2, 3] if swapped else [0, 1, 2])
        np.testing.assert_array_equal(worker.axis_data["y"], [0, 1, 2] if swapped else [0, 1, 2, 3])
        np.testing.assert_array_equal(worker.dataGrid, expected.T if swapped else expected)
        assert worker.loaded_point_count == 8


@pytest.mark.parametrize("cancel", [False, True])
def test_normal_multidimensional_decoder_is_bounded_readonly_and_cancellable(
    tmp_path, monkeypatch, cancel,
):
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    with multidimensional_heatmap_dataset(tmp_path) as dataset:
        worker = make_worker(dataset)
        connect = worker_module.sqlite_read_only_connection
        chunks = worker._arrays_from_values
        decoded = []
        blobs = []

        class Blob:
            def __init__(self, blob):
                self.blob = blob
                blobs.append(blob)

            def __getattr__(self, name):
                return getattr(self.blob, name)

            def __len__(self):
                return len(self.blob)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.blob.close()

            def read(self, size=-1):
                # NPY headers are bounded separately from numeric slices.
                assert 0 <= size <= 128
                return self.blob.read(size)

        class Connection:
            def __init__(self, *args, **kwargs):
                self.conn = connect(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def blobopen(self, *args, **kwargs):
                assert kwargs["readonly"] is True
                return Blob(self.conn.blobopen(*args, **kwargs))

        def record_chunk(x, y, z):
            result = chunks(x, y, z)
            if np.asarray(z).size <= 2:
                decoded.append(result)
                if cancel:
                    worker.cancel()
            return result

        monkeypatch.setattr(worker_module, "sqlite_read_only_connection", Connection)
        monkeypatch.setattr(worker, "_arrays_from_values", record_chunk)
        monkeypatch.setattr(worker_module, "load_param_data_from_db",
                            lambda *args, **kwargs: pytest.fail("QCoDeS array decoder used"))
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert errors == []
        assert finished == [not cancel]
        assert worker._sql_connection is None
        assert not hasattr(worker, "cache_data")
        assert blobs
        for blob in blobs:
            with pytest.raises(Exception, match="closed"):
                blob.read(1)
        if cancel:
            assert len(decoded) == 1
            assert not hasattr(worker, "dataGrid")
        else:
            assert len(decoded) == 5
            x, y, z = (np.concatenate([chunk[axis] for chunk in decoded])
                       for axis in range(3))
            np.testing.assert_array_equal(x, [0, 1, 0, 1, 0, 1, 2, 0, 1, 2])
            np.testing.assert_array_equal(y, [0, 0, 1, 1, 2, 2, 2, 3, 3, 3])
            np.testing.assert_array_equal(z, [0, 1, 10, 11, 20, 21, 22, 30, 31, 32])


def test_normal_multidimensional_heatmap_keeps_full_resolution_operations(tmp_path):
    with multidimensional_heatmap_dataset(tmp_path) as dataset:
        seen = []

        def add_one(data):
            seen.append(data["z"].copy())
            return {"z": data["z"] + 1}

        # A small display limit must be applied only after the operation.
        worker = make_worker(dataset, operations=[OperationCall("Add one", add_one)],
                             max_heatmap_grid_cells=4, max_heatmap_grid_side=2)
        run_worker(worker)
        expected = np.array([[0, 1, np.nan], [10, 11, np.nan], [20, 21, 22], [30, 31, 32]])
        np.testing.assert_array_equal(seen[0], expected)
        np.testing.assert_array_equal(worker.dataGrid, expected + 1)


@pytest.mark.parametrize("limit", [9, 10], ids=["sample-limit", "sparse-grid-limit"])
def test_normal_multidimensional_operations_respect_resolution_limit(tmp_path, limit):
    with multidimensional_heatmap_dataset(tmp_path) as dataset:
        worker = make_worker(
            dataset, max_full_heatmap_points=limit,
            operations=[OperationCall("Must not run", lambda data: pytest.fail("Operation ran"))],
        )
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1
        assert "full-resolution operation limit" in str(errors[0])
        assert worker._sql_connection is None
        assert not hasattr(worker, "dataGrid")


@pytest.mark.parametrize("operations", [False, True])
def test_multidimensional_decoder_bounds_growth_since_preflight(tmp_path, monkeypatch, operations):
    with multidimensional_heatmap_dataset(tmp_path) as dataset:
        worker = make_worker(
            dataset, max_full_heatmap_points=8,
            operations=[OperationCall("Must not run", lambda data: pytest.fail("Operation ran"))]
            if operations else [],
        )
        count = worker._large_heatmap_point_count

        def earlier_count():
            count()  # Inspect the real headers and select the compatibility decoder.
            worker.total_point_count_estimate = 6
            return 6  # Simulate more records becoming visible after this preflight.

        monkeypatch.setattr(worker, "_large_heatmap_point_count", earlier_count)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [not operations]
        assert worker._sql_connection is None
        if operations:
            assert len(errors) == 1
            assert "full-resolution operation limit" in str(errors[0])
            assert not hasattr(worker, "dataGrid")
        else:
            assert errors == []
            assert worker.loaded_from_sql_heatmap
            assert worker.loaded_point_count == 10
            np.testing.assert_array_equal(worker.dataGrid, [
                [0, 1, np.nan], [10, 11, np.nan], [20, 21, 22], [30, 31, 32],
            ])


def test_normal_loader_does_not_fallback_for_unrelated_decode_errors(tmp_path, monkeypatch):
    with multidimensional_heatmap_dataset(tmp_path, widths=(3, 3)) as dataset:
        worker = make_worker(dataset)
        failure = ValueError("Unrelated decoding failure")

        def fail(*args, **kwargs):
            raise failure

        monkeypatch.setattr(worker_module, "load_param_data_from_db", fail)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False]
        assert errors == [failure]
        assert not worker.loaded_from_sql_heatmap
        assert worker._sql_connection is None


def paired_shaped_unshaped_results(
    tmp_path, slow_order, fast_order, signal_value, operation,
):
    """Run equivalent current-QCoDeS scalar scans with and without shape."""

    path = tmp_path / "operated_coordinate_order.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment(
        "operated_coordinate_order", sample_name="test",
    )
    run_ids = []
    for shaped in (False, True):
        measurement = Measurement(
            exp=experiment,
            name="shaped_scan" if shaped else "unshaped_scan",
        )
        measurement.register_custom_parameter("slow")
        measurement.register_custom_parameter("fast")
        measurement.register_custom_parameter(
            "signal", setpoints=("slow", "fast"),
        )
        if shaped:
            measurement.set_shapes({"signal": (len(slow_order), len(fast_order))})
        with measurement.run() as datasaver:
            for slow in slow_order:
                for fast in fast_order:
                    datasaver.add_result(
                        ("slow", slow),
                        ("fast", fast),
                        ("signal", float(signal_value(slow, fast))),
                    )
            run_ids.append(datasaver.dataset.run_id)

    before = hashlib.sha256(path.read_bytes()).digest()
    results = []
    for run_id in run_ids:
        dataset = load_by_id_read_only(run_id, str(path))
        try:
            worker = make_worker(dataset, operations=[operation])
            run_worker(worker)
            results.append((
                {axis: values.copy() for axis, values in worker.axis_data.items()},
                worker.dataGrid.copy(),
            ))
        finally:
            dataset.conn.close()

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not path.with_name(path.name + "-journal").exists()
    assert not path.with_name(path.name + "-wal").exists()
    return results


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


@pytest.mark.parametrize("limit", [None, 4], ids=["default", "bounded"])
@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_variable_length_array_heatmap(tmp_path, limit, swapped, missing):
    records = [(0, [0, 2, 1], [0, 2, 1]), (1, [3, 2, 1, 0], [13, 12, 11, 10])]
    expected = np.array([[0, 1, 2, np.nan], [10, 11, 12, 13]])
    if missing:
        records = [(0, [0, 2, 1], [0, np.nan, 1]),
                   (1, [3, 2, np.nan, 0], [13, 12, 11, 10])]
        expected[0, 2] = np.nan
        expected[1, 1] = np.nan
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, **({} if limit is None else {
            "max_full_heatmap_points": limit,
        }))
        if swapped:
            worker.axes_dict = {"x": "slow", "y": "fast"}
        run_worker(worker)
        assert worker.loaded_from_sql_heatmap == (limit == 4)

        def assert_result(result):
            np.testing.assert_array_equal(result.axis_data["x"], [0, 1] if swapped else [0, 1, 2, 3])
            np.testing.assert_array_equal(result.axis_data["y"], [0, 1, 2, 3] if swapped else [0, 1])
            np.testing.assert_array_equal(result.dataGrid, expected.T if swapped else expected)

        assert_result(worker)
        if limit is None:
            source = worker.cache_data["signal"]
            assert all(values.dtype == object for values in source.values())
            original = {name: [record.copy() for record in values]
                        for name, values in source.items()}
            assert update_cache_parameter_data(
                dataset.cache, "signal", worker.updated_read_status,
                worker.updated_write_status, worker.cache_data,
            )
            cached = make_worker(dataset, read_data=False)
            cached.axes_dict = worker.axes_dict.copy()
            run_worker(cached)
            assert_result(cached)
            for name, values in source.items():
                for value, original_value in zip(values, original[name], strict=True):
                    np.testing.assert_array_equal(value, original_value)


@pytest.mark.parametrize("complex_column", ["fast", "signal"])
@pytest.mark.parametrize("limit", [None, 4], ids=["cached", "bounded"])
def test_variable_length_complex_records_are_rejected(tmp_path, complex_column, limit):
    records = [(0, np.array([0, 2, 1]), np.array([0, 2, 1])),
               (1, np.array([3, 2, 1, 0]), np.array([13, 12, 11, 10]))]
    index = 1 if complex_column == "fast" else 2
    records = [tuple(value + 1j if position == index else value
                     for position, value in enumerate(record)) for record in records]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        if limit is None:
            # Use the real QCoDeS object arrays from an already populated
            # cache, bypassing SQL's independent complex BLOB validation.
            source = dataset.get_parameter_data()
            assert source["signal"][complex_column].dtype == object
            assert update_cache_parameter_data(
                dataset.cache, "signal", {"signal": 2}, {"signal": 0}, source,
            )
        worker = make_worker(dataset, read_data=limit is not None,
                             **({} if limit is None else {"max_full_heatmap_points": limit}))
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert finished == [False]
        assert len(errors) == 1
        assert "Complex-valued heatmaps" in str(errors[0])
        assert complex_column in str(errors[0])
        assert not hasattr(worker, "dataGrid")


@pytest.mark.parametrize("phase", ["count", "copy", None], ids=["cancel-count", "cancel-copy", "complete"])
def test_record_normalisation_is_private_linear_and_cancellable(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    records = [(0, [0, 2, 1], [0, 2, 1]), (1, [3, 2, 1, 0], [13, 12, 11, 10])]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        source = dataset.get_parameter_data()["signal"]
        original = {name: [record.copy() for record in values]
                    for name, values in source.items()}
        assert update_cache_parameter_data(
            dataset.cache, "signal", {"signal": 2}, {"signal": 0}, {"signal": source},
        )
        worker = make_worker(dataset, read_data=False)
        normalise = worker._normalise_array_records
        check = worker._check_cancelled
        empty = np.empty
        allocations = []

        def allocate(size, *, dtype):
            # Seven actual samples, never records * longest-record padding.
            assert size == 7
            result = empty(size, dtype=dtype)
            result.fill(-999)
            allocations.append(result)
            return result

        def check_normalisation():
            if phase == "count" and not allocations:
                worker.cancel()
            if phase == "copy" and any(np.any(values != -999) for values in allocations):
                # Cancellation is observed after at most one two-sample copy.
                assert sum(np.count_nonzero(values != -999) for values in allocations) <= 2
                worker.cancel()
            check()

        def normalise_with_checks(data):
            # Represent scalar slow setpoints as one value per record. QCoDeS
            # usually expands them already; the normaliser must also broadcast
            # this representation within each record rather than globally.
            data = {**data, "slow": np.array([0., 1.])}
            with monkeypatch.context() as patch:
                patch.setattr(worker, "_check_cancelled", check_normalisation)
                patch.setattr(worker_module.np, "empty", allocate)
                return normalise(data)

        monkeypatch.setattr(worker, "_normalise_array_records", normalise_with_checks)
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()
        assert errors == []
        assert finished == [phase is None]
        if phase is None:
            np.testing.assert_array_equal(worker.dataGrid, [[0, 1, 2, np.nan], [10, 11, 12, 13]])
        else:
            assert not hasattr(worker, "dataGrid")
            assert not hasattr(worker, "axis_data")
        assert len(allocations) == (0 if phase == "count" else 3)
        for name, values in source.items():
            for value, original_value in zip(values, original[name], strict=True):
                np.testing.assert_array_equal(value, original_value)


@pytest.mark.parametrize("streamed", [False, True], ids=["buffered", "streamed"])
@pytest.mark.parametrize("swapped", [False, True])
def test_partial_planned_array_scan_preserves_nonuniform_short_axis(
    tmp_path,
    monkeypatch,
    streamed,
    swapped,
):
    slow_values = np.array([0.0, 1.0, 100.0])
    fast_values = np.arange(1001, dtype=float)
    records = [
        (slow, fast_values, np.full(fast_values.size, slow))
        for slow in slow_values
    ]
    if streamed:
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2_000)

    with heatmap_dataset(
        tmp_path,
        records=records,
        planned_shape=(3000, 1001),
    ) as dataset:
        worker = make_worker(dataset)
        if swapped:
            worker.axes_dict = {"x": "slow", "y": "fast"}
        run_worker(worker)

        assert worker.loaded_from_sql_heatmap
        assert worker.aggregated_heatmap_source is streamed
        if swapped:
            np.testing.assert_array_equal(worker.axis_data["x"], slow_values)
            assert worker.axis_data["y"].size == 800
            assert worker.dataGrid.shape == (800, 3)
            np.testing.assert_allclose(
                worker.dataGrid,
                np.broadcast_to(slow_values, worker.dataGrid.shape),
            )
        else:
            assert worker.axis_data["x"].size == 800
            np.testing.assert_array_equal(worker.axis_data["y"], slow_values)
            assert worker.dataGrid.shape == (3, 800)
            np.testing.assert_allclose(
                worker.dataGrid,
                np.broadcast_to(slow_values[:, None], worker.dataGrid.shape),
            )


def test_partial_planned_array_viewport_reload_preserves_nonuniform_short_axis(
    tmp_path,
):
    slow_values = np.array([0.0, 1.0, 100.0])
    fast_values = np.arange(1001, dtype=float)
    records = [
        (slow, fast_values, np.full(fast_values.size, slow))
        for slow in slow_values
    ]
    with heatmap_dataset(
        tmp_path,
        records=records,
        planned_shape=(3000, 1001),
    ) as dataset:
        worker = make_worker(
            dataset,
            force_sql_heatmap=True,
            heatmap_axis_ranges={"x": (100.0, 900.0), "y": (-1.0, 101.0)},
            heatmap_full_axis_ranges={"x": (0.0, 1000.0), "y": (0.0, 100.0)},
        )
        run_worker(worker)

        assert worker.axis_data["x"].size == 800
        np.testing.assert_array_equal(worker.axis_data["y"], slow_values)
        assert worker.dataGrid.shape == (3, 800)
        for row, slow in zip(worker.dataGrid, slow_values, strict=True):
            finite = np.isfinite(row)
            assert np.any(finite)
            np.testing.assert_allclose(row[finite], slow)


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


@pytest.mark.parametrize(
    "max_full_heatmap_points, source_aggregated",
    [(100, False), (4, True)],
    ids=["full-resolution", "bounded-spatial-aggregation"],
)
def test_array_heatmap_missing_coordinate_pairs_survive_loading_threshold(
    tmp_path,
    monkeypatch,
    max_full_heatmap_points,
    source_aggregated,
):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 4)
    records = [
        (0, np.zeros(5), np.full(5, 10.0)),
        (1, np.ones(5), np.full(5, 20.0)),
    ]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(
            dataset,
            max_full_heatmap_points=max_full_heatmap_points,
        )
        run_worker(worker)

        np.testing.assert_array_equal(worker.axis_data["x"], [0.0, 1.0])
        np.testing.assert_array_equal(worker.axis_data["y"], [0.0, 1.0])
        np.testing.assert_allclose(
            worker.dataGrid,
            [[10.0, np.nan], [np.nan, 20.0]],
            equal_nan=True,
        )
        assert worker.aggregated_heatmap_source is source_aggregated
        if source_aggregated:
            assert worker.heatmap_downsample_info["source_aggregated"]
            assert not worker.heatmap_downsample_info["grid_binned"]
            assert not worker.heatmap_downsample_info["empty_bins_filled"]


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


@pytest.mark.parametrize("shaped", [False, True], ids=["unshaped", "shaped"])
@pytest.mark.parametrize("limit", [100, 4], ids=["cache", "bounded-sql"])
def test_complex_array_setpoint_is_rejected_without_losing_imaginary_values(
    tmp_path, shaped, limit,
):
    records = [
        (0, [1 + 10j, 2 + 20j, 3 + 30j], [0, 1, 2]),
        (1, [1 + 10j, 2 + 20j, 3 + 30j], [10, 11, 12]),
    ]
    with heatmap_dataset(tmp_path, shaped=shaped, records=records) as dataset:
        worker = make_worker(dataset, max_full_heatmap_points=limit)
        errors, finished = [], []
        worker.emitter.errorOccurred.connect(errors.append)
        worker.emitter.finished.connect(finished.append)
        worker.run()

        assert finished == [False]
        assert len(errors) == 1
        assert "complex" in str(errors[0]).lower()
        assert "coordinate" in str(errors[0]).lower()
        assert "fast" in str(errors[0])
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


@pytest.mark.parametrize(
    ("slow_order", "fast_order"),
    [
        ([0, 1], [0, 2, 1]),
        ([1, 0], [2, 1, 0]),
    ],
    ids=["unordered-fast", "descending-both"],
)
def test_real_qcodes_shaped_and_unshaped_derivatives_use_coordinate_order(
    tmp_path, slow_order, fast_order,
):
    operation = OperationCall(
        "dz/dx",
        lambda data, cancelled_callback=None: differentiate(
            "x", data, cancelled_callback=cancelled_callback,
        ),
        derivative_axis="x",
        cooperative=True,
    )

    results = paired_shaped_unshaped_results(
        tmp_path,
        slow_order,
        fast_order,
        lambda slow, fast: fast ** 2 + 10 * slow,
        operation,
    )

    for axes, grid in results:
        np.testing.assert_array_equal(axes["x"], [0, 1, 2])
        np.testing.assert_array_equal(axes["y"], [0, 1])
        np.testing.assert_allclose(grid, [[1, 2, 3], [1, 2, 3]])
    np.testing.assert_array_equal(results[0][1], results[1][1])


@pytest.mark.parametrize(
    ("which", "slow_order", "fast_order", "signal_value", "expected"),
    [
        (
            "right",
            [1, 0],
            [2, 1, 0],
            lambda slow, fast: (
                np.nan if slow == 0 and fast == 1 else 10 * slow + fast + 1
            ),
            [[1, 1, 3], [11, 12, 13]],
        ),
        (
            "below",
            [2, 1, 0],
            [1, 0],
            lambda slow, fast: (
                np.nan if slow == 1 and fast == 0 else 100 * fast + slow + 1
            ),
            [[1, 101], [1, 102], [3, 103]],
        ),
    ],
    ids=["fill-right-descending-x", "fill-below-descending-y"],
)
def test_real_qcodes_shaped_and_unshaped_directional_fill_uses_coordinate_order(
    tmp_path, which, slow_order, fast_order, signal_value, expected,
):
    operation = OperationCall(
        f"Fill {which.title()}",
        lambda data, cancelled_callback=None: fill_heatmap(
            which,
            data,
            max_depth=1,
            cancelled_callback=cancelled_callback,
        ),
        cooperative=True,
    )

    results = paired_shaped_unshaped_results(
        tmp_path, slow_order, fast_order, signal_value, operation,
    )

    for axes, grid in results:
        np.testing.assert_array_equal(axes["x"], sorted(fast_order))
        np.testing.assert_array_equal(axes["y"], sorted(slow_order))
        np.testing.assert_array_equal(grid, expected)
    np.testing.assert_array_equal(results[0][1], results[1][1])
