"""Spatial means retain finite QCoDeS samples through every bounded path."""

from contextlib import contextmanager
from fractions import Fraction

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

import qplot.tools.worker as worker_module
from qplot.datahandling.readonly import load_by_id_read_only
from qplot.tools.finite_means import FiniteBinMeans
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker
from tests.windows.test_complex_line_data import database_state


@contextmanager
def finite_surface(tmp_path, values, arrays):
    path = tmp_path / "finite-means.db"
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("finite spatial means", "test")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("slow")
    measurement.register_custom_parameter("fast", paramtype="array" if arrays else "numeric")
    measurement.register_custom_parameter("signal", paramtype="array" if arrays else "numeric",
                                          setpoints=("slow", "fast"))
    try:
        with measurement.run(write_in_background=False) as saver:
            for slow, row in ((0, values), (1, np.full(len(values), 1e-308))):
                if arrays:
                    saver.add_result(("slow", slow), ("fast", np.arange(len(row))), ("signal", row))
                else:
                    for fast, value in enumerate(row):
                        saver.add_result(("slow", slow), ("fast", fast), ("signal", value))
            run_id = saver.dataset.run_id
    finally:
        experiment.conn.close()
    protected = database_state(path)
    dataset = load_by_id_read_only(run_id, str(path))
    try:
        yield dataset
    finally:
        dataset.conn.close()
        assert database_state(path) == protected


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed", "scalar"])
@pytest.mark.parametrize("values", [
    np.full(4, np.finfo(float).max),
    np.array([1e308, 1e308, -1e308, -1e308]),
    np.array([1e308, 1e308, np.nan, np.nan]),
])
def test_finite_bounded_means_match_rational_oracle(tmp_path, monkeypatch, route, values):
    if route in ("streamed", "scalar"):
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    # Chunks force overflow recovery to retain a bin's prior total correctly.
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    with finite_surface(tmp_path, values, arrays=route != "scalar") as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        run_worker(worker)
        assert worker.dataGrid.size > 0
        expected = []
        for pair in values.reshape(2, 2):
            finite = pair[np.isfinite(pair)]
            expected.append(float(sum(Fraction(float(value)) for value in finite) / len(finite))
                            if len(finite) else np.nan)
        np.testing.assert_array_equal(worker.axis_data["y"], [0., 1.])
        np.testing.assert_allclose(worker.dataGrid, [expected, [1e-308, 1e-308]],
                                   rtol=2e-15, atol=0, equal_nan=True)


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed", "scalar"])
@pytest.mark.parametrize("sign", [-1, 1])
def test_finite_mean_at_rounded_sum_boundary(tmp_path, monkeypatch, route, sign):
    if route in ("streamed", "scalar"):
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    value = sign * np.finfo(float).max / 3
    with heatmap_dataset(tmp_path, arrays=route != "scalar", shaped=False,
                         records=[(0, [0, 1, 2], np.full(3, value))]) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=1, max_heatmap_grid_cells=1,
                             max_heatmap_grid_side=1)
        run_worker(worker)
        np.testing.assert_array_equal(worker.dataGrid, [[value]])


@pytest.mark.parametrize("chunks", [1, 3, 9])
@pytest.mark.parametrize("sign", [-1, 1])
def test_rounding_boundary_across_chunks(chunks, sign):
    value = sign * np.finfo(float).max / 9
    means = FiniteBinMeans((1,))
    for start in range(0, 9, chunks):
        count = min(chunks, 9 - start)
        means.add((np.zeros(count, dtype=int),), np.full(count, value))
    np.testing.assert_allclose(means.means(), [value], rtol=2e-15, atol=0)


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed", "scalar"])
@pytest.mark.parametrize("leading", [1e308, 1e307, 1e200])
def test_overflow_recovery_retains_earlier_chunk_residual(tmp_path, monkeypatch, route, leading):
    if route in ("streamed", "scalar"):
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    records = [(0, [0, 1], [leading, 1]), (0, [2, 3], [1e308, -1e308]),
               (0, [4], [-leading])]
    with heatmap_dataset(tmp_path, arrays=route != "scalar", shaped=False,
                         records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=1, max_heatmap_grid_cells=1,
                             max_heatmap_grid_side=1)
        run_worker(worker)
        np.testing.assert_array_equal(worker.dataGrid, [[0.2]])


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed"])
def test_original_sample_replay_is_cancellable(tmp_path, monkeypatch, route):
    if route == "streamed":
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    with finite_surface(tmp_path, np.full(4, 1e308), arrays=True) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        replay_calls = []
        original_replay = FiniteBinMeans.replay

        def cancel_in_replay(means, indices, values):
            replay_calls.append(True)
            worker.cancel()
            original_replay(means, indices, values)

        monkeypatch.setattr(FiniteBinMeans, "replay", cancel_in_replay)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert replay_calls == [True]
        assert worker.is_cancelled()
        assert finished == [False] and errors == []
        assert not hasattr(worker, "dataGrid")


