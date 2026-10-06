import sqlite3
import threading
from fractions import Fraction

import numpy as np
import pytest

from qplot.datahandling import trusted_heatmap, trusted_plot
from qplot.datahandling.trusted_heatmap import NumericHeatmap, NumericHeatmapPlan
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadRequestCancelledError,
)
from qplot.tools.worker import loader
from tests.datahandling.test_trusted_plot import (
    make_run,
    prohibit_snapshots,
    protected_state,
)


def plan(**overrides):
    values = dict(x='y', y='x', z='z', full_limit=4, max_cells=4, max_side=2,
                  ranges=(None, None), operations=False)
    return NumericHeatmapPlan(**(values | overrides))


@pytest.mark.parametrize('dense_tail', [False, True])
def test_adaptive_summary_matches_small_batches_and_bounds_dense_replies(tmp_path, monkeypatch, dense_tail):
    from qplot.datahandling.trusted_live_service import _BrokerQueryExecutor

    path = tmp_path / 'summary-boundary.db'
    _, guid, table = make_run(path)
    length = 700_003 if dense_tail else 400_003
    with sqlite3.connect(path) as writer:
        writer.execute(f'DELETE FROM "{table}"')
        writer.executemany(f'INSERT INTO "{table}" (id,x,y,z) VALUES (?,?,?,?)',
                           ((i + 1, i, i if dense_tail and i >= 196_608 else i % 2, i % 97 + 1)
                            for i in range(length)))
    writer.close()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    original = _BrokerQueryExecutor.query_batch
    summaries = []

    def record(executor, queries, **kwargs):
        results = original(executor, queries, **kwargs)
        if queries[0].sql.startswith('SELECT COUNT(*), COUNT('):
            summaries.append((queries[0].bindings, [len(result.rows) for result in results]))
        return results

    monkeypatch.setattr(_BrokerQueryExecutor, 'query_batch', record)
    captures = []
    for batch in (262_144, 65_536):
        monkeypatch.setattr(trusted_heatmap, 'LARGE_SUMMARY_ROWS', batch)
        summaries.clear()
        service = TrustedLiveReadService(path)
        try:
            dataset = service.submit_plot_dataset(guid).wait()
            result = service.submit_plot_prefix(
                dataset, heatmap_plan=plan(x='x', y='y', max_cells=64, max_side=8),
            ).wait()
            captures.append(result)
            assert result.row_count == result.selected_count == result.matching_count == length
        finally:
            service.close()
        assert all(sum(sizes) <= 2 * 65_536 + 3 for _, sizes in summaries)
        if batch == 262_144:
            assert [interval for interval, _ in summaries] == [
                (0, 65_536), (65_536, 131_072), (131_072, 196_608),
            ] + ([(196_608, 458_752), (458_752, length)] if dense_tail else [(196_608, length)])
            assert summaries[3][1] == [1, 1, 131_073 if dense_tail else 2]
            if dense_tail:
                assert summaries[-1][1] == [1, 1]  # Bounds/counts continue after both axes saturate.
        else:
            assert len(summaries) == (11 if dense_tail else 7)
    actual, reference = captures
    np.testing.assert_array_equal(actual.grid, reference.grid)
    for axis, expected in zip(actual.axes, reference.axes, strict=True):
        np.testing.assert_array_equal(axis, expected)
    assert actual.bounds == reference.bounds
    assert actual.cardinality_exact == reference.cardinality_exact == (False, not dense_tail)
    assert actual.unique_counts == reference.unique_counts
    assert protected_state(path) == before


