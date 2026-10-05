"""Bounded means round acquired integer values only after accumulation."""
from fractions import Fraction

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from qplot.tools.finite_means import FiniteBinMeans
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed"])
@pytest.mark.parametrize("values", [
    (2**63 + 1, -2**63), (2**63, -(2**63) + 1), (2**53 + 1, 2**53 + 2),
    (2**51, 2**51, 2**51, 2**51 + 1, 2**51 + 1),
])
def test_real_bounded_integer_mean(tmp_path, monkeypatch, route, values):
    records = [(0, [0], np.array([v], dtype=np.uint64 if v >= 2**63 else np.int64))
               for v in values]
    records.extend([(1000, [1000], np.array([3], dtype=np.int64)),
                    (2000, [2000], np.array([3], dtype=np.int64))])
    if route == "streamed":
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 1)
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4)
        run_worker(worker)
        expected = float(sum(map(Fraction, values), Fraction()) / len(values))
        assert worker.dataGrid[0, 0] == expected
        assert np.isnan(worker.dataGrid[0, 1])
        assert worker.dataGrid[1, 1] == 3
        assert worker.aggregated_heatmap_source == (route == "streamed")


@pytest.mark.parametrize("values", [
    np.array([2**53 + 1, 2**53 + 2], dtype=np.int64),
    np.array([2**63 + 1, -(2**63)], dtype=object),
    np.array([2**51, 2**51, 2**51, 2**51 + 1, 2**51 + 1], dtype=np.int64),
])
def test_cross_chunk_exact_replay_keeps_recorded_values(values):
    original = values.copy()
    means = FiniteBinMeans((1, 1))
    indices = (np.array([0]), np.array([0]))
    for value in values:
        means.add(indices, np.array([value], dtype=values.dtype))
    assert means.begin_exact_replay()
    for value in values:
        means.replay(indices, np.array([value], dtype=values.dtype))
    assert means.means()[0, 0] == float(sum(map(Fraction, values.tolist()), Fraction()) / len(values))
    np.testing.assert_array_equal(values, original)


def test_integer_sum_risk_survives_later_floating_record():
    parts = [np.array([2**51], dtype=np.int64)] * 3
    parts += [np.array([2**51 + 1], dtype=np.int64), np.array([2**51 + 1.], dtype=float)]
    indices = (np.array([0]), np.array([0]))
    means = FiniteBinMeans((1, 1))
    for part in parts:
        means.add(indices, part)
    assert means.begin_exact_replay()
    for part in parts:
        means.replay(indices, part)
    assert means.means()[0, 0] == 2**51 + .5


def test_integer_exact_replay_remains_cancellable():
    cancelled = False
    def check():
        if cancelled:
            raise InterruptedError("cancelled")
    means = FiniteBinMeans((1, 1), check)
    indices = (np.array([0]), np.array([0]))
    means.add(indices, np.array([2**63 + 1], dtype=np.uint64))
    assert means.begin_exact_replay()
    cancelled = True
    with pytest.raises(InterruptedError):
        means.replay(indices, np.array([2**63 + 1], dtype=np.uint64))
