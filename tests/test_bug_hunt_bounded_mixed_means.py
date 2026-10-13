"""Mixed acquisition records must preserve fractional bin-mean distinctions."""

import sqlite3
from contextlib import closing
from fractions import Fraction

import numpy as np
import pytest

import qplot.tools.worker as worker_module
from qplot.datahandling import trusted_heatmap
from qplot.datahandling.readonly import sqlite_read_only_connection
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.tools.finite_means import FiniteBinMeans
from tests.datahandling.test_trusted_plot import (
    make_run,
    prohibit_snapshots,
    protected_state,
)
from tests.test_bug_hunt_tiny_heatmap_bins import _SqlExecutor
from tests.test_worker_array_heatmaps import heatmap_dataset, make_worker, run_worker


@pytest.mark.parametrize("route", ["memory", "buffered", "streamed"])
@pytest.mark.parametrize("integer_position", [0, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_qcodes_mixed_fractional_bin_mean(tmp_path, monkeypatch, route, integer_position, sign):
    baseline = sign * 2**51
    values = [baseline + sign * .5] * 2
    values.insert(integer_position, baseline)
    records = [(0, [0], np.array([value])) for value in values]
    records.extend([(1000, [1000], np.array([3])), (2000, [2000], np.array([3]))])
    monkeypatch.setattr(worker_module, "CANCELLATION_CHUNK_SIZE", 1)
    if route == "streamed":
        monkeypatch.setattr(worker_module, "MAX_SQL_HEATMAP_SOURCE_ROWS", 1)
    with heatmap_dataset(tmp_path, shaped=False, records=records) as dataset:
        worker = make_worker(dataset, force_sql_heatmap=route != "memory",
                             max_full_heatmap_points=4, max_heatmap_grid_cells=4)
        run_worker(worker)
        expected = float(sum(map(Fraction, values), Fraction()) / len(values))
        assert worker.dataGrid[0, 0] == expected
        assert np.isnan(worker.dataGrid[0, 1])
        assert worker.dataGrid[1, 1] == 3
        assert worker.aggregated_heatmap_source == (route == "streamed")


@pytest.mark.parametrize("integer_first", [False, True])
@pytest.mark.parametrize("integer_dtype", [np.int64, np.uint64, np.bool_])
def test_fractional_replay_tracks_each_bin_across_record_dtypes(integer_first, integer_dtype):
    integer = np.array([1, 2, 1], dtype=integer_dtype)
    fractional = np.array([.5, 2., .25])
    parts = [integer, fractional] if integer_first else [fractional, integer]
    indices = (np.zeros(3, dtype=int), np.arange(3))
    means = FiniteBinMeans((1, 3))
    for part in parts:
        means.add(indices, part)
    assert means.begin_exact_replay()
    # The unrelated bin contains only integral values and needs no replay.
    assert set(means.exact) == {0, 2}
    for part in parts:
        means.replay(indices, part)
    expected = [float((Fraction(int(value)) + Fraction(other)) / 2)
                for value, other in zip(integer, fractional, strict=True)]
    np.testing.assert_array_equal(means.means(), [expected])


def test_integral_floating_records_keep_vectorized_accumulation():
    means = FiniteBinMeans((1, 3))
    indices = (np.zeros(3, dtype=int), np.arange(3))
    for values in (np.array([1, 2, 3]), np.array([2., 3., 4.])):
        means.add(indices, values)
    assert not means.begin_exact_replay()
    np.testing.assert_array_equal(means.means(), [[1.5, 2.5, 3.5]])


@pytest.mark.parametrize("page_size", [1, 2, 100])
@pytest.mark.parametrize("integer_position", [0, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_sql_mixed_fractional_mean_is_independent_of_pages(
        tmp_path, monkeypatch, page_size, integer_position, sign):
    baseline = sign * 2**51
    values = [baseline + sign * .5] * 2
    values.insert(integer_position, baseline)
    monkeypatch.setattr(trusted_heatmap, "AGGREGATE_ROWS", page_size)
    with heatmap_dataset(tmp_path, arrays=False, shaped=False,
                         records=[(0, [0, 0, 0], values)]) as dataset:
        # Use the raw SQLite scalars, as the trusted reader does, without
        # QCoDeS's declared-type decimal text conversion.
        with closing(sqlite_read_only_connection(tmp_path / "array_heatmap.db")) as connection:
            plan = trusted_heatmap.NumericHeatmapPlan(
                "fast", "slow", "signal", 1, 4, 2, (None, None), False,
            )
            work = trusted_heatmap.numeric_heatmap(_SqlExecutor(connection), dataset, plan, 3, True)
            with pytest.raises(StopIteration) as finished:
                while True:
                    next(work)
        expected = float(sum(map(Fraction, values), Fraction()) / 3)
        assert finished.value.value.grid[0, 0] == expected


@pytest.mark.parametrize("page_size", [1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_native_trusted_reader_preserves_mixed_page_mean(tmp_path, monkeypatch, page_size, sign):
    path = tmp_path / "native-mixed-mean.db"
    _run_id, guid, table = make_run(path)
    baseline = sign * 2**51
    values = [baseline, baseline + sign * .5, baseline + sign * .5]
    # Modify only this newly created synthetic fixture, before viewing it.
    with closing(sqlite3.connect(path)) as writer:
        writer.execute(f'DELETE FROM "{table}"')
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (0,0,?)',
                           [(value,) for value in values])
        writer.commit()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_heatmap, "AGGREGATE_ROWS", page_size)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        plan = trusted_heatmap.NumericHeatmapPlan("x", "y", "z", 1, 4, 2, (None, None), False)
        result = service.submit_plot_prefix(dataset, heatmap_plan=plan).wait()
        assert result.grid[0, 0] == float(sum(map(Fraction, values), Fraction()) / 3)
    finally:
        service.close()
    assert protected_state(path) == before