def test_larger_aggregate_batches_preserve_weighted_means_and_reply_limits(tmp_path, monkeypatch):
    from qplot.datahandling.trusted_live_service import _BrokerQueryExecutor

    path = tmp_path / 'batch-boundary.db'
    _, guid, table = make_run(path)
    length = 262_145  # One full new batch and a one-row tail with a large outlier.
    values = np.arange(length, dtype=np.int64) % 101
    values[-1] = 1_000_000_000
    with sqlite3.connect(path) as writer:
        writer.execute(f'DELETE FROM "{table}"')
        writer.executemany(f'INSERT INTO "{table}" (id,x,y,z) VALUES (?,?,?,?)',
                           ((i + 1, i % 16, (i // 16) % 8, int(value))
                            for i, value in enumerate(values)))
    writer.close()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    original = _BrokerQueryExecutor.query
    replies = []

    def record(executor, sql, bindings=None, **kwargs):
        result = original(executor, sql, bindings, **kwargs)
        if 'GROUP BY gx, gy' in sql:
            replies.append((bindings[-2] - bindings[-3], len(result.rows)))
        return result

    monkeypatch.setattr(_BrokerQueryExecutor, 'query', record)
    grids = []
    for batch in (trusted_heatmap.AGGREGATE_ROWS, 65_536):
        monkeypatch.setattr(trusted_heatmap, 'AGGREGATE_ROWS', batch)
        replies.clear()
        service = TrustedLiveReadService(path)
        try:
            dataset = service.submit_plot_dataset(guid).wait()
            result = service.submit_plot_prefix(
                dataset, heatmap_plan=plan(x='x', y='y', max_cells=128, max_side=16),
            ).wait()
            grids.append(result.grid.copy())
            assert result.row_count == result.matching_count == length
        finally:
            service.close()
        assert [span for span, _ in replies] == [batch] * (length // batch) + [length % batch]
        assert all(size <= trusted_heatmap.MAX_GROUPS for _, size in replies)
    np.testing.assert_array_equal(grids[0], grids[1])
    keys = np.arange(length) % 128
    expected = (np.bincount(keys, weights=values) / np.bincount(keys)).reshape(8, 16)
    np.testing.assert_array_equal(grids[0], expected)
    assert protected_state(path) == before


@pytest.mark.parametrize('ranges', [(None, None), ((1., 3.), (0., 2.)), ((100., 101.), None)])
def test_paged_means_match_existing_spatial_renderer(tmp_path, monkeypatch, ranges):
    import qplot.tools.worker as worker_module
    path = tmp_path / 'means.db'
    _, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    # Unequal group sizes straddle page boundaries. Nonfinite observations
    # must affect neither extents nor means.
    writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                       [(0, 0, 1), (0, 0, 2), (0, 0, 3), (1, 1, float('inf')),
                        (float('inf'), 1, 999), (1, 1, None)])
    writer.commit()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_heatmap, 'SUMMARY_ROWS', 5)
    monkeypatch.setattr(trusted_heatmap, 'AGGREGATE_ROWS', 5)
    monkeypatch.setattr(trusted_heatmap, 'MAX_GROUPS', 2)  # exercise page splitting
    monkeypatch.setattr(worker_module, 'MAX_SQL_HEATMAP_SOURCE_ROWS', 1)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        axis_ranges = {axis: bounds for axis, bounds in zip(('x', 'y'), ranges, strict=True)
                       if bounds is not None}
        def render():
            worker = loader(dataset.cache, dataset.paramspecs['z'], dataset.paramspecs,
                            {'x': 'y', 'y': 'x'}, max_full_heatmap_points=4,
                            max_heatmap_grid_cells=4, max_heatmap_grid_side=2,
                            heatmap_axis_ranges=axis_ranges)
            errors, finished = [], []
            worker.emitter.errorOccurred.connect(errors.append)
            worker.emitter.finished.connect(finished.append)
            worker.run()
            assert not errors
            assert finished == [True]
            return worker
        actual = render()
        assert all(isinstance(value, NumericHeatmap) for value in service._plot_prefix_cache.values())
        monkeypatch.setattr(loader, '_trusted_heatmap_plan', lambda self: None)
        reference = render()
        np.testing.assert_allclose(actual.dataGrid, reference.dataGrid, rtol=1e-14, equal_nan=True)
        for axis in ('x', 'y'):
            np.testing.assert_array_equal(actual.axis_data[axis], reference.axis_data[axis])
        assert protected_state(path) == before
    finally:
        service.close()
        writer.close()


@pytest.mark.parametrize('values', [
    [1e308, 1e308], [1e308, 1., -1e308], [2**53 + 1, 2**53 + 3, -(2**53)],
])
def test_paged_aggregation_preserves_sensitive_means(tmp_path, monkeypatch, values):
    path = tmp_path / 'precision.db'
    _, guid, table = make_run(path)
    with sqlite3.connect(path) as writer:
        writer.execute(f'DELETE FROM "{table}"')
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (0,0,?)', [(v,) for v in values])
    writer.close()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_heatmap, 'AGGREGATE_ROWS', 1)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        result = service.submit_plot_prefix(dataset, heatmap_plan=plan(full_limit=1)).wait()
        assert result.grid[0, 0] == float(sum(map(Fraction, values)) / len(values))
        assert protected_state(path) == before
    finally:
        service.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_large_summary_yield_allows_writer_checkpoint_append_and_cancel(tmp_path, monkeypatch, cancel):
    path = tmp_path / 'live-summary.db'
    run_id, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    writer.execute(f'DELETE FROM "{table}"')
    writer.executemany(f'INSERT INTO "{table}" (id,x,y,z) VALUES (?,?,0,7)',
                       [(i + 1, i) for i in range(48)])
    writer.execute('UPDATE runs SET is_completed=0 WHERE run_id=?', (run_id,))
    writer.commit()
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_heatmap, 'SUMMARY_ROWS', 4)
    monkeypatch.setattr(trusted_heatmap, 'LARGE_SUMMARY_ROWS', 16)
    monkeypatch.setattr(trusted_heatmap, 'MAX_AXIS_VALUES', 3)
    reached, release = threading.Event(), threading.Event()
    original = trusted_heatmap.numeric_heatmap

    def suspend(*args):
        steps = original(*args)
        try:
            next(steps)  # Four rows saturate x's exact-cardinality budget.
            next(steps)  # The first larger summary transaction has ended.
            reached.set()
            while not release.is_set():
                args[0].check_cancelled()
                yield
            return (yield from steps)
        finally:
            steps.close()

    monkeypatch.setattr(trusted_heatmap, 'numeric_heatmap', suspend)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        settings = plan(x='x', y='y', max_cells=8, max_side=8)
        request = service.submit_plot_prefix(dataset, heatmap_plan=settings)
        assert reached.wait(10)
        assert request.progress.phase == "Scanning coordinates"
        assert (request.progress.completed, request.progress.total) == (20, 48)
        assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        writer.executemany(f'INSERT INTO "{table}" (id,x,y,z) VALUES (?,?,0,7)',
                           [(i + 1, i) for i in range(48, 52)])
        writer.execute('UPDATE runs SET is_completed=1 WHERE run_id=?', (run_id,))
        writer.commit()
        before = protected_state(path)
        if cancel:
            assert request.cancel()
        release.set()
        if cancel:
            with pytest.raises(TrustedReadRequestCancelledError):
                request.wait(10)
        else:
            result = request.wait(10)
            assert not result.completed and result.row_count == 48
            assert result.bounds == ((0, 47), (0, 0))
            np.testing.assert_array_equal(result.grid, np.full((1, 4), 7.))
        refreshed = service.submit_plot_prefix(dataset, heatmap_plan=settings).wait()
        assert refreshed.completed and refreshed.row_count == 52
        assert refreshed.bounds == ((0, 51), (0, 0))
        np.testing.assert_array_equal(refreshed.grid, np.full((1, 4), 7.))
        assert protected_state(path) == before
    finally:
        release.set()
        service.close()
        writer.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_aggregate_capture_releases_source_and_fixes_append_watermark(tmp_path, monkeypatch, cancel):
    path = tmp_path / 'live.db'
    run_id, guid, table = make_run(path)
    writer = sqlite3.connect(path)
    writer.execute('UPDATE runs SET is_completed=0 WHERE run_id=?', (run_id,))
    writer.commit()
    prohibit_snapshots(monkeypatch)
    reached, release = threading.Event(), threading.Event()
    original = trusted_heatmap.numeric_heatmap

    def suspend(*args):
        steps = original(*args)
        try:
            next(steps)
            reached.set()
            while not release.is_set():
                args[0].check_cancelled()
                yield
            return (yield from steps)
        finally:
            steps.close()

    monkeypatch.setattr(trusted_heatmap, 'numeric_heatmap', suspend)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        settings = plan(max_cells=100, max_side=100)
        request = service.submit_plot_prefix(dataset, heatmap_plan=settings)
        assert reached.wait(10)
        assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,?,?)',
                           [(3, y, 30 + y) for y in range(4)])
        writer.execute('UPDATE runs SET is_completed=1 WHERE run_id=?', (run_id,))
        writer.commit()
        before = protected_state(path)
        if cancel:
            assert request.cancel()
        release.set()
        if cancel:
            with pytest.raises(TrustedReadRequestCancelledError):
                request.wait(10)
        else:
            result = request.wait(10)
            assert not result.completed
            np.testing.assert_array_equal(result.grid, np.arange(3)[:, None] * 10 + np.arange(4))
        result = service.submit_plot_prefix(dataset, heatmap_plan=settings).wait()
        assert result.completed and result.row_count == 16
        np.testing.assert_array_equal(result.grid, np.arange(4)[:, None] * 10 + np.arange(4))
        # Completed refresh validates source identity, then reuses plot data.
        monkeypatch.setattr(trusted_heatmap, 'numeric_heatmap', lambda *args: pytest.fail('recaptured'))
        assert service.submit_plot_prefix(dataset, heatmap_plan=settings).wait() is result
        assert protected_state(path) == before
    finally:
        release.set()
        service.close()
        writer.close()


