"""Repeated and spatial means retain cancellation across bounded chunks."""

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from qplot.tools.finite_means import FiniteBinMeans
from tests.test_bounded_heatmap_means import finite_surface
from tests.test_heatmap_float_means import repeated_dataset
from tests.test_worker_array_heatmaps import make_worker, run_worker


@pytest.mark.parametrize('force_sql', [False, True])
@pytest.mark.parametrize('values', [[1e16, 1., -1e16], [-1e16, 1., 1e16]])
def test_repeated_cell_cancellation(tmp_path, force_sql, values):
    with repeated_dataset(tmp_path, np.array(values), shaped=False) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=force_sql)
        run_worker(worker)
        np.testing.assert_array_equal(worker.dataGrid, [[1/3], [1/3]])


@pytest.mark.parametrize('route', ['memory', 'buffered', 'streamed', 'scalar'])
def test_spatial_cancellation_and_unrelated_tiny_bins(tmp_path, monkeypatch, route):
    if route in ('streamed', 'scalar'):
        monkeypatch.setattr(worker_module, 'MAX_SQL_HEATMAP_SOURCE_ROWS', 3)
    monkeypatch.setattr(worker_module, 'CANCELLATION_CHUNK_SIZE', 2)
    values = np.array([1e16, 1., -1e16, -1e16, 1., 1e16])
    with finite_surface(tmp_path, values, arrays=route != 'scalar') as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != 'memory',
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        run_worker(worker)
        np.testing.assert_allclose(worker.dataGrid, [[1/3, 1/3], [1e-308, 1e-308]],
                                   rtol=2e-15, atol=0)


def test_mixed_sign_replay_cancels_before_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, 'MAX_SQL_HEATMAP_SOURCE_ROWS', 3)
    monkeypatch.setattr(worker_module, 'CANCELLATION_CHUNK_SIZE', 2)
    values = np.array([1e16, 1., -1e16, -1e16, 1., 1e16])
    with finite_surface(tmp_path, values, arrays=True) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=True,
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4,
                             max_heatmap_grid_side=2)
        original = FiniteBinMeans.replay
        calls = []

        def replay(means, indices, samples):
            calls.append(True)
            worker.cancel()
            return original(means, indices, samples)

        monkeypatch.setattr(FiniteBinMeans, 'replay', replay)
        finished, errors = [], []
        worker.emitter.finished.connect(finished.append)
        worker.emitter.errorOccurred.connect(errors.append)
        worker.run()
        assert calls == [True]
        assert finished == [False] and errors == []
        assert not hasattr(worker, 'dataGrid')