def test_ordinary_streaming_means_do_not_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    with finite_surface(tmp_path, np.array([2., 4., 6., 8.]), arrays=True) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True,
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        scans = []
        original_chunks = worker._array_heatmap_chunks

        def chunks(connection):
            scans.append(connection)
            yield from original_chunks(connection)

        def unexpected_replay(*_args):
            raise AssertionError("Ordinary finite sums must not replay source data")

        monkeypatch.setattr(worker, "_array_heatmap_chunks", chunks)
        monkeypatch.setattr(FiniteBinMeans, "replay", unexpected_replay)
        run_worker(worker)
        assert len(scans) == 2 and scans[0] is scans[1]
        np.testing.assert_allclose(worker.dataGrid, [[3., 7.], [1e-308, 1e-308]],
                                   rtol=2e-15, atol=0)


def test_replay_keeps_original_integer_cells_and_counts():
    values = np.array([-1e308, np.int64(2**53 + 1), 1e308], dtype=object)
    indices = (np.zeros(3, dtype=int),)
    means = FiniteBinMeans((2,))
    means.add(indices, values)
    counts = means.counts.copy()
    assert means.begin_exact_replay()
    means.replay(indices, values)
    np.testing.assert_array_equal(means.counts, counts)
    np.testing.assert_array_equal(means.means(), [float(Fraction(2**53 + 1, 3)), np.nan])


def test_streamed_replay_uses_visible_samples_and_one_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 2)
    records = [(0, [0, 1, 2], [1e308, 1e307, 1]),
               (0, [3, 4, 5, 6], [1e308, -1e308, -1e307, 1e308])]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True,
                             max_full_heatmap_points=1, max_heatmap_grid_cells=1,
                             max_heatmap_grid_side=1, heatmap_axis_ranges={"x": (1., 5.)})
        scans = []
        original_chunks = worker._array_heatmap_chunks

        def chunks(connection):
            scans.append(connection)
            yield from original_chunks(connection)

        monkeypatch.setattr(worker, "_array_heatmap_chunks", chunks)
        run_worker(worker)
        assert len(scans) == 3 and all(connection is scans[0] for connection in scans)
        np.testing.assert_array_equal(worker.dataGrid, [[0.2]])


@pytest.mark.parametrize("chunks", [1, 2, 4])
def test_overflow_recovery_preserves_cancellation_and_unrelated_small_bin(chunks):
    values = np.array([1e308, 1e308, -1e308, -1e308, 1e-308])
    targets = np.array([0, 0, 0, 0, 1])
    means = FiniteBinMeans((2,))
    for start in range(0, len(values), chunks):
        means.add((targets[start:start + chunks],), values[start:start + chunks])
    expected = [float(sum(Fraction(float(value)) for value in values[:4]) / 4), 1e-308]
    np.testing.assert_array_equal(means.means(), expected)


def test_exact_bin_recovery_checks_cancellation():
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise InterruptedError("cancelled")

    means = FiniteBinMeans((1,), check)
    with pytest.raises(InterruptedError, match="cancelled"):
        means.add((np.zeros(4096, dtype=int),), np.full(4096, 1e308))


def test_sql_failed_mean_repair_is_cancellable(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 3)
    with finite_surface(tmp_path, np.full(4, 1e308), arrays=False) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True,
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        original_fraction = worker_module.Fraction

        def cancel_during_repair(*args):
            if args:
                worker.cancel()
            return original_fraction(*args)

        monkeypatch.setattr(worker_module, "Fraction", cancel_during_repair)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert worker.is_cancelled()
        assert finished == [False] and errors == []
        assert not hasattr(worker, "dataGrid")
