"""Bounded reader routes must retain finite acquired cell means."""

import numpy as np
import pytest

from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


@pytest.mark.parametrize("arrays", [True, False])
@pytest.mark.parametrize("values,expected", [
    (np.full(4, 1e308), 1e308),
    (np.array([1e308, 1e308, -1e308, -1e308]), 0.),
])
def test_sql_heatmap_exact_cells_use_bounded_means(tmp_path, arrays, values, expected):
    with heatmap_dataset(tmp_path, arrays=arrays, shaped=False,
                         records=[(0, np.zeros(4), values)]) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True)
        run_worker(worker)
        np.testing.assert_allclose(worker.dataGrid, [[expected]], rtol=2e-15, atol=0)


def test_sql_exact_cells_replay_cross_chunk_residual_and_small_bin(tmp_path, monkeypatch):
    import qplot.tools.worker as worker_module

    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 2)
    records = [(0, np.zeros(5), [1e307, 1., 1e308, -1e308, -1e307]),
               (1, [1], [1e-308])]
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True)
        run_worker(worker)
        np.testing.assert_array_equal(worker.dataGrid, [[.2, np.nan], [np.nan, 1e-308]])


def test_sql_exact_cell_replay_is_cancellable(tmp_path, monkeypatch):
    from qplot.tools.finite_means import FiniteBinMeans

    with heatmap_dataset(tmp_path, shaped=False,
                         records=[(0, np.zeros(4), np.full(4, 1e308))]) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True)
        original = FiniteBinMeans.replay
        calls = []

        def cancel_during_replay(means, indices, values):
            calls.append(True)
            worker.cancel()
            return original(means, indices, values)

        monkeypatch.setattr(FiniteBinMeans, "replay", cancel_during_replay)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert finished == [False] and errors == []
        assert calls == [True] and worker.is_cancelled()


@pytest.mark.parametrize("shaped", [False, True])
@pytest.mark.parametrize("force_sql", [False, True])
def test_inexact_integer_heatmap_coordinates_fail_explicitly(tmp_path, shaped, force_sql):
    from tests.test_worker_coordinate_precision import (
        coordinate_dataset,
        coordinate_worker,
        run_coordinate_worker,
    )

    coordinates = np.array([2**53 + 1, 2**53 + 9, 2**53 + 17], dtype=np.int64)
    with coordinate_dataset(tmp_path, coordinates=coordinates, shaped=shaped) as dataset:
        worker = coordinate_worker(dataset, force_sql_heatmap=force_sql)
        finished, errors = run_coordinate_worker(worker)
        assert finished == [False]
        assert len(errors) == 1 and "coordinate precision" in str(errors[0])
        assert getattr(worker, "dataGrid", None) is None