def test_numeric_aggregation_never_merges_distinct_integer_coordinates(tmp_path, monkeypatch):
    from qplot.datahandling.trusted_live import TrustedLiveQueryError
    path = tmp_path / 'coordinates.db'
    _, guid, table = make_run(path)
    with sqlite3.connect(path) as writer:
        writer.execute(f'DELETE FROM "{table}"')
        writer.executemany(f'INSERT INTO "{table}" (x,y,z) VALUES (?,0,?)',
                           [(2**53, 1), (2**53 + 1, 100)])
    writer.close()
    before = protected_state(path)
    prohibit_snapshots(monkeypatch)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        with pytest.raises(TrustedLiveQueryError, match='coordinates would be merged'):
            service.submit_plot_prefix(dataset, heatmap_plan=plan(full_limit=1)).wait()
        assert service.submit_plot_dataset(guid).wait().guid == guid
        assert protected_state(path) == before
    finally:
        service.close()


def test_large_numeric_plot_never_stages_raw_rows_and_refuses_full_resolution_operations(tmp_path, monkeypatch):
    from qplot.datahandling.trusted_live import TrustedLiveResultLimitError
    from qplot.tools.operation_registry import OperationExecutionError
    path = tmp_path / 'bounded.db'
    _, guid, _ = make_run(path)
    prohibit_snapshots(monkeypatch)
    monkeypatch.setattr(trusted_plot, 'PlotPrefix', lambda: pytest.fail('staged raw acquisition rows'))
    monkeypatch.setattr(trusted_heatmap, 'MAX_AXIS_VALUES', 2)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        result = service.submit_plot_prefix(dataset, heatmap_plan=plan()).wait()
        assert isinstance(result, NumericHeatmap)
        assert result.grid.size <= 4
        assert result.cardinality_exact == (False, False)
        plot_worker = loader(
            dataset.cache, dataset.paramspecs['z'], dataset.paramspecs, {'x': 'y', 'y': 'x'},
            max_full_heatmap_points=4, max_heatmap_grid_cells=4, max_heatmap_grid_side=2,
        )
        plot_worker.run()
        assert not plot_worker.heatmap_full_resolution
        assert plot_worker.heatmap_downsample_info['source_dimensions_limited']
        with pytest.raises(OperationExecutionError):
            service.submit_plot_prefix(dataset, heatmap_plan=plan(operations=True)).wait()
        monkeypatch.setattr(trusted_heatmap, 'MAX_NUMERIC_HEATMAP_CELLS', 3)
        with pytest.raises(TrustedLiveResultLimitError, match='memory budget'):
            service.submit_plot_prefix(dataset, heatmap_plan=plan(max_cells=9, max_side=3)).wait()
        assert service.submit_plot_dataset(guid).wait().guid == guid
    finally:
        service.close()
